
import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.execution.capabilities import version_matches
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    freeze_platform_manifest,
)
from agentflow.execution.models import AppTarget, JobKind, TargetConfig
from agentflow.testing.adapters import ADAPTERS, BuildRecipe, plan_execution


def target(kind="api", **kwargs):
    return TargetConfig(app_target=kind, os_name="Linux", os_version_constraint="*", cpu_architecture="*",
        required_display_protocol="x11" if kind == "linux_native" else "not_required",
        required_device_mode="simulator" if kind == "ios_native" else "not_required",
        ui_framework="GTK3" if kind.endswith("native") else None, **kwargs)


def test_matrix_fingerprint_has_no_manifest_result_cycle():
    config = target()
    entry = MatrixPlanEntry(test_case_id="save", app_target="api", component_roles=["app", "tests"],
                            target_config_id=config.target_config_id, target_config_revision=1)
    plan = MatrixPlan(required_app_targets=["api"], target_configs=[config], entries=[entry])
    first = plan.fingerprint
    source = SourceManifest(source_commit="a"*40, source_tree_oid="b"*40,
        source_bundle_artifact_version_id="source", source_bundle_digest=canonical_digest("source"),
        test_package_artifact_version_id="test", test_package_digest=canonical_digest("test"),
        build_plan_artifact_version_id="build", build_plan_digest=canonical_digest("build"),
        target_matrix_fingerprint=first, required_app_targets=("api",))
    artifacts = [BuildArtifact(artifact_version_id=k, app_target="api", target_config_id=config.target_config_id,
        component_role=role, kind=k, digest=canonical_digest(k), source_manifest_fingerprint=source.fingerprint,
        toolchain_fingerprint=canonical_digest("tools"), verified_upload=True)
        for k,role in [("product","app"),("test","tests")]]
    platform = freeze_platform_manifest(source, plan, artifacts)
    binding = bind_matrix(plan, source, platform, {entry.matrix_entry_id: {"capability_id": "first"}})
    newer = bind_matrix(plan, source, platform, {entry.matrix_entry_id: {"capability_id": "second"}})
    assert plan.fingerprint == first and binding["binding_fingerprint"] != newer["binding_fingerprint"]
    with pytest.raises(DomainError):
        freeze_platform_manifest(source, plan, artifacts[:1])


def test_all_seven_targets_have_real_tool_adapters():
    assert set(ADAPTERS) == set(AppTarget)


def test_apple_formal_tests_use_without_building(tmp_path):
    cfg = target("ios_native")
    recipe = BuildRecipe(adapter="ios_native", xctestrun="products/test.xctestrun", device_id="ABC-123",
                         product_path="products/app.app", test_product_path="products/tests.xctest")
    plan = plan_execution(JobKind.TEST, cfg, recipe, tmp_path)
    argv = plan.commands[0].argv
    assert "test-without-building" in argv and "build-for-testing" not in argv
    assert len(plan.frozen_input_paths) == 3


def test_windows_formal_tests_cannot_implicitly_restore_or_build(tmp_path):
    cfg = target("windows_native")
    recipe = BuildRecipe(adapter="windows_native", project_file="app.sln", gui_project="tests.csproj",
                         test_selectors=["Reference.GuiTests"])
    plan = plan_execution(JobKind.TEST, cfg, recipe, tmp_path)
    assert "--no-build" in plan.commands[0].argv and "--no-restore" in plan.commands[0].argv
    assert plan.commands[0].argv[-2:] == ("--filter", "FullyQualifiedName~Reference.GuiTests")


def test_wayland_is_not_silently_replaced_by_x11(tmp_path):
    cfg = target("linux_native")
    cfg.required_display_protocol = "wayland"
    with pytest.raises(DomainError, match="Wayland"):
        plan_execution(JobKind.TEST, cfg, BuildRecipe(adapter="linux_native", gui_project="tests"), tmp_path)


def test_recipe_paths_and_version_constraints_are_not_shell_programs(tmp_path):
    with pytest.raises(DomainError):
        plan_execution(JobKind.BUILD, target(), BuildRecipe(adapter="api", project_path="../outside"), tmp_path)
    assert version_matches("Xcode 16.2", ">=16,<17")
    assert not version_matches("Xcode 16.2", "$(touch /tmp/pwn)")
    assert not version_matches(None, "*")
