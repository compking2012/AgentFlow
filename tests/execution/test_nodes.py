import asyncio
import hashlib
import ssl
from datetime import UTC, datetime, timedelta

import pytest
from conftest import source_manifest

from agentflow.common import DomainError, canonical_digest
from agentflow.execution.models import JobClaimRequest
from agentflow.execution.pki import create_node_key_and_csr, sign_receipt
from agentflow.execution.transport import MAX_CHUNK_BYTES, ArtifactTransport, safe_extract_tar


async def claim(paired, target, *, key="claim", resources=()):
    service, identity, cap, boot, *_ = paired
    return await service.claim_job(identity, JobClaimRequest(operation_id=key, node_revision=1,
        boot_fingerprint=boot, capability_ids=[cap["id"]], available_resource_ids=list(resources)))


async def test_pairing_is_single_use_and_pinned_to_csr(paired, tmp_path):
    service, identity, _, _, _, _, pair, csr = paired
    with pytest.raises(DomainError) as error:
        await service.redeem_pairing(pair["pairing"]["id"], {"single_use_code": pair["single_use_code"],
            "csr_pem": csr, "controller_certificate_fingerprint": service.pki.controller_fingerprint}, "different-request")
    assert error.value.code == "pairing_unavailable"
    other_csr, other_fp = create_node_key_and_csr(tmp_path / "other", "other")
    next_pair = await service.create_pairing({"node_label": "bound", "expected_node_public_key_fingerprint": other_fp,
        "allowed_app_targets": ["api"]}, "next")
    with pytest.raises(DomainError, match="differs"):
        await service.redeem_pairing(next_pair["pairing"]["id"], {"single_use_code": next_pair["single_use_code"],
            "csr_pem": csr, "controller_certificate_fingerprint": service.pki.controller_fingerprint}, "wrong-csr")
    assert other_csr != csr
    await service.revoke_node(identity.node_id, 1, "test revocation", "revoke")
    with pytest.raises(DomainError):
        await service.get_job(identity, "unknown")


async def test_real_mutual_tls_requires_node_certificate(paired, tmp_path):
    service, identity, _, _, keys, redeemed, *_ = paired
    server_context = service.pki.server_context(require_client_certificate=True)
    received = []

    async def handle(reader, writer):
        tls = writer.get_extra_info("ssl_object")
        who = await service.authenticate_peer_certificate(tls.getpeercert(binary_form=True))
        received.append(who.node_id)
        await reader.read(4)
        writer.write(b"node-only")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    (keys / "node.pem").write_text(redeemed["node_certificate_pem"])
    context = ssl.create_default_context(cadata=service.pki.ca_pem)
    context.load_cert_chain(str(keys / "node.pem"), str(keys / "node.key"))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read() == b"node-only"
        writer.close()
        await writer.wait_closed()
        assert received == [identity.node_id]
        no_certificate = ssl.create_default_context(cadata=service.pki.ca_pem)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=no_certificate)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.read() == b""
            writer.close()
        except (ssl.SSLError, ConnectionResetError):
            pass
        assert received == [identity.node_id]
    finally:
        server.close()
        await server.wait_closed()


async def test_claim_idempotency_and_resource_exclusion(paired, target):
    service, identity, *_ = paired
    r = await service.register_resource(identity.node_id, "workspace", canonical_digest("disk"), "resource")
    jobs = [await service.enqueue_job("run", kind="build", target_config=target, source_manifest=source_manifest(),
        required_resource_ids=[r["id"]], idempotency_key=str(i)) for i in range(2)]
    first = await claim(paired, target, resources=[r["id"]])
    again = await claim(paired, target, resources=[r["id"]])
    assert first == again
    assert first["assignment"]["job_id"] in {j["id"] for j in jobs}
    assert (await claim(paired, target, key="second", resources=[r["id"]]))["assignment"] is None
    await service.cancel_job(first["assignment"]["job_id"], "cancel", "cancel")
    assert (await service.store.read("node_resource", r["id"]))["state"] == "quarantined"


