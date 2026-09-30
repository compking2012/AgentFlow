from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_serializer

from agentflow.common import DomainError

DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
Quality = Literal["unknown", "passed", "failed", "inconclusive", "not_applicable"]
Role = Literal[
    "research", "product", "architecture_planning", "development", "review", "unit_test", "integration_test"
]
ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh']


class Transaction(Protocol):
    def get(self, kind: str, id: str) -> dict[str, Any] | None: ...
    def put(
        self, kind: str, id: str, body: dict[str, Any], expected_revision: int | None = None
    ) -> dict[str, Any]: ...
    def event(self, type: str, body: dict[str, Any], run_id: str | None = None) -> Any: ...


class Store(Protocol):
    async def command(
        self, scope: str, key: str, payload: dict[str, Any], handler: Callable[[Transaction], dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def read(self, kind: str, id: str) -> dict[str, Any] | None: ...
    async def list(self, kind: str) -> list[dict[str, Any]]: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReasoningPolicy(StrictModel):
    """Optional Responses policy; unset serializes exactly like legacy records."""
    reasoning_effort: ReasoningEffort | None = None

    @model_serializer(mode='wrap')
    def serialize_policy(self, handler):
        value = handler(self)
        if self.reasoning_effort is None:
            value.pop('reasoning_effort', None)
        return value


class Capability(StrictModel):
    name: str
    status: Literal["verified", "observed", "unsupported", "unverified"]
    enforcement: Literal["hard", "observed", "unsupported", "not_applicable", "unverified"]
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class TaskEnvelope(ReasoningPolicy):
    attempt_id: str
    operation_id: str
    work_item_id: str
    run_id: str
    iteration_id: str
    role: Role
    goal: str = Field(min_length=1)
    review_phase_contract: dict[str, Any] | None = None
    planning_contract: dict[str, Any] | None = None
    input_fingerprint: str = Field(pattern=DIGEST_PATTERN)
    fencing_token: int = Field(ge=1)
    workspace: Path
    artifact_dir: Path
    allowed_read_roots: list[Path] = Field(default_factory=list)
    context_directory: Path | None = None
    allowed_write_roots: list[Path] = Field(default_factory=list)
    protected_roots: list[Path] = Field(default_factory=list)
    allow_code_write: bool = False
    allowed_web_hosts: list[str] = Field(default_factory=list)
    resolved_web_hosts: dict[str, list[str]] = Field(default_factory=dict)
    allow_public_web: bool = False
    model_profile_id: str
    model: str = Field(min_length=1)
    proxy_base_url: str
    proxy_token: SecretStr = Field(exclude=True, repr=False)
    max_active_seconds: float = Field(default=300, gt=0, le=86400)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_tool_calls: int = Field(default=30, ge=0)
    max_iterations: int = Field(default=30, ge=1, strict=True)
    max_log_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, strict=True)
    require_hard_tool_limit: bool = False
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    @model_serializer(mode='wrap')
    def serialize_policy(self, handler):
        value = super().serialize_policy(handler)
        if self.planning_contract is None:
            value.pop('planning_contract', None)
        if self.review_phase_contract is None:
            value.pop('review_phase_contract', None)
        return value

    @field_validator("proxy_base_url")
    @classmethod
    def loopback_proxy(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("Agent proxy must be an explicit loopback origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Proxy URL must not carry credentials, query or fragment")
        return value.rstrip("/")

    def assert_paths(self) -> None:
        workspace = self.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise DomainError("invalid_workspace", "Workspace is not a directory", 422)
        if self.allow_code_write and not self.allowed_write_roots:
            raise DomainError("write_scope_required", "Coding requires explicit writable workspace paths", 422)
        if any(not path.resolve().is_relative_to(workspace) for path in self.allowed_write_roots):
            raise DomainError("unsafe_write_scope", "Code writes must remain inside the owned workspace", 403)
        protected = [p.resolve() for p in self.protected_roots]
        for allowed in [workspace, *[p.resolve() for p in self.allowed_write_roots]]:
            if any(allowed == p or allowed.is_relative_to(p) or p.is_relative_to(allowed) for p in protected):
                raise DomainError("unsafe_workspace", "Writable and protected roots overlap", 403)


class LaunchSpec(StrictModel):
    attempt_id: str
    operation_id: str
    run_id: str
    input_fingerprint: str = Field(pattern=DIGEST_PATTERN)
    fencing_token: int = Field(ge=1)
    argv: list[str] = Field(min_length=1)
    cwd: Path
    environment: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    stdin_text: str = Field(default="", exclude=True, repr=False)
    timeout_seconds: float = Field(default=300, gt=0)
    stop_grace_seconds: float = Field(default=2, ge=0, le=30)
    max_log_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    backend: str = "managed_process"
    backend_version: str = "unknown"
    output_schema: dict[str, Any] | None = None
    final_output_path: Path | None = None

    @field_validator("argv")
    @classmethod
    def no_nuls(cls, value: list[str]) -> list[str]:
        if any("\0" in arg for arg in value):
            raise ValueError("NUL in command")
        if not Path(value[0]).is_absolute():
            raise ValueError("Executable path must be absolute; shell resolution is not allowed")
        return value


class BackendHandle(StrictModel):
    attempt_id: str
    operation_id: str
    backend: str
    backend_version: str
    input_fingerprint: str
    fencing_token: int
    state: str
    pid: int | None = None
    process_started_at: float | None = None
    boot_fingerprint: str | None = None
    launcher_nonce: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    exit_code: int | None = None
    reason: str | None = None
    active_seconds: float | None = None


def require_record(tx: Transaction, kind: str, id: str) -> dict[str, Any]:
    item = tx.get(kind, id)
    if item is None:
        raise DomainError("not_found", f"Missing {kind} record", 404)
    return item
