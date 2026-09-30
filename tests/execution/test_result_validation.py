"""Adversarial node claims use real uploaded reports and controller validation."""
import hashlib

import pytest
from conftest import source_manifest

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.manifests import BuildArtifact, PlatformManifest, execution_key
from agentflow.execution.models import JobClaimRequest


async def prepared_claim(paired, target, raw=b'<testsuite><testcase classname="C" name="one"/></testsuite>', *, two_cases=False, required_cases=None):
    service, identity, cap, boot, *_ = paired
    source = source_manifest()
    components = [BuildArtifact(artifact_id=f"{config}-{kind}", artifact_version_id=f"{config}-{kind}", app_target="api",
        target_config_id=config, component_role=kind, kind=kind, digest=canonical_digest(f"{config}-{kind}"),
        source_manifest_fingerprint=source.fingerprint, toolchain_fingerprint=cap["environment_fingerprint"], verified_upload=True)
        for config in [target.target_config_id, "other-api-configuration"] for kind in ["product", "test"]]
    platform = PlatformManifest(source_manifest=source.ref(), target_matrix_fingerprint=source.target_matrix_fingerprint,
                                artifacts=tuple(components), required_app_targets=("api",))
    entries = [{"matrix_entry_id": "one", "test_case_id": "logical-one", "framework_case_ids": required_cases or ["C::one"]}]
    if two_cases:
        entries.append({"matrix_entry_id": "two", "test_case_id": "logical-two", "framework_case_ids": ["C::two"]})
    await service.enqueue_functional_probe("run", capability_id=cap["id"], target_config=target, source_manifest=source,
        platform_manifest=platform, recipe={}, matrix_entries=entries, matrix_plan_fingerprint=source.target_matrix_fingerprint,
        matrix_binding_fingerprint=canonical_digest("binding"), idempotency_key="probe")
    job = (await service.claim_job(identity, JobClaimRequest(operation_id="claim", node_revision=1,
        boot_fingerprint=boot, capability_ids=[cap["id"]])))["assignment"]
    assert job["test_package_artifact_version_id"] == f"{target.target_config_id}-test"
    assert "other-api-configuration-test" not in job["download_artifact_version_ids"]
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    upload = await service.begin_upload(identity, job["job_id"], job["attempt_token"],
                                        {"name": "junit.xml", "size": len(raw), "digest": digest}, "upload")
    await service.append_chunk(identity, job["job_id"], upload["id"], job["attempt_token"], 0, raw, digest, "chunk")
    artifact = await service.complete_upload(identity, job["job_id"], upload["id"], job["attempt_token"], "complete")
    key = execution_key("logical-one", target.target_config_id, platform.fingerprint)
    check = {"matrix_entry_id": "one", "test_case_id": "logical-one", "raw_report_artifact_version_id": artifact["id"],
        "report_format": "junit", "quality_result": "passed", "platform_artifact_manifest_fingerprint": platform.fingerprint,
        "matrix_binding_fingerprint": job["matrix_binding_fingerprint"], "environment_fingerprint": cap["environment_fingerprint"],
        "exit_code": 0, "expected_execution_keys": [key], "actual_execution_keys": [key],
        "target_config_id": target.target_config_id, "target_config_revision": target.revision,
        "matrix_plan_fingerprint": job["matrix_plan_fingerprint"],
        "observed_components": [{"component_id": component.artifact_id, "actual_digest": component.digest}
                                for component in components if component.target_config_id == target.target_config_id]}
    request = {"expected_revision": job["revision"], "operation_id": "result", "job_kind": "capability_probe", "app_target": "api",
        "fencing_token": job["fencing_token"], "input_fingerprint": job["input_fingerprint"], "execution_status": "completed",
        "quality_result": "passed", "observed_source_manifest_fingerprint": source.fingerprint,
        "observed_platform_artifact_manifest_fingerprint": platform.fingerprint,
        "observed_test_package_digest": job["test_package_digest"], "checks": [check], "artifact_version_ids": [artifact["id"]],
        "finished_at": utc_now()}
    return service, identity, job, request


@pytest.mark.parametrize("alteration,error", [
    ("wrong_components", "observed_components_mismatch"),
    ("wrong_target", "check_target_or_plan_mismatch"),
    ("wrong_key", "execution_key_scope_mismatch"),
    ("wrong_environment", "check_environment_mismatch"),
    ("missing_entry", "missing_required_matrix_entries"),
])
async def test_exact_environment_components_and_case_mapping_are_required(paired, target, alteration, error):
    service, identity, job, request = await prepared_claim(paired, target, two_cases=alteration == "missing_entry")
    check = request["checks"][0]
    if alteration == "wrong_components":
        check["observed_components"].append({"component_id": "other-api-configuration-product", "actual_digest": canonical_digest("other-api-configuration-product")})
    elif alteration == "wrong_target":
        check["target_config_id"] = "other-api-configuration"
    elif alteration == "wrong_key":
        check["expected_execution_keys"] = check["actual_execution_keys"] = [canonical_digest("foreign-case")]
    elif alteration == "wrong_environment":
        check["environment_fingerprint"] = canonical_digest("another-host")
    outcome = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    result = await service.store.read("node_result", outcome["result_id"])
    assert result["assessment_state"] == "rejected" and error in result["errors"]
    assert result["verified_checks"] == []


