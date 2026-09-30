"""Actual local TLS, bootstrap, transfer, build and frozen unit test evidence."""
import asyncio
import io
import json
import platform
import socket
import ssl
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from agentflow.common import DomainError, canonical_digest
from agentflow.control.node_routes import create_executor_app
from agentflow.control.tls import PeerCertificateH11Protocol
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    freeze_platform_manifest,
)
from agentflow.execution.models import TargetConfig
from agentflow.execution.pki import create_node_key_and_csr
from agentflow.execution.service import NodeService
from agentflow.testing.adapters import BuildRecipe
from node_agent.client import enroll_node
from node_agent.daemon import NodeDaemon


class LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


@pytest.fixture
async def gateway(store, tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    service = NodeService(store, tmp_path / "gateway", origin)
    server = LocalServer(uvicorn.Config(create_executor_app(service), log_level="error", proxy_headers=False,
        http=PeerCertificateH11Protocol, ssl_certfile=str(service.pki.server_cert_path),
        ssl_keyfile=str(service.pki.server_key_path), ssl_ca_certs=str(service.pki.directory / "ca.pem"),
        ssl_cert_reqs=ssl.CERT_OPTIONAL, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        yield service
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_real_pinned_bootstrap_chunk_resume_and_frozen_functional_probe(gateway, tmp_path, monkeypatch):
    service = gateway
    directory = tmp_path / "node"
    _, fingerprint = create_node_key_and_csr(directory, "real-node")
    pair = await service.create_pairing({"node_label": "real-node", "expected_node_public_key_fingerprint": fingerprint,
                                       "allowed_app_targets": ["api"]}, "pair-real")
    await enroll_node(directory, origin=service.executor_origin, pairing_id=pair["pairing"]["id"],
                      single_use_code=pair["single_use_code"], controller_fingerprint=service.pki.controller_fingerprint,
                      label="real-node")
    target = TargetConfig(target_config_id="node-test-api", app_target="api", os_name=platform.system(),
        os_version_constraint="*", cpu_architecture=platform.machine(), required_display_protocol="not_required",
        required_device_mode="not_required")
    daemon = NodeDaemon(directory, [target], trusted_project_mode=True)
    try:
        reports = await daemon.initialize()
        assert reports[0]["state"] == "static_verified", reports
        resource = await service.register_resource(daemon.client.config["node_id"], "workspace", canonical_digest("workspace"), "workspace")
        daemon.resource_ids.append(resource["id"])
        await service.store.command("test-run", "real-run", {}, lambda tx: tx.put("run", "real-run", {
            "execution_state": "running", "input_fingerprint": canonical_digest("run")}))
        framework_id = "test::stored value survives reopen"
        entry = MatrixPlanEntry(matrix_entry_id="case", test_case_id="persist", app_target="api",
            component_roles=["product", "test"], target_config_id=target.target_config_id, target_config_revision=1)
        plan = MatrixPlan(required_app_targets=["api"], target_configs=[target], entries=[entry])
        product = "import{writeFileSync,readFileSync}from'node:fs';export function save(p,v){writeFileSync(p,v)};export function read(p){return readFileSync(p,'utf8')}"
        tests = "import{test}from'node:test';import assert from'node:assert/strict';import{mkdtempSync,rmSync}from'node:fs';import{tmpdir}from'node:os';import{save,read}from'../product/index.mjs';test('stored value survives reopen',()=>{const p=mkdtempSync(tmpdir()+'/agentflow-real-');try{save(p+'/data','frozen');assert.equal(read(p+'/data'),'frozen')}finally{rmSync(p,{recursive:true})}})"
        build_script = "import{mkdirSync,writeFileSync}from'node:fs';mkdirSync('bundle/product',{recursive:true});mkdirSync('bundle/tests',{recursive:true});"
        build_script += f"writeFileSync('bundle/product/index.mjs',{json.dumps(product)});writeFileSync('bundle/tests/unit.test.mjs',{json.dumps(tests)});"
        files = {"package.json": json.dumps({"name": "real-node-fixture", "version": "1.0.0", "scripts": {"build": "node build.mjs"}}),
                 "package-lock.json": json.dumps({"name": "real-node-fixture", "version": "1.0.0", "lockfileVersion": 3,
                    "packages": {"": {"name": "real-node-fixture", "version": "1.0.0"}}}), "build.mjs": build_script}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, value in files.items():
                info = tarfile.TarInfo(name)
                content = value.encode()
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        bundle = await service.import_input(buffer.getvalue(), name="source.tar", run_id="real-run")
        build_plan = await service.import_input(b"{}", name="build-plan.json", run_id="real-run")
        source = SourceManifest(source_commit="a" * 40, source_tree_oid="b" * 40,
            source_bundle_artifact_version_id=bundle["artifact_version_id"], source_bundle_digest=bundle["digest"],
            test_package_artifact_version_id=bundle["artifact_version_id"], test_package_digest=bundle["digest"],
            build_plan_artifact_version_id=build_plan["artifact_version_id"], build_plan_digest=build_plan["digest"],
            target_matrix_fingerprint=plan.fingerprint, required_app_targets=("api",))
        build = await service.enqueue_job("real-run", kind="build", target_config=target, source_manifest=source,
            recipe=BuildRecipe(adapter="api", output_paths={"product": "bundle/product", "test": "bundle/tests"}),
            required_resource_ids=[resource["id"]], idempotency_key="build")
        request = daemon.client.json_request
        lost_ack = False

        async def lose_first_upload_ack(method, path, **kwargs):
            nonlocal lost_ack
            result = await request(method, path, **kwargs)
            if path.endswith("/complete") and not lost_ack:
                lost_ack = True
                raise ConnectionError("completed upload response lost")
            return result

        monkeypatch.setattr(daemon.client, "json_request", lose_first_upload_ack)
        with pytest.raises(ConnectionError, match="upload response lost"):
            await daemon.run_once()
        local_build = daemon.journal.get(build['id'])['result']
        local_packages = [Path(item['path']) for item in local_build['built_artifacts']]
        assert all(path.is_file() for path in local_packages)
        assert any((directory / 'inputs').iterdir())
        completed = await daemon.run_once()
        assert completed["quality_result"] == "passed", completed
        record = await service.store.read("node_result", completed["receipt"]["result_id"])
        assert record["assessment_state"] == "validated", record
        assert len(record["verified_build_artifacts"]) == 2
        assert not completed["cleanup_required"]
        assert (await service.store.read("node_resource", resource["id"]))["state"] == "available"
        assert all(not path.exists() for path in local_packages)
        assert not any((directory / 'inputs').iterdir())
        assert all(Path(item['path']).is_file() for item in local_build['artifact_files']
                   if item['format'] != 'build_artifact')
        # The same completed input is downloaded again through a valid metadata range.
        assignment = daemon.journal.get(build["id"])["assignment"]
        local = tmp_path / "resumed-source.tar"
        local.write_bytes(buffer.getvalue())
        # Download authorization is job-scoped: the completed build is deliberately rejected.
        with pytest.raises(DomainError, match="no longer accepts"):
            await daemon.client.download(assignment, bundle["artifact_version_id"], local)
        artifacts = [BuildArtifact(artifact_id=item["component_id"], artifact_version_id=item["artifact_version_id"],
            app_target="api", target_config_id=target.target_config_id, component_role=item["component_role"],
            kind="product" if item["kind"] == "application" else "test", digest=item["digest"],
            source_manifest_fingerprint=source.fingerprint, toolchain_fingerprint=item["environment_fingerprint"],
            verified_upload=True, metadata={"relative_path": item["relative_path"], "content_digest": item["content_digest"]})
            for item in record["verified_build_artifacts"]]
        frozen = freeze_platform_manifest(source, plan, artifacts)
        recipe = BuildRecipe(adapter="api", test_project_path="bundle/tests", test_kind="unit",
            unit_project="bundle/tests/unit.test.mjs", expected_case_ids=[framework_id])
        mapping = [{"matrix_entry_id": "case", "test_case_id": "persist", "framework_case_ids": [framework_id]}]
        probe = await service.enqueue_functional_probe("real-run", capability_id=daemon.capability_ids[0],
            target_config=target, source_manifest=source, platform_manifest=frozen, recipe=recipe, matrix_entries=mapping,
            matrix_plan_fingerprint=plan.fingerprint, matrix_binding_fingerprint=canonical_digest("binding"), idempotency_key="functional")
        outcome = await daemon.run_once()
        assert outcome["quality_result"] == "passed", outcome
        assert outcome["receipt"]["assessment_state"] == "validated", outcome
        await service.confirm_functional_capability(daemon.capability_ids[0], outcome["receipt"]["result_id"], "promote-real")
        observed = await service.store.read("node_result", outcome["receipt"]["result_id"])
        assert len(observed["verified_checks"][0]["normalized_report"]["cases"]) == 1
        assert (await service.store.read("node_job", probe["id"]))["quality_result"] == "passed"
        await service.enqueue_job("real-run", kind="test", target_config=target, source_manifest=source,
            platform_manifest=frozen, recipe=recipe, matrix_entries=mapping, matrix_entry_ids=["case"],
            matrix_plan_fingerprint=plan.fingerprint, matrix_binding_fingerprint=canonical_digest("binding"), idempotency_key="formal")
        formal = await daemon.run_once()
        assert formal["quality_result"] == "passed" and formal["receipt"]["assessment_state"] == "validated", formal
    finally:
        await daemon.close()
