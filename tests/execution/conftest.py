
import pytest

from agentflow.common import canonical_digest, utc_now
from agentflow.execution.models import CapabilityReport, DisplayObservation, TargetConfig
from agentflow.execution.pki import create_node_key_and_csr
from agentflow.execution.service import NodeService
from agentflow.storage import Store


@pytest.fixture
async def store(tmp_path):
    store = Store(tmp_path / "controller")
    await store.start()
    yield store
    await store.close()


@pytest.fixture
def target():
    return TargetConfig(app_target="api", os_name="Linux", os_version_constraint="*", cpu_architecture="*",
                        required_display_protocol="not_required", required_device_mode="not_required")


@pytest.fixture
async def paired(store, tmp_path, target):
    service = NodeService(store, tmp_path / "controller", "https://127.0.0.1:8443")
    await store.command("test-run", "create", {}, lambda tx: tx.put("run", "run", {
        "run_id": "run", "execution_state": "running", "input_fingerprint": canonical_digest("run-input")}))
    keys = tmp_path / "node-keys"
    csr, key_fp = create_node_key_and_csr(keys, "test node")
    pair = await service.create_pairing({"node_label": "test node", "expected_node_public_key_fingerprint": key_fp,
                                         "allowed_app_targets": ["api"], "expires_in_seconds": 300}, "pair")
    redeemed = await service.redeem_pairing(pair["pairing"]["id"], {
        "single_use_code": pair["single_use_code"], "csr_pem": csr,
        "controller_certificate_fingerprint": service.pki.controller_fingerprint}, "redeem")
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(redeemed["node_certificate_pem"].encode())
    identity = await service.authenticate_peer_certificate(cert.public_bytes(serialization.Encoding.DER))
    boot = canonical_digest({"boot": 1})
    await service.heartbeat(identity, {"expected_revision": 1, "heartbeat_sequence": 1,
                                       "boot_fingerprint": boot, "active_job_ids": []}, "hb1")
    # Synthetic static report tests protocol authorization, not real platform support.
    report = CapabilityReport(app_target="api", target_config_fingerprint=target.fingerprint,
                              os_name="Linux", os_version="test", architecture="test", boot_fingerprint=boot,
                              display=DisplayObservation(session_fingerprint=boot), tools=[],
                              state="static_verified", observed_at=utc_now())
    cap = await service.register_capability(identity.node_id, report, "cap")
    return service, identity, cap, boot, keys, redeemed, pair, csr


def source_manifest():
    from agentflow.execution.manifests import SourceManifest
    return SourceManifest(source_commit="a" * 40, source_tree_oid="b" * 40,
                          source_bundle_artifact_version_id="source", source_bundle_digest=canonical_digest("source"),
                          test_package_artifact_version_id="tests", test_package_digest=canonical_digest("tests"),
                          build_plan_artifact_version_id="plan", build_plan_digest=canonical_digest("plan"),
                          target_matrix_fingerprint=canonical_digest("matrix"), required_app_targets=("api",))
