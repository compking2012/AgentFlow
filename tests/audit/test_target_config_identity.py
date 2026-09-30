from __future__ import annotations

from agentflow.common import canonical_digest
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    freeze_platform_manifest,
)
from agentflow.execution.models import TargetConfig
from agentflow.execution.service import NodeService
from agentflow.storage import Store


async def test_two_configurations_of_one_app_target_select_their_own_test_package(tmp_path):
    store = Store(tmp_path / "controller")
    await store.start()
    try:
        await store.command("audit", "run", {}, lambda tx: tx.put("run", "run", {
            "execution_state": "running", "input_fingerprint": canonical_digest("input")}))
        service = NodeService(store, tmp_path / "controller", "https://127.0.0.1:8443")
        configs = [TargetConfig(target_config_id=identity, app_target="api", os_name="Linux",
            os_version_constraint=version, cpu_architecture="*", required_display_protocol="not_required",
            required_device_mode="not_required") for identity, version in (("linux-one", "1"), ("linux-two", "2"))]
        entries = [MatrixPlanEntry(matrix_entry_id=config.target_config_id, test_case_id=config.target_config_id,
            app_target="api", target_config_id=config.target_config_id, target_config_revision=1,
            component_roles=["product", "test"]) for config in configs]
        matrix = MatrixPlan(required_app_targets=["api"], target_configs=configs, entries=entries)
        source = SourceManifest(source_commit="a" * 40, source_tree_oid="b" * 40,
            source_bundle_artifact_version_id="source", source_bundle_digest=canonical_digest("source"),
            test_package_artifact_version_id="source-tests", test_package_digest=canonical_digest("tests"),
            build_plan_artifact_version_id="plan", build_plan_digest=canonical_digest("plan"),
            target_matrix_fingerprint=matrix.fingerprint, required_app_targets=("api",))
        builds = [BuildArtifact(artifact_id=f"{config.target_config_id}:{role}",
            artifact_version_id=f"{config.target_config_id}:{role}:version", app_target="api",
            target_config_id=config.target_config_id, component_role=role, kind=role,
            digest=canonical_digest(f"{config.target_config_id}:{role}"),
            source_manifest_fingerprint=source.fingerprint, toolchain_fingerprint=canonical_digest("toolchain"),
            verified_upload=True) for config in configs for role in ("product", "test")]
        platform = freeze_platform_manifest(source, matrix, builds)
        binding = bind_matrix(matrix, source, platform, {entry.matrix_entry_id: {} for entry in entries})
        job = await service.enqueue_job("run", kind="test", target_config=configs[0], idempotency_key="config-one",
            source_manifest=source, platform_manifest=platform, matrix_plan_fingerprint=matrix.fingerprint,
            matrix_binding_fingerprint=binding["binding_fingerprint"], matrix_entry_ids=[entries[0].matrix_entry_id],
            matrix_entries=[{"matrix_entry_id": entries[0].matrix_entry_id, "test_case_id": entries[0].test_case_id,
                             "framework_case_ids": ["required-test"]}])
        assert job["test_package_artifact_version_id"] == "linux-one:test:version"
    finally:
        await store.close()
