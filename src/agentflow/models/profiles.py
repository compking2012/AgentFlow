from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from agentflow.common import DomainError, utc_now
from agentflow.runtime.contracts import DIGEST_PATTERN, ReasoningPolicy, Store, StrictModel


class PricingPolicy(StrictModel):
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    input_micros_per_million: int = Field(ge=0)
    output_micros_per_million: int = Field(ge=0)
    input_token_upper_bound: int = Field(gt=0)
    output_control_verified: bool = False
    input_bound_verified: bool = False
    source_version: str = Field(min_length=1)

    def reservation_cost(self, output_tokens: int) -> int:
        if not self.output_control_verified or not self.input_bound_verified:
            raise DomainError("budget_unbounded", "Model input/output cost bounds are not verified")
        return self.cost(self.input_token_upper_bound, output_tokens)

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        return (
            input_tokens * self.input_micros_per_million
            + output_tokens * self.output_micros_per_million
            + 999_999
        ) // 1_000_000


class ModelProfile(ReasoningPolicy):
    model_profile_id: str
    revision: int = Field(default=1, ge=1)
    provider: Literal["deepseek", "openai_compatible", "local_test"]
    requested_model: str
    provider_documented_version: str | None = None
    accepted_api_model: str | None = None
    acceptance_status: Literal["pending_user_confirmation", "accepted", "rejected", "unverified"] = (
        "pending_user_confirmation"
    )
    base_url: str
    protocols: list[Literal["chat_completions", "responses"]]
    credential_reference: str
    pricing: PricingPolicy | None = None
    max_output_tokens: int = Field(default=4096, ge=1)
    max_request_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    request_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    allow_loopback_upstream: bool = False
    allowed_tool_names: list[str] | None = None

    @field_validator("base_url")
    @classmethod
    def fixed_upstream(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("Upstream must be an HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Upstream cannot contain credentials/query/fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def only_explicit_local_test(self) -> ModelProfile:
        url = urlsplit(self.base_url)
        loopback = url.hostname in {"127.0.0.1", "::1", "localhost"}
        if self.allow_loopback_upstream and self.provider != "local_test":
            raise ValueError("Loopback upstream is a separately registered test provider")
        if url.scheme != "https" and not (loopback and self.allow_loopback_upstream):
            raise ValueError("Non-TLS upstream is not allowed outside explicit local protocol tests")
        if self.acceptance_status == "accepted" and not self.accepted_api_model:
            raise ValueError("Accepted profile needs an explicit API model")
        return self

    def assert_accepted(self, protocol: str) -> None:
        if self.acceptance_status != "accepted" or not self.accepted_api_model:
            raise DomainError("model_confirmation_required", "Model route has not been accepted", 409)
        if protocol not in self.protocols:
            raise DomainError("unsupported_protocol", "Protocol is not accepted for this profile", 422)

    def public_view(self) -> dict[str, Any]:
        return self.model_dump(exclude={"credential_reference", "base_url", "allow_loopback_upstream"})


class AttemptContext(ReasoningPolicy):
    attempt_id: str
    run_id: str
    iteration_id: str
    model_profile_id: str
    fencing_token: int = Field(ge=1)
    input_fingerprint: str = Field(pattern=DIGEST_PATTERN)
    expires_at: str
    deadline_monotonic: float | None = Field(default=None, ge=0, exclude=True)
    deadline_boot_identity: dict | None = Field(default=None, exclude=True)
    deadline_evidence: dict | None = Field(default=None, exclude=True)
    max_model_requests: int = Field(ge=0, strict=True)
    cost_mode: Literal["strict", "request_limited"] = "strict"
    max_output_tokens: int = Field(ge=1)
    max_tool_calls: int = Field(default=30, ge=0)
    allowed_tool_names: list[str] | None = None
    protocols: list[Literal["chat_completions", "responses"]] = Field(
        default_factory=lambda: ["chat_completions", "responses"]
    )

    def assert_current(self, protocol: str) -> None:
        if self.deadline_monotonic is not None:
            import math
            import time

            from agentflow.runtime.process_identity import boot_relation
            if (not math.isfinite(self.deadline_monotonic) or not self.deadline_boot_identity
                    or boot_relation(self.deadline_boot_identity) != 'same'):
                raise DomainError('stale_task_token', 'Task execution clock cannot be verified on this boot', 403)
            expired = time.monotonic() >= self.deadline_monotonic
        else:
            expired = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")) <= datetime.now(UTC)
        if expired:
            raise DomainError('task_authorization_expired', 'Task authorization expired', 403,
                              {'expires_at': self.expires_at, 'deadline_monotonic': self.deadline_monotonic})
        if protocol not in self.protocols:
            raise DomainError("forbidden", "Task is not authorized for this protocol", 403)


class ModelRegistry:
    """Profiles are controlled configuration, never fields copied from a wire request."""

    def __init__(self, store: Store):
        self.store = store

    async def get(self, profile_id: str) -> ModelProfile:
        raw = await self.store.read("model_profile", profile_id)
        if raw is None:
            raise DomainError("not_found", "Model profile not found", 404)
        raw = {k: v for k, v in raw.items() if k in ModelProfile.model_fields}
        return ModelProfile.model_validate(raw)

    async def list(self) -> list[dict[str, Any]]:
        records = await self.store.list("model_profile")
        return [
            ModelProfile.model_validate({k: v for k, v in raw.items() if k in ModelProfile.model_fields}).public_view()
            for raw in records
        ]

    async def register(self, profile: ModelProfile, key: str, expected_revision: int | None = None) -> dict:
        payload = profile.model_dump(exclude={"revision"})

        def apply(tx):
            result = tx.put("model_profile", profile.model_profile_id, payload, expected_revision)
            tx.event("model_profile_registered", {"profile_id": profile.model_profile_id})
            return {"profile_id": profile.model_profile_id, "revision": result["revision"]}

        return await self.store.command("model_profile", key, payload, apply)

    async def probe(self, profile_id: str) -> dict[str, Any]:
        profile = await self.get(profile_id)
        return {
            "profile_id": profile_id,
            "mode": "offline",
            "checked_at": utc_now(),
            "acceptance_status": profile.acceptance_status,
            "protocols": profile.protocols,
            "live_verified": False,
            "paid_requests_started": 0,
            "status": "configuration_valid",
            "strict_budget_supported": bool(
                profile.pricing and profile.pricing.input_bound_verified and profile.pricing.output_control_verified
            ),
        }
