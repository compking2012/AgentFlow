import asyncio
import ipaddress
import json
import ssl

import pytest
from cryptography import x509

from agentflow.application import Application
from agentflow.common import DomainError
from agentflow.configuration import load_configuration
from agentflow.execution.pki import NodeCertificateAuthority
from agentflow.local_execution import LocalExecutionService
from agentflow.storage import Store


@pytest.fixture
async def existing_local_identity(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    await store.start()
    local = LocalExecutionService(store, data)
    saved = {"data": data, "ca": (data / "nodes/pki/ca.pem").read_bytes(),
             "ca_key": (data / "nodes/pki/ca.key").read_bytes(),
             "local": (data / "nodes/pki/local-server.pem").read_bytes(),
             "local_key": (data / "nodes/pki/local-server.key").read_bytes(),
             "server": (data / "nodes/pki/server.pem").read_bytes(),
             "server_key": (data / "nodes/pki/server.key").read_bytes()}
    await local.close()
    await store.close()
    return saved


def configuration(monkeypatch, home, data, *, host="192.168.50.10", concurrency=7):
    monkeypatch.setenv("HOME", str(home))
    path = home / ".config/agentflow/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[app]\ndata_dir = {json.dumps(str(data))}\nexecutor_host = {json.dumps(host)}\nagent_concurrency = {concurrency}\n')
    path.chmod(0o600)
    return load_configuration()


async def test_explicit_lan_configuration_reissues_only_primary_leaf_and_verifies_new_san(existing_local_identity, monkeypatch, tmp_path):
    saved = existing_local_identity
    data = saved["data"]
    config = configuration(monkeypatch, tmp_path / "home", data)
    application = await Application(config.settings, configuration=config).start(schedule=False)
    await application.close()
    current = (data / "nodes/pki/server.pem").read_bytes()
    assert current != saved["server"]
    certificate = x509.load_pem_x509_certificate(current)
    assert ipaddress.ip_address("192.168.50.10") in certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
    certificate.verify_directly_issued_by(x509.load_pem_x509_certificate(saved["ca"]))
    for key, name in [("ca", "ca.pem"), ("ca_key", "ca.key"), ("local", "local-server.pem"),
                      ("local_key", "local-server.key"), ("server_key", "server.key")]:
        assert (data / "nodes/pki" / name).read_bytes() == saved[key]
    pki = NodeCertificateAuthority(data / "nodes/pki", "192.168.50.10")

    async def reply(_reader, writer):
        writer.write(b"new gateway identity")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(reply, "127.0.0.1", 0, ssl=pki.server_context(require_client_certificate=False))
    try:
        context = ssl.create_default_context(cadata=saved["ca"].decode())
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1],
            ssl=context, server_hostname="192.168.50.10")
        assert await reader.read() == b"new gateway identity"
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_same_lan_identity_is_stable_and_other_configuration_is_preserved(existing_local_identity, monkeypatch, tmp_path):
    data = existing_local_identity["data"]
    config = configuration(monkeypatch, tmp_path / "home", data)
    original = config.config_path.read_bytes()
    first = await Application(config.settings, configuration=config).start(schedule=False)
    await first.close()
    leaf = (data / "nodes/pki/server.pem").read_bytes()
    second = await Application(config.settings, configuration=config.reload()).start(schedule=False)
    await second.close()
    assert (data / "nodes/pki/server.pem").read_bytes() == leaf
    assert config.reload().settings.agent_concurrency == 7
    assert config.config_path.read_bytes() == original


async def test_rejected_configuration_does_not_rotate_a_certificate(existing_local_identity, monkeypatch, tmp_path):
    saved = existing_local_identity
    data = saved["data"]
    with pytest.raises(DomainError, match="配置文件字段无效"):
        configuration(monkeypatch, tmp_path / "home", data, host="0.0.0.0")
    assert (data / "nodes/pki/server.pem").read_bytes() == saved["server"]
    assert (data / "nodes/pki/local-server.pem").read_bytes() == saved["local"]