@pytest.mark.parametrize("raw,error", [
    (b'<testsuite tests="1" failures="0"><testcase classname="C" name="one"><failure message="lost save"/></testcase></testsuite>', "claimed_pass_disagrees_with_raw_report"),
    (b'<testsuite tests="200" failures="0"/>', "claimed_pass_disagrees_with_raw_report"),
    (b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><testsuite>&e;</testsuite>', "raw_report_invalid:invalid_report"),
])
async def test_false_pass_counters_empty_and_hostile_reports_never_promote(paired, target, raw, error):
    service, identity, job, request = await prepared_claim(paired, target, raw)
    outcome = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    record = await service.store.read("node_result", outcome["result_id"])
    assert outcome["assessment_state"] == "rejected" and error in record["errors"]
    with pytest.raises(DomainError, match="validated frozen fixture"):
        await service.confirm_functional_capability(job["capability_id"], outcome["result_id"], "cannot-promote")


async def test_parent_input_change_invalidates_late_success(paired, target):
    service, identity, job, request = await prepared_claim(paired, target)
    run = await service.store.read("run", "run")
    await service.store.command("test-parent", "change", {}, lambda tx: tx.put("run", "run",
        {**run, "input_fingerprint": canonical_digest("new-input")}, run["revision"]))
    with pytest.raises(DomainError, match="changed before result"):
        await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    assert await service.store.list("node_result") == []


async def test_cancelled_execution_cannot_publish_a_late_pass(paired, target):
    service, identity, job, request = await prepared_claim(paired, target)
    cancelled = await service.cancel_job(job["job_id"], "owner stopped the work", "cancel")
    request["expected_revision"] = cancelled["revision"]
    outcome = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    assert outcome["assessment_state"] == "rejected"
    current = await service.store.read("node_job", job["job_id"])
    assert current["state"] == "cancelled" and current["quality_result"] == "unknown"


async def test_preflight_failure_can_truthfully_leave_inputs_unobserved(paired, target):
    service, identity, cap, boot, *_ = paired
    await service.enqueue_job("run", kind="build", target_config=target, source_manifest=source_manifest(), idempotency_key="build")
    job = (await service.claim_job(identity, JobClaimRequest(operation_id="claim", node_revision=1,
        boot_fingerprint=boot, capability_ids=[cap["id"]])))["assignment"]
    outcome = await service.submit_job_result(identity, job["job_id"], {
        "expected_revision": job["revision"], "operation_id": "blocked", "job_kind": "build", "app_target": "api",
        "fencing_token": job["fencing_token"], "input_fingerprint": job["input_fingerprint"],
        "execution_status": "failed", "quality_result": "unknown", "summary": "isolation_unverified"}, job["attempt_token"])
    assert outcome["assessment_state"] == "validated"
    assert (await service.store.read("node_job", job["job_id"]))["quality_result"] == "unknown"


async def test_same_application_configuration_accepts_only_its_own_components(paired, target):
    service, identity, job, request = await prepared_claim(paired, target)
    outcome = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    assert outcome["assessment_state"] == "validated"


async def test_overall_pass_cannot_hide_a_truthfully_reported_failed_check(paired, target):
    service, identity, job, request = await prepared_claim(paired, target,
        b'<testsuite><testcase classname="C" name="one"><failure/></testcase></testsuite>')
    request["checks"][0]["quality_result"] = "failed"
    outcome = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    record = await service.store.read("node_result", outcome["result_id"])
    assert "overall_pass_disagrees_with_raw_checks" in record["errors"]


@pytest.mark.parametrize('assertion_failed', [False, True])
async def test_partial_raw_results_are_retained_without_claiming_complete_execution(paired, target, assertion_failed):
    raw = ('<testsuite><testcase classname="C" name="one">'
           + ('<failure message="wrong select assertion"/>' if assertion_failed else '')
           + '</testcase></testsuite>').encode()
    service, identity, job, request = await prepared_claim(paired, target, raw, required_cases=['C::one', 'C::two'])
    quality = 'failed' if assertion_failed else 'unknown'
    request.update(execution_status='failed', quality_result=quality)
    request['checks'][0].update(actual_execution_keys=[], quality_result=quality, exit_code=1 if assertion_failed else 0)
    outcome = await service.submit_job_result(identity, job['job_id'], request, job['attempt_token'])
    record = await service.store.read('node_result', outcome['result_id'])
    assert record['assessment_state'] == 'validated', record['errors']
    report = record['verified_checks'][0]['normalized_report']
    assert report['missing_case_ids'] == ['C::two']
    assert report['quality_result'] == quality and report['execution_status'] == 'error'
    assert report['cases'][0]['status'] == ('failed' if assertion_failed else 'passed')
    assert record['verified_checks'][0]['actual_execution_keys'] == []
    with pytest.raises(DomainError):
        await service.confirm_functional_capability(job['capability_id'], outcome['result_id'], 'no-incomplete-promotion')


async def test_incomplete_report_cannot_forge_full_execution_coverage(paired, target):
    service, identity, job, request = await prepared_claim(paired, target, required_cases=['C::one', 'C::two'])
    request.update(execution_status='failed', quality_result='unknown')
    request['checks'][0].update(quality_result='unknown')
    outcome = await service.submit_job_result(identity, job['job_id'], request, job['attempt_token'])
    result = await service.store.read('node_result', outcome['result_id'])
    assert result['assessment_state'] == 'rejected'
    assert 'execution_key_scope_mismatch' in result['errors'] and not result['verified_checks']
