"""Pinned bootstrap and mTLS pull client; never sends an owner or model token."""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import ssl
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from agentflow.common import DomainError
from agentflow.execution.pki import create_node_key_and_csr, fingerprint
from agentflow.execution.transport import MAX_CHUNK_BYTES


def _save(path: Path, value: str) -> None:
    if path.is_symlink():
        raise DomainError("unsafe_credential_path", "Node credential cannot be a symlink", 403)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _pinned_redeem(origin: str, pairing_id: str, payload: dict, pin: str) -> dict:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or parsed.username or parsed.query or parsed.fragment:
        raise DomainError("invalid_gateway", "Pairing needs a credential-free HTTPS origin", 422)
    # The manually verified fingerprint is checked on the SAME socket before sending the single-use secret.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, context=context, timeout=15)
    try:
        connection.connect()
        if fingerprint(connection.sock.getpeercert(binary_form=True)) != pin:
            raise DomainError("controller_pin_mismatch", "TLS peer differs from owner-confirmed fingerprint", 403)
        body = json.dumps(payload).encode()
        path = f"/executor/v1/pairings/{quote(pairing_id, safe='')}/redeem"
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json",
                           "Idempotency-Key": f"redeem:{pairing_id}:{hashlib.sha256(body).hexdigest()}"})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise DomainError("response_limit", "Pairing response too large", 502)
        if response.status != 200:
            raise DomainError("pairing_failed", f"Pairing rejected with status {response.status}", response.status)
        return json.loads(raw)
    finally:
        connection.close()


async def enroll_node(directory: Path, *, origin: str, pairing_id: str, single_use_code: str,
                      controller_fingerprint: str, label: str) -> dict:
    csr, _ = create_node_key_and_csr(directory, label)
    payload = {"single_use_code": single_use_code, "csr_pem": csr,
               "controller_certificate_fingerprint": controller_fingerprint}
    response = await asyncio.to_thread(_pinned_redeem, origin, pairing_id, payload, controller_fingerprint)
    if response.get("node_audience") != "agentflow_node":
        raise DomainError("invalid_enrollment", "Wrong credential audience in enrollment", 502)
    _save(directory / "node.pem", response["node_certificate_pem"])
    _save(directory / "ca.pem", response["controller_ca_certificate_pem"])
    config = {"origin": origin.rstrip("/"), "node_id": response["node_id"],
              "node_revision": response["node_revision"], "controller_fingerprint": controller_fingerprint}
    _save(directory / "node.json", json.dumps(config))
    return config


