"""Immutable source/build manifests with a deliberately acyclic hash graph."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.models import (
    AppTarget,
    Digest,
    ManifestRef,
    Revision,
    TargetConfig,
    WireModel,
    new_id,
)


def file_digest(path: Path, *, maximum_bytes: int = 2 * 1024**3) -> str:
    if path.is_symlink() or not path.is_file():
        raise DomainError("unsafe_artifact", "Artifact must be a regular file, not a link", 422)
    if path.stat().st_size > maximum_bytes:
        raise DomainError("artifact_too_large", "Artifact exceeds the configured byte limit", 413)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return "sha256:" + h.hexdigest()


def tree_digest(directory: Path) -> str:
    """Includes all file names and contents; symlinks and special files are rejected."""
    if directory.is_symlink() or not directory.is_dir():
        raise DomainError("unsafe_tree", "Expected a real artifact directory", 422)
    rows = []
    for p in sorted(directory.rglob("*")):
        if p.is_symlink():
            raise DomainError("unsafe_tree", "Linked files cannot enter a frozen artifact", 422)
        if p.is_dir():
            continue
        if not p.is_file():
            raise DomainError("unsafe_tree", "Special files cannot enter a frozen artifact", 422)
        rows.append({"path": p.relative_to(directory).as_posix(), "size": p.stat().st_size,
                     "digest": file_digest(p), "executable": bool(p.stat().st_mode & 0o111)})
    if not rows:
        raise DomainError("empty_artifact", "An empty directory is not a built artifact", 422)
    return canonical_digest(rows)


class MatrixPlanEntry(WireModel):
    matrix_entry_id: str = Field(default_factory=new_id)
    test_case_id: str
    app_target: AppTarget
    required: bool = True
    component_roles: list[str] = Field(min_length=1)
    target_config_id: str
    target_config_revision: Revision


class MatrixPlan(WireModel):
    matrix_id: str = Field(default_factory=new_id)
    plan_revision: Revision = 1
    required_app_targets: list[AppTarget] = Field(min_length=1)
    target_configs: list[TargetConfig] = Field(min_length=1)
    entries: list[MatrixPlanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_plan(self) -> MatrixPlan:
        configs = {c.target_config_id: c for c in self.target_configs}
        if len(configs) != len(self.target_configs):
            raise ValueError("duplicate target configuration identity")
        seen: set[str] = set()
        for e in self.entries:
            if e.matrix_entry_id in seen:
                raise ValueError("duplicate matrix entry")
            seen.add(e.matrix_entry_id)
            c = configs.get(e.target_config_id)
            if c is None or c.revision != e.target_config_revision or c.app_target != e.app_target:
                raise ValueError("matrix entry references a different or missing target revision")
        required = {e.app_target for e in self.entries if e.required}
        if required != set(self.required_app_targets):
            raise ValueError("required targets must exactly match required planned entries")
        return self

    @property
    def fingerprint(self) -> str:
        # No bindings, results, manifests, nodes, or general view revision participate.
        value = self.model_dump(mode="json")
        value["required_app_targets"] = sorted(value["required_app_targets"])
        value["target_configs"].sort(key=lambda x: x["target_config_id"])
        value["entries"].sort(key=lambda x: x["matrix_entry_id"])
        for e in value["entries"]:
            e["component_roles"] = sorted(e["component_roles"])
        return canonical_digest(value)


class SourceManifest(WireModel):
    model_config = {"extra": "forbid", "frozen": True}
    manifest_id: str = Field(default_factory=new_id)
    revision: Revision = 1
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}([a-f0-9]{24})?$")
    source_tree_oid: str = Field(pattern=r"^[a-f0-9]{40}([a-f0-9]{24})?$")
    source_bundle_artifact_version_id: str
    source_bundle_digest: Digest
    test_package_artifact_version_id: str
    test_package_digest: Digest
    build_plan_artifact_version_id: str
    build_plan_digest: Digest
    target_matrix_fingerprint: Digest
    required_app_targets: tuple[AppTarget, ...]
    state: Literal["frozen"] = "frozen"

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def ref(self) -> ManifestRef:
        return ManifestRef(manifest_id=self.manifest_id, revision=self.revision, fingerprint=self.fingerprint)


class BuildArtifact(WireModel):
    artifact_id: str = Field(default_factory=new_id)
    artifact_version_id: str
    app_target: AppTarget
    target_config_id: str
    component_role: str
    kind: Literal["product", "test", "service", "data", "build_report"]
    digest: Digest
    source_manifest_fingerprint: Digest
    toolchain_fingerprint: Digest
    verified_upload: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlatformManifest(WireModel):
    model_config = {"extra": "forbid", "frozen": True}
    manifest_id: str = Field(default_factory=new_id)
    revision: Revision = 1
    source_manifest: ManifestRef
    target_matrix_fingerprint: Digest
    artifacts: tuple[BuildArtifact, ...]
    required_app_targets: tuple[AppTarget, ...]
    frozen_at: str = Field(default_factory=utc_now)
    state: Literal["frozen"] = "frozen"

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def ref(self) -> ManifestRef:
        return ManifestRef(manifest_id=self.manifest_id, revision=self.revision, fingerprint=self.fingerprint)


def freeze_platform_manifest(source: SourceManifest, plan: MatrixPlan,
                             artifacts: list[BuildArtifact]) -> PlatformManifest:
    if source.target_matrix_fingerprint != plan.fingerprint:
        raise DomainError("matrix_plan_changed", "Source references a different frozen matrix plan")
    if set(source.required_app_targets) != set(plan.required_app_targets):
        raise DomainError("target_scope_changed", "Source and matrix target scope differ")
    if len({a.artifact_id for a in artifacts}) != len(artifacts):
        raise DomainError("duplicate_artifact", "Build artifact identities must be unique", 422)
    for artifact in artifacts:
        if not artifact.verified_upload or artifact.source_manifest_fingerprint != source.fingerprint:
            raise DomainError("unverified_build", "Build upload or source provenance has not been verified")
    for entry in plan.entries:
        if not entry.required:
            continue
        for role in entry.component_roles:
            matches = [a for a in artifacts if a.component_role == role
                       and a.target_config_id == entry.target_config_id and a.app_target == entry.app_target]
            if not matches:
                raise DomainError("missing_build", f"Missing {entry.target_config_id}/{role}")
        kinds = {a.kind for a in artifacts if a.target_config_id == entry.target_config_id}
        if not {"product", "test"}.issubset(kinds):
            raise DomainError("missing_build", "Every required target needs product and prebuilt test artifacts")
    return PlatformManifest(source_manifest=source.ref(), target_matrix_fingerprint=plan.fingerprint,
                            artifacts=tuple(sorted(artifacts, key=lambda a: a.artifact_id)),
                            required_app_targets=tuple(sorted(plan.required_app_targets)))


def bind_matrix(plan: MatrixPlan, source: SourceManifest, platform: PlatformManifest,
                bindings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if platform.source_manifest.fingerprint != source.fingerprint or platform.target_matrix_fingerprint != plan.fingerprint:
        raise DomainError("manifest_mismatch", "Cannot bind manifests from different frozen inputs")
    rows = []
    for e in sorted(plan.entries, key=lambda item: item.matrix_entry_id):
        b = bindings.get(e.matrix_entry_id)
        if b is None and e.required:
            raise DomainError("missing_binding", f"Required matrix entry {e.matrix_entry_id} has no binding")
        if b is None:
            continue
        component_ids = sorted(a.artifact_id for a in platform.artifacts
                               if a.target_config_id == e.target_config_id and a.component_role in e.component_roles)
        rows.append({"matrix_entry_id": e.matrix_entry_id, "frozen_component_ids": component_ids,
                     "capability_id": b.get("capability_id"),
                     "expected_environment_fingerprint": b.get("expected_environment_fingerprint"),
                     "required_resource_ids": sorted(b.get("required_resource_ids", []))})
    payload = {"fingerprint": plan.fingerprint, "source_manifest": source.ref().model_dump(mode="json"),
               "platform_artifact_manifest": platform.ref().model_dump(mode="json"), "entries": rows}
    return {**payload, "binding_fingerprint": canonical_digest(payload)}


def execution_key(case_id: str, target_config_id: str, candidate_manifest_fingerprint: str) -> str:
    return canonical_digest({"case_id": case_id, "target_config_id": target_config_id,
                             "candidate_manifest_digest": candidate_manifest_fingerprint})


def target_artifacts(platform: dict, target: TargetConfig | dict) -> list[dict]:
    """Select the exact frozen environment, never all components of an application type."""
    config = target.model_dump(mode="json") if isinstance(target, TargetConfig) else target
    artifacts = platform["artifacts"]
    if any(not artifact.get("target_config_id") for artifact in artifacts):
        raise DomainError("artifact_target_missing", "Every frozen platform component must name its target configuration", 422)
    return [artifact for artifact in artifacts
            if artifact["target_config_id"] == config["target_config_id"]
            and artifact["app_target"] == config["app_target"]]


def service_components(platform: dict, recipe: dict) -> dict[str, dict]:
    result = {}
    for name in recipe.get("service_urls", {}):
        if name not in {"api", "web"}:
            raise DomainError("invalid_service_name", "Only declared API/Web service bindings are supported")
        target_id = recipe.get("service_target_config_ids", {}).get(name)
        candidates = [a for a in platform["artifacts"] if a["app_target"] == name
                      and a["kind"] in {"product", "application", "service", "api_service"}
                      and (target_id is None or a["target_config_id"] == target_id)]
        if len(candidates) != 1:
            raise DomainError("service_artifact_ambiguous", "An external service must identify exactly one frozen target component")
        digest = candidates[0].get("content_digest") or candidates[0].get("metadata", {}).get("content_digest")
        if (not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])):
            raise DomainError("service_artifact_identity_missing", "The frozen external service must bind its actual content digest")
        result[name] = candidates[0]
    return result
