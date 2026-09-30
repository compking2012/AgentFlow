import asyncio
import socket
import ssl
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from agentflow.control.node_routes import create_executor_app
from agentflow.control.tls import PeerCertificateH11Protocol
from agentflow.execution.pki import create_node_key_and_csr
from agentflow.execution.service import NodeService
from agentflow.storage import Store


class TestServer(uvicorn.Server):
    __test__ = False

    @contextmanager
    def capture_signals(self):
        yield


@pytest.mark.asyncio
async def test_real_tls_peer_identity_and_listener_separation(tmp_path):
    store = Store(tmp_path / "control")
    await store.start()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    service = NodeService(store, tmp_path / "control", origin)
    app = create_executor_app(service)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", proxy_headers=False,
        http=PeerCertificateH11Protocol, ssl_certfile=str(service.pki.server_cert_path),
        ssl_keyfile=str(service.pki.server_key_path), ssl_ca_certs=str(service.pki.directory / "ca.pem"),
        ssl_cert_reqs=ssl.CERT_OPTIONAL, lifespan="off")
    server = TestServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.02)
        assert server.started
        context = ssl.create_default_context(cafile=str(service.pki.directory / "ca.pem"))
        async with httpx.AsyncClient(base_url=origin, verify=context, trust_env=False) as client:
            denied = await client.post("/executor/v1/jobs/claim", json={}, headers={
                "Idempotency-Key": "forged", "X-Client-Cert": "forged", "X-Node-Id": "forged"})
            assert denied.status_code == 401
            assert (await client.get("/api/v1/projects")).status_code == 404
            csr, fingerprint = create_node_key_and_csr(tmp_path / "node", "test-node")
            pairing = await service.create_pairing({"node_label": "test-node", "location": "user_lan_host",
                "expected_node_public_key_fingerprint": fingerprint, "allowed_app_targets": ["api"],
                "expires_in_seconds": 120}, "pair")
            response = await client.post(f"/executor/v1/pairings/{pairing['pairing']['id']}/redeem", json={
                "single_use_code": pairing["single_use_code"], "csr_pem": csr,
                "controller_certificate_fingerprint": service.pki.controller_fingerprint},
                headers={"Idempotency-Key": "redeem"})
            assert response.status_code == 200, response.text
            result = response.json()
        certificate = tmp_path / "node/client.pem"
        certificate.write_text(result["node_certificate_pem"])
        context.load_cert_chain(str(certificate), str(tmp_path / "node/node.key"))
        async with httpx.AsyncClient(base_url=origin, verify=context, trust_env=False) as client:
            # Transport authentication succeeds; the domain rejects an unowned job.
            response = await client.get("/executor/v1/jobs/absent")
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "job_forbidden"
            response = await client.get("/executor/v1/jobs/absent", headers={"Origin": "https://example.org"})
            assert response.status_code == 403
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
        await store.close()