async def test_expired_resource_stays_quarantined_until_signed_cleanup(paired, target):
    service, identity, _, _, keys, *_ = paired
    resource = await service.register_resource(identity.node_id, "desktop_session", canonical_digest("desktop"), "r")
    await service.enqueue_job("run", kind="build", target_config=target, source_manifest=source_manifest(),
                              required_resource_ids=[resource["id"]], idempotency_key="job")
    job = (await claim(paired, target, resources=[resource["id"]]))["assignment"]
    row = await service.store.read("node_job", job["job_id"])
    old = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    await service.store.command("test-expiry", "expire", {}, lambda tx: tx.put("node_job", row["id"],
        {**row, "lease_expires_at": old}, row["revision"]))
    assert await service.reconcile_expired_leases() == [job["job_id"]]
    assert (await service.store.read("node_resource", resource["id"]))["state"] == "quarantined"
    lease = job["resource_leases"][0]
    payload = {"node_id": identity.node_id, "job_id": job["job_id"], "resource_id": resource["id"],
               "lease_id": lease["lease_id"], "fencing_token": lease["fencing_token"],
               "alive_process_count": 0, "method": "process_stop_verified"}
    with pytest.raises(DomainError):
        await service.submit_cleanup_receipt(identity, payload, "forged", "forged")
    await service.submit_cleanup_receipt(identity, payload, sign_receipt(keys / "node.key", payload), "real")
    assert (await service.store.read("node_resource", resource["id"]))["state"] == "available"
    resolved = await service.store.read("node_job", job["job_id"])
    assert resolved["state"] == "failed" and resolved["quality_result"] == "unknown"


async def test_chunk_transfer_checks_identity_offsets_and_final_hash(store, tmp_path):
    transport = ArtifactTransport(store, tmp_path / "transport")
    content = b"native-report\n" * 500
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    upload = await transport.begin("node", "job", digest, len(content), "report.xml", "start")
    uid = upload["id"]
    with pytest.raises(DomainError):
        await transport.append(uid, "intruder", "job", 0, content, digest, "bad-actor")
    with pytest.raises(DomainError):
        await transport.append(uid, "node", "job", 3, content, digest, "bad-offset")
    saved = await transport.append(uid, "node", "job", 0, content, digest, "chunk")
    assert saved == await transport.append(uid, "node", "job", 0, content, digest, "chunk")
    final = await transport.complete(uid, "node", "job", "complete")
    assert final == await transport.complete(uid, "node", "job", "complete")
    chunk = await transport.read_chunk(final["id"], [final["id"]], 5, 20)
    assert chunk["data"] == content[5:25]
    with pytest.raises(DomainError):
        await transport.read_chunk(final["id"], [], 0, 1)
    with pytest.raises(DomainError):
        await transport.read_chunk(final["id"], [final["id"]], 0, MAX_CHUNK_BYTES + 1)


def test_archive_rejects_traversal_links_and_expansion(tmp_path):
    import io
    import tarfile
    for name, kind in [("../escape", None), ("link", tarfile.SYMTYPE)]:
        tar = tmp_path / "bad.tar"
        with tarfile.open(tar, "w") as output:
            info = tarfile.TarInfo(name)
            info.size = 1
            if kind:
                info.type, info.linkname = kind, "/etc/passwd"
                output.addfile(info)
            else:
                output.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(DomainError):
            safe_extract_tar(tar, tmp_path / "unpack")
    assert not (tmp_path / "escape").exists()


async def test_build_cannot_reference_future_platform_manifest(paired, target):
    service, *_ = paired
    with pytest.raises(DomainError):
        await service.enqueue_job("run", kind="build", target_config=target, source_manifest=source_manifest(),
                                  platform_manifest={"state": "frozen"}, idempotency_key="cyclic")
    with pytest.raises(DomainError):
        await service.enqueue_job("run", kind="test", target_config=target, source_manifest=source_manifest(),
                                  idempotency_key="unfrozen")
