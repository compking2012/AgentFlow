from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.common import canonical_digest

Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Revision = Annotated[int, Field(ge=1)]


def new_id() -> str:
    return str(uuid4())


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AppTarget(StrEnum):
    WEB = "web"
    API = "api"
    IOS = "ios_native"
    ANDROID = "android_native"
    WINDOWS = "windows_native"
    MACOS = "macos_native"
    LINUX = "linux_native"


class JobKind(StrEnum):
    PROBE = "capability_probe"
    BUILD = "build"
    INSTALL = "install"
    TEST = "test"


class QualityResult(StrEnum):
    UNKNOWN = "unknown"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not_applicable"


class VersionRef(WireModel):
    object_id: str
    kind: str
    revision: Revision
    fingerprint: Digest


class ManifestRef(WireModel):
    manifest_id: str
    revision: Revision = 1
    fingerprint: Digest


class ToolRequirement(WireModel):
    name: str = Field(min_length=1, max_length=100)
    version_constraint: str = Field(min_length=1, max_length=200)


class TargetConfig(WireModel):
    target_config_id: str = Field(default_factory=new_id)
    revision: Revision = 1
    app_target: AppTarget
    os_name: str = Field(min_length=1)
    os_version_constraint: str = Field(min_length=1)
    cpu_architecture: str = Field(min_length=1)
    ui_framework: str | None = None
    ui_framework_version_constraint: str | None = None
    required_display_protocol: Literal[
        "not_required", "any", "x11", "wayland", "windows_desktop", "macos_aqua", "android", "ios"
    ]
    required_device_mode: Literal["not_required", "any", "simulator", "physical"]
    device_model_constraints: list[str] = Field(default_factory=list)
    required_resource_ids: list[str] = Field(default_factory=list)
    sdk_requirements: list[ToolRequirement] = Field(default_factory=list)
    build_backend: ToolRequirement | None = None
    test_backend: ToolRequirement | None = None
    required_capabilities: list[
        Literal["interactive_session", "screen_capture", "input_control", "accessibility", "protocol_calls"]
    ] = Field(default_factory=list)
    source_test_plan_ref: VersionRef | None = None

    @model_validator(mode="after")
    def distinguish_native(self) -> TargetConfig:
        if self.app_target == AppTarget.LINUX and self.required_display_protocol not in {"x11", "wayland", "any"}:
            raise ValueError("Linux native requires an explicitly declared display protocol")
        if self.app_target not in {AppTarget.WEB, AppTarget.API} and not self.ui_framework:
            raise ValueError("native targets require their actual UI framework")
        if len(self.required_resource_ids) != len(set(self.required_resource_ids)):
            raise ValueError("duplicate resource IDs")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ToolObservation(WireModel):
    name: str
    path: str | None
    version: str | None
    available: bool
    executable_fingerprint: str | None = None
    detail: str = ""


class DisplayObservation(WireModel):
    protocol: str = "none"
    mode: Literal["headless", "virtual", "physical", "simulator"] = "headless"
    compositor: str | None = None
    session_fingerprint: str
    interactive: bool = False
    unlocked: bool | None = None
    screen_capture: Literal["verified", "unavailable", "unknown"] = "unknown"
    input_control: Literal["verified", "unavailable", "unknown"] = "unknown"
    accessibility: Literal["verified", "unavailable", "unknown"] = "unknown"
    accessibility_backend: str | None = None


class CapabilityReport(WireModel):
    probe_id: str = Field(default_factory=new_id)
    app_target: AppTarget
    target_config_fingerprint: str
    os_name: str
    os_version: str
    architecture: str
    boot_fingerprint: str
    display: DisplayObservation
    tools: list[ToolObservation]
    state: Literal["static_verified", "blocked", "failed"]
    blocking_reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    observed_at: str
    # Only controller-validated reference execution can grant functional support.
    functional_verified: Literal[False] = False

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    @property
    def environment_fingerprint(self) -> str:
        return canonical_digest({"os_name": self.os_name, "os_version": self.os_version,
                                 "architecture": self.architecture, "boot": self.boot_fingerprint,
                                 "display": self.display.model_dump(mode="json"),
                                 "tools": [t.model_dump(mode="json") for t in self.tools]})