class NodeClient:
    def __init__(self, directory: Path):
        self.directory = directory
        self.config = json.loads((directory / "node.json").read_text())
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(directory / "node.pem"), str(directory / "node.key"))
        self.client = httpx.AsyncClient(base_url=self.config["origin"], verify=context, trust_env=False,
                                         follow_redirects=False, timeout=30)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, *, payload=None, data: bytes | None = None,
                      idempotency_key: str | None = None, attempt_token: str | None = None,
                      headers: dict | None = None) -> tuple[dict, bytes]:
        if not path.startswith("/executor/v1/") or "://" in path:
            raise DomainError("forbidden_node_path", "Node client cannot invoke owner or model routes", 403)
        request_headers = dict(headers or {})
        if idempotency_key:
            request_headers["Idempotency-Key"] = (idempotency_key if len(idempotency_key) <= 128 and idempotency_key.isascii()
                                                   and idempotency_key.isprintable() else
                                                   "node:" + hashlib.sha256(idempotency_key.encode()).hexdigest())
        if attempt_token:
            request_headers["Authorization"] = f"Bearer {attempt_token}"
        async with self.client.stream(method, path, json=payload, content=data, headers=request_headers) as response:
            stream = response.extensions.get("network_stream")
            tls = stream.get_extra_info("ssl_object") if stream else None
            if not tls or fingerprint(tls.getpeercert(binary_form=True)) != self.config["controller_fingerprint"]:
                raise DomainError("controller_pin_mismatch", "Gateway TLS certificate changed; pairing must be reviewed", 403)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > max(MAX_CHUNK_BYTES + 65536, 2 * 1024 * 1024):
                    raise DomainError("response_limit", "Gateway response exceeds limit", 502)
            if response.status_code >= 400:
                try:
                    error = json.loads(raw)
                except ValueError:
                    error = {}
                error = error.get("error", error)
                raise DomainError(error.get("code", "gateway_rejected"), error.get("message", "Gateway rejected request"), response.status_code)
            return dict(response.headers), bytes(raw)

    async def json_request(self, method: str, path: str, **kwargs) -> dict:
        _, raw = await self.request(method, path, **kwargs)
        return json.loads(raw) if raw else {}

    async def claim(self, payload: dict) -> dict:
        return await self.json_request("POST", "/executor/v1/jobs/claim", payload=payload,
                                       idempotency_key=payload["operation_id"])

    async def inspect(self, job_id: str) -> dict:
        return await self.json_request("GET", f"/executor/v1/jobs/{quote(job_id, safe='')}")

    async def upload(self, assignment: dict, path: Path, digest: str) -> dict:
        job_id = assignment["job_id"]
        payload = {"name": path.name, "size": path.stat().st_size, "digest": digest}
        start = await self.json_request("POST", f"/executor/v1/jobs/{job_id}/uploads", payload=payload,
                                        attempt_token=assignment["attempt_token"], idempotency_key=f"upload:{job_id}:{digest}:{path.name}")
        upload_id = start.get("upload_id", start.get("id"))
        status = await self.json_request("GET", f"/executor/v1/uploads/{upload_id}", attempt_token=assignment["attempt_token"])
        offset = status["received_bytes"]
        if status["digest"] != digest or status["size"] != payload["size"] or not 0 <= offset <= payload["size"]:
            raise DomainError("upload_resume_mismatch", "Server upload identity differs from the local artifact", 409)
        with path.open("rb") as stream:
            stream.seek(offset)
            while block := stream.read(MAX_CHUNK_BYTES):
                chunk_digest = "sha256:" + hashlib.sha256(block).hexdigest()
                result = await self.json_request("PUT", f"/executor/v1/uploads/{upload_id}", data=block,
                    attempt_token=assignment["attempt_token"], idempotency_key=f"chunk:{upload_id}:{offset}",
                    headers={"Content-Type": "application/octet-stream",
                             "X-Chunk-Digest": chunk_digest, "Upload-Offset": str(offset)})
                offset = result["received_bytes"]
        return await self.json_request("POST", f"/executor/v1/uploads/{upload_id}/complete",
                                       payload={"job_id": job_id}, attempt_token=assignment["attempt_token"],
                                       idempotency_key=f"complete:{upload_id}")

    async def download(self, assignment: dict, artifact_id: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise DomainError("unsafe_download", "Artifact destination must not be linked", 403)
        offset = destination.stat().st_size if destination.exists() else 0
        # Always read metadata at a valid range before resuming. A complete local file must
        # not request offset == size, which is an invalid range on many HTTP servers.
        path = f"/executor/v1/jobs/{assignment['job_id']}/artifacts/{quote(artifact_id, safe='')}?offset=0&size=1"
        metadata, _ = await self.request("GET", path, attempt_token=assignment["attempt_token"])
        total, digest = int(metadata["x-artifact-size"]), metadata["x-artifact-digest"]
        if not 0 <= offset <= total <= 512 * 1024 * 1024:
            raise DomainError("invalid_download_range", "Local download size is outside the declared artifact", 502)
        with destination.open("ab") as output:
            while offset < total:
                path = f"/executor/v1/jobs/{assignment['job_id']}/artifacts/{quote(artifact_id, safe='')}?offset={offset}&size={MAX_CHUNK_BYTES}"
                headers, data = await self.request("GET", path, attempt_token=assignment["attempt_token"])
                if (int(headers["x-artifact-size"]) != total or headers["x-artifact-digest"] != digest
                        or int(headers["upload-offset"]) != offset or offset + len(data) > total):
                    raise DomainError("invalid_download_range", "Gateway returned an invalid artifact range", 502)
                if "sha256:" + hashlib.sha256(data).hexdigest() != headers["x-chunk-digest"]:
                    raise DomainError("chunk_digest_mismatch", "Download chunk was corrupted", 502)
                if not data and offset < total:
                    raise DomainError("download_stalled", "Artifact stream ended early", 502)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                offset += len(data)
        from agentflow.execution.manifests import file_digest
        if file_digest(destination) != digest:
            raise DomainError("artifact_digest_mismatch", "Downloaded artifact differs from its content digest", 502)
        return destination