class PairingCreateRequest(WireModel):
    node_label: str = Field(min_length=1, max_length=160)
    location: Literal["controller_host", "user_lan_host", "user_vm"] = "user_lan_host"
    expected_node_public_key_fingerprint: Digest
    allowed_app_targets: list[AppTarget] = Field(min_length=1, max_length=7)
    expires_in_seconds: int = Field(default=300, ge=60, le=900)


class PairingRedeemRequest(WireModel):
    single_use_code: str = Field(min_length=32, max_length=512)
    csr_pem: str = Field(min_length=64, max_length=20000)
    controller_certificate_fingerprint: Digest


class NodeIdentity(WireModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    certificate_fingerprint: Digest
    certificate_serial: str


class JobLimits(WireModel):
    maximum_active_seconds: int = Field(default=600, ge=1, le=86400)
    maximum_output_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    maximum_memory_bytes: int = Field(default=2 * 1024**3, ge=1024**2)
    maximum_processes: int = Field(default=64, ge=1, le=1024)


class ResourceLease(WireModel):
    lease_id: str
    resource_id: str
    resource_revision: Revision
    fencing_token: Revision
    expires_at: str


class JobClaimRequest(WireModel):
    operation_id: str
    node_revision: Revision
    boot_fingerprint: Digest
    capability_ids: list[str] = Field(min_length=1)
    available_resource_ids: list[str] = Field(default_factory=list)
    resume_job_id: str | None = None
    observed_process_fingerprint: Digest | None = None

    @model_validator(mode="after")
    def resume_requires_identity(self) -> JobClaimRequest:
        if self.resume_job_id and not self.observed_process_fingerprint:
            raise ValueError("resuming requires the recorded process identity")
        return self


class JobHeartbeatRequest(WireModel):
    expected_revision: Revision
    fencing_token: Revision
    input_fingerprint: Digest
    boot_fingerprint: Digest
    process_fingerprint: Digest | None
    observed_state: Literal["preparing", "starting", "running", "stopping", "process_exited"]
    resource_fences: list[ResourceLease] = Field(default_factory=list)
    last_activity_at: str
    local_journal_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def process_identity_after_preparation(self):
        if self.observed_state != "preparing" and self.process_fingerprint is None:
            raise ValueError("Executing or stopped processes require their recorded identity")
        return self


class JobResultRequest(WireModel):
    expected_revision: Revision
    operation_id: str
    job_kind: JobKind
    app_target: AppTarget
    fencing_token: Revision
    input_fingerprint: Digest
    execution_status: Literal["completed", "failed", "cancelled", "execution_unknown"]
    quality_result: QualityResult
    observed_source_manifest_fingerprint: Digest | None = None
    observed_platform_artifact_manifest_fingerprint: Digest | None = None
    observed_test_package_digest: Digest | None = None
    built_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    artifact_version_ids: list[str] = Field(default_factory=list)
    cleanup_evidence: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = Field(default="", max_length=16000)
    finished_at: str | None = None

    @model_validator(mode="after")
    def never_invent_success(self) -> JobResultRequest:
        if self.quality_result != QualityResult.PASSED:
            return self
        if self.execution_status != "completed":
            raise ValueError("only completed execution can contain passed quality")
        if self.job_kind == JobKind.BUILD:
            if not self.built_artifacts or not self.observed_source_manifest_fingerprint:
                raise ValueError("build success requires actual artifacts and source identity")
            if self.observed_platform_artifact_manifest_fingerprint is not None:
                raise ValueError("build cannot consume a future platform manifest")
        elif self.job_kind in {JobKind.INSTALL, JobKind.TEST}:
            if not all((self.observed_source_manifest_fingerprint,
                        self.observed_platform_artifact_manifest_fingerprint,
                        self.observed_test_package_digest)):
                raise ValueError("formal execution requires frozen source/platform/test identities")
            if self.job_kind == JobKind.TEST and not self.checks:
                raise ValueError("test success without check evidence is forbidden")
        elif not self.artifact_version_ids:
            raise ValueError("functional probe success requires evidence")
        return self
