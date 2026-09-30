"""Research authorization and public-network boundaries; no live models or owner state."""
import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from agentflow.application import Application
from agentflow.control.api import create_app
from agentflow.models.profiles import AttemptContext, ModelProfile
from agentflow.models.service import ModelService
from agentflow.settings import Settings


@pytest_asyncio.fixture
async def research_app(tmp_path, task):
    application = Application(Settings(data_dir=tmp_path / 'controller'))
    await application.store.start()
    # Any accidental model credential lookup fails the test.
    def forbidden_secret(_):
        raise AssertionError('Research must not resolve model credentials')
    models = ModelService(application.store, application.settings.data_dir,
                          application.authorize_attempt, forbidden_secret)
    application.models = models
    await models.registry.register(ModelProfile(model_profile_id='research-model', provider='openai_compatible',
        requested_model='fixture', accepted_api_model='fixture', acceptance_status='accepted',
        base_url='https://models.example.invalid', protocols=['chat_completions'],
        credential_reference='local:never-read'), 'register')
    context = AttemptContext(attempt_id=task.attempt_id, run_id=task.run_id, iteration_id=task.iteration_id,
        model_profile_id='research-model', fencing_token=1, input_fingerprint=task.input_fingerprint,
        expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(), max_model_requests=5,
        max_output_tokens=1024, max_tool_calls=20, protocols=['chat_completions'])
    token = 'scoped-research-fixture-token'
    frozen = task.model_dump(mode='json') | {'role': 'research', 'allow_public_web': True,
        'allowed_web_hosts': [], 'max_tool_calls': 20}
    def initial(tx):
        tx.put('task_authorization', hashlib.sha256(token.encode()).hexdigest(), context.model_dump())
        tx.put('attempt', task.attempt_id, {'work_item_id': task.work_item_id})
        tx.put('work_item', task.work_item_id, {'status': 'running', 'attempt_id': task.attempt_id,
            'fencing_token': 1, 'input_fingerprint': task.input_fingerprint})
        tx.put('run', task.run_id, {'execution_state': 'running'})
        tx.put('dispatch_context', task.attempt_id, {'task': frozen})
        return {}
    await application.store.command('fixture', 'initial', {}, initial)
    app = create_app(application.settings, store=application.store, models=models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=application.settings.origin, trust_env=False) as client:
        async def fetch(url='https://new-source.example/article', **changes):
            return await client.post('/internal/v1/research/fetch', json={'url': url, **changes},
                headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': str(uuid4())})
        async def update(kind, identity, **changes):
            def write(tx):
                current = tx.get(kind, identity)
                tx.put(kind, identity, {**current, **changes}, expected_revision=current['revision'])
                return {}
            await application.store.command('fixture-update', str(uuid4()), {}, write)
        yield application, client, fetch, update, frozen, token
    await models.close()
    await application.store.close()


async def test_unknown_attempt_cannot_reach_research_network(research_app):
    _, client, _, _, _, _ = research_app
    result = await client.post('/internal/v1/research/fetch', json={'url': 'https://example.com'},
        headers={'Authorization': 'Bearer invented', 'Idempotency-Key': str(uuid4())})
    assert result.status_code == 401


async def test_explicit_disable_denies_even_a_preapproved_host(research_app):
    application, _, fetch, update, frozen, _ = research_app
    await update('dispatch_context', frozen['attempt_id'], task=frozen | {
        'allow_public_web': False, 'allowed_web_hosts': ['new-source.example']})
    result = await fetch()
    assert result.status_code == 403
    assert result.json()['error']['code'] == 'network_not_authorized'
    assert await application.store.list('research_usage') == []


async def test_nonresearch_role_cannot_reuse_model_token_for_research(research_app):
    _, _, fetch, update, frozen, _ = research_app
    await update('dispatch_context', frozen['attempt_id'], task=frozen | {'role': 'review'})
    assert (await fetch()).status_code == 403


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/', 'http://[::1]/', 'http://10.0.0.5/', 'http://169.254.169.254/',
    'http://100.64.0.1/', 'http://[fc00::1]/', 'http://[ff02::1]/', 'http://224.0.0.1/',
    'http://[::ffff:127.0.0.1]/', 'http://[2002:7f00:1::]/',
    'file:///etc/passwd', 'https://user:secret@example.com/', 'http://example.com:8787/',
    'https://example.com:bad/', 'https://[bad', 'https://example.com/\r\nheader',
])
async def test_private_special_and_malformed_urls_are_rejected(research_app, url):
    _, _, fetch, _, _, _ = research_app
    response = await fetch(url)
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'forbidden_url'


async def test_revoked_attempt_cannot_fetch(research_app):
    _, _, fetch, update, frozen, _ = research_app
    await update('work_item', frozen['work_item_id'], status='cancelled')
    response = await fetch()
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'stale_task_token'


@pytest.fixture
def upstream(monkeypatch):
    import socket
    observed = []
    def dns(host, port, **kwargs):
        assert port in {80, 443}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]
    async def send(_transport, request):
        observed.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'Public source evidence'), request=request)
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    return observed


async def test_unlisted_public_source_is_pinned_and_credentials_never_forwarded(research_app, upstream, monkeypatch):
    _, _, fetch, _, _, token = research_app
    monkeypatch.setenv('HTTPS_PROXY', 'http://owner:private@127.0.0.1:9')
    result = await fetch()
    assert result.status_code == 200
    evidence = result.json()
    assert evidence['content'] == 'Public source evidence'
    assert evidence['url'] == evidence['final_url'] == 'https://new-source.example/article'
    assert evidence['digest'] == 'sha256:' + hashlib.sha256(b'Public source evidence').hexdigest()
    request = upstream[0]
    assert str(request.url) == 'https://93.184.216.34/article'
    assert request.headers['host'] == 'new-source.example'
    assert request.extensions['sni_hostname'] == 'new-source.example'
    assert request.method == 'GET'
    assert 'authorization' not in request.headers and 'cookie' not in request.headers
    assert token not in str(request.headers)


async def test_allowlist_restricts_optional_policy(research_app, upstream):
    _, _, fetch, update, frozen, _ = research_app
    await update('dispatch_context', frozen['attempt_id'], task=frozen | {'allowed_web_hosts': ['allowed.example']})
    assert (await fetch()).status_code == 403
    assert upstream == []


async def test_mixed_public_private_dns_is_rejected_before_connection(research_app, upstream, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    _, _, fetch, _, _, _ = research_app
    assert (await fetch()).status_code == 403
    assert upstream == []


async def test_redirect_revalidates_dns_and_never_carries_cookie(research_app, upstream, monkeypatch):
    async def send(_transport, request):
        upstream.append(request)
        if len(upstream) == 1:
            return httpx.Response(302, headers={'Location': 'https://second.example/final',
                'Set-Cookie': 'tracking=secret; Domain=.example'}, request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b'Final evidence'), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 200
    assert response.json()['final_url'] == 'https://second.example/final'
    assert response.json()['redirects'] == ['https://new-source.example/article']
    assert len(upstream) == 2
    assert upstream[1].headers['host'] == 'second.example'
    assert upstream[1].extensions['sni_hostname'] == 'second.example'
    assert 'cookie' not in upstream[1].headers


async def test_redirect_to_controller_is_blocked(research_app, upstream, monkeypatch):
    async def send(_transport, request):
        upstream.append(request)
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/'}, request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    assert (await fetch()).status_code == 403
    assert len(upstream) == 1


async def test_dns_rebinding_on_same_host_redirect_is_blocked(research_app, upstream, monkeypatch):
    import socket
    answers = iter(['93.184.216.34', '127.0.0.1'])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (next(answers), 443))])
    async def send(_transport, request):
        upstream.append(request)
        return httpx.Response(302, headers={'Location': '/next'}, request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    assert (await fetch()).status_code == 403
    assert len(upstream) == 1


async def test_revocation_while_dns_is_pending_blocks_outbound_request(research_app, upstream, monkeypatch):
    import socket
    import threading
    begun, release = threading.Event(), threading.Event()
    def dns(*a, **k):
        begun.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    _, _, fetch, update, frozen, _ = research_app
    pending = asyncio.create_task(fetch())
    try:
        assert await asyncio.to_thread(begun.wait, 5)
        await update('work_item', frozen['work_item_id'], status='cancelled')
    finally:
        release.set()
    response = await pending
    assert response.status_code == 403
    assert upstream == []


async def test_network_quota_is_atomic_for_concurrent_reads(research_app, upstream):
    _, _, fetch, update, frozen, _ = research_app
    await update('dispatch_context', frozen['attempt_id'], task=frozen | {'max_tool_calls': 1})
    results = await asyncio.gather(fetch(), fetch())
    assert sorted(r.status_code for r in results) == [200, 429]
    assert len(upstream) == 1


async def test_oversized_source_is_not_returned_as_evidence(research_app, upstream, monkeypatch):
    async def send(_transport, request):
        return httpx.Response(200, stream=httpx.ByteStream(b'x' * (2 * 1024 * 1024 + 1)), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'source_too_large'


async def test_repeated_redirects_stop_at_bound(research_app, upstream, monkeypatch):
    async def send(_transport, request):
        upstream.append(request)
        return httpx.Response(302, headers={'Location': '/again'}, request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    assert (await fetch()).status_code == 422
    assert len(upstream) == 6


def test_worker_uses_controller_broker_and_saves_full_source(task, http_fixture):
    import json

    from agentflow.adapters.openhands.tools import ToolBroker
    raw = 'Public evidence ' * 3000
    evidence = {'url': 'https://unlisted.example/', 'final_url': 'https://unlisted.example/',
        'redirects': [], 'accessed_at': '2026-09-26T00:00:00Z', 'content': raw,
        'digest': 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()}
    with http_fixture(lambda *_: (200, {'Content-Type': 'application/json'}, json.dumps(evidence).encode())) as (base, calls):
        broker = ToolBroker(task.model_copy(update={'role': 'research', 'allow_public_web': True,
            'proxy_base_url': base, 'allowed_web_hosts': []}))
        result = broker.fetch_url('https://unlisted.example/')
    assert len(result['content']) == 24000
    assert json.loads((task.artifact_dir / result['source_artifact']).read_text())['content'] == raw
    assert calls[0]['path'] == '/internal/v1/research/fetch'
    assert calls[0]['authorization'] == 'Bearer scoped-fixture-token'
    assert calls[0]['body'] == {'url': 'https://unlisted.example/'}


async def test_research_sandbox_has_no_direct_public_destinations(task, tmp_path):
    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    public_task = task.model_copy(update={'role': 'research', 'allow_public_web': True,
        'allowed_web_hosts': [], 'resolved_web_hosts': {'example.com': ['93.184.216.34']}})
    assert await sandbox.resolve_web_hosts(public_task) == {}
    assert await sandbox.resolve_web_hosts(public_task.model_copy(update={
        'allowed_web_hosts': ['example.com']})) == {}


async def test_real_research_sandbox_allows_broker_but_denies_direct_network_and_controller_state(
        task, tmp_path, http_fixture):
    import json
    import platform
    import sys
    from pathlib import Path

    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    if platform.system() != 'Darwin':
        pytest.skip('Real Seatbelt boundary requires macOS')
    source = Path(__file__).resolve().parents[2] / 'src'
    protected = tmp_path / 'protected'
    protected.mkdir()
    secret = protected / 'controller-secret'
    secret.write_text('must remain private')
    evidence = {'url': 'https://new.example/', 'final_url': 'https://new.example/', 'redirects': [],
        'accessed_at': '2026-09-26T00:00:00Z', 'digest': 'sha256:' + hashlib.sha256(b'real broker').hexdigest(),
        'content': 'real broker'}
    with http_fixture(lambda *_: (200, {'Content-Type': 'application/json'}, json.dumps(evidence).encode())) as (base, calls):
        public_task = task.model_copy(update={'role': 'research', 'allow_public_web': True,
            'proxy_base_url': base, 'allowed_read_roots': [source], 'protected_roots': [protected]})
        sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
        prefix, proof = await sandbox.prepare(public_task, Path(sys.executable).resolve(), tmp_path / 'private')
        assert proof['verified']
        profile = Path(prefix[-1])
        body = public_task.model_dump(mode='json') | {'proxy_token': 'scoped-fixture-token'}
        script = f'import sys,site;site.addsitedir({str(Path(sys.prefix) / 'lib/python3.12/site-packages')!r});sys.path.insert(0,{str(source)!r})\n'
        script += 'from agentflow.runtime.contracts import TaskEnvelope\nfrom agentflow.adapters.openhands.tools import ToolBroker\n'
        script += f't=TaskEnvelope.model_validate({body!r})\n'
        script += "assert ToolBroker(t).fetch_url('https://new.example/')['content']=='real broker'"
        assert await sandbox._run_probe(profile, script) == 0
        assert len(calls) == 1
        assert await sandbox._run_probe(profile, sandbox._denial_probe(
            "import socket\nsocket.create_connection(('93.184.216.34',80),timeout=2)")) == 77
        assert await sandbox._run_probe(profile, sandbox._denial_probe(
            f'open({str(secret)!r}).read()')) == 77


async def test_source_error_and_dns_failure_return_no_evidence(research_app, upstream, monkeypatch):
    import socket
    _, _, fetch, _, _, _ = research_app
    async def unavailable(_transport, request):
        return httpx.Response(503, content=b'not evidence', request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', unavailable)
    response = await fetch()
    assert response.status_code == 422 and 'content' not in response.json()
    def no_dns(*args, **kwargs):
        raise socket.gaierror('private OS diagnostics')
    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    response = await fetch()
    assert response.status_code == 422 and 'private OS diagnostics' not in response.text


async def test_malformed_redirect_is_a_controlled_source_failure(research_app, upstream, monkeypatch):
    async def invalid_redirect(_transport, request):
        return httpx.Response(302, headers={'Location': 'https://[bad'}, request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', invalid_redirect)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'forbidden_url'


async def test_broker_accepts_no_method_headers_or_post_body_from_role(research_app, upstream):
    _, _, fetch, _, _, _ = research_app
    response = await fetch(method='POST', headers={'Authorization': 'secret'})
    assert response.status_code == 422
    assert upstream == []


async def test_nat64_cannot_translate_to_private_network(research_app, upstream):
    _, _, fetch, _, _, _ = research_app
    response = await fetch('http://[64:ff9b::7f00:1]/')
    assert response.status_code == 403
    assert upstream == []


async def test_explicit_zero_port_is_not_reinterpreted_as_a_standard_port(research_app, upstream):
    _, _, fetch, _, _, _ = research_app
    response = await fetch('https://new-source.example:0/')
    assert response.status_code == 403
    assert upstream == []


async def test_real_tls_reader_validates_original_hostname_on_pinned_ip(research_app, tmp_path, monkeypatch):
    """Only DNS/socket routing is redirected to a fixture; HTTP and TLS are real."""
    import socket
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from httpcore._backends.anyio import AnyIOBackend

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'new-source.example')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('new-source.example')]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    requests, sni, connections = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(200)
            if self.path == '/compressed':
                import gzip
                self.send_header('Content-Encoding', 'gzip')
                body = gzip.compress(b'<html>Actual gzip evidence</html>')
            else:
                body = b'Actual TLS evidence'
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    context.set_servername_callback(lambda _socket, hostname, _context: sni.append(hostname))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_context, original_dns, original_connect = ssl.create_default_context, socket.getaddrinfo, AnyIOBackend.connect_tcp
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: original_context(cafile=str(cert_file)))
    def dns(host, port, **kwargs):
        if host in {'new-source.example', 'wrong-source.example'}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]
        return original_dns(host, port, **kwargs)
    async def connect(self, host, port, **kwargs):
        connections.append((host, port))
        assert (host, port) == ('93.184.216.34', 443)
        return await original_connect(self, '127.0.0.1', server.server_port, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(AnyIOBackend, 'connect_tcp', connect)
    try:
        _, _, fetch, _, _, token = research_app
        response = await fetch()
        assert response.status_code == 200, response.text
        assert response.json()['content'] == 'Actual TLS evidence'
        assert connections == [('93.184.216.34', 443)]
        assert sni == ['new-source.example']
        assert requests[0][0] == '/article'
        assert requests[0][1]['Host'] == 'new-source.example'
        assert 'Authorization' not in requests[0][1] and token not in str(requests)
        compressed = await fetch('https://new-source.example/compressed')
        assert compressed.status_code == 200
        assert compressed.json()['content'] == '<html>Actual gzip evidence</html>'
        assert requests[-1][1]['Accept-Encoding'] == 'identity'
        rejected = await fetch('https://wrong-source.example/article')
        assert rejected.status_code == 422
        assert len(requests) == 2, 'A certificate for the wrong hostname must fail before GET'
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(2)


async def test_compressed_bomb_never_allocates_more_than_the_bounded_output(research_app, upstream, monkeypatch):
    import gzip
    import zlib

    compressed = gzip.compress(b'x' * (8 * 1024 * 1024))
    assert len(compressed) < 9000
    class Bomb(httpx.AsyncByteStream):
        consumed = False
        async def __aiter__(self):
            self.consumed = True
            yield compressed
    stream = Bomb()
    outputs = []
    original = zlib.decompressobj
    class MeasuredDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = original(*args, **kwargs)
        def decompress(self, data, max_length=0):
            result = self.decoder.decompress(data, max_length)
            outputs.append(len(result))
            return result
        def __getattr__(self, name):
            return getattr(self.decoder, name)
    monkeypatch.setattr(zlib, 'decompressobj', MeasuredDecoder)
    async def send(_transport, request):
        return httpx.Response(200, headers={'Content-Encoding': 'gzip', 'Content-Type': 'text/html'},
                              stream=stream, request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'source_too_large'
    assert stream.consumed
    assert outputs and max(outputs) <= 2 * 1024 * 1024 + 1
    assert sum(outputs) == 2 * 1024 * 1024 + 1


@pytest.mark.parametrize('encoding', [None, 'identity'])
async def test_identity_encoding_preserves_public_text_and_requests_no_compression(
        research_app, upstream, monkeypatch, encoding):
    content = '<html><body>公开来源 evidence</body></html>'.encode()
    async def send(_transport, request):
        upstream.append(request)
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        if encoding:
            headers['Content-Encoding'] = encoding
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(content), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 200
    assert response.json()['content'] == content.decode()
    assert response.json()['digest'] == 'sha256:' + hashlib.sha256(content).hexdigest()
    assert upstream[0].headers['Accept-Encoding'] == 'identity'


@pytest.mark.parametrize('translated', ['::ffff:0:127.0.0.1', '::ffff:0:10.0.0.1', '::ffff:0:93.184.216.34'])
async def test_ipv4_translated_prefix_is_not_public_research_egress(research_app, upstream, translated):
    _, _, fetch, _, _, _ = research_app
    response = await fetch(f'http://[{translated}]/')
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'forbidden_url'
    assert upstream == []


async def test_supported_gzip_source_returns_decoded_evidence(research_app, upstream, monkeypatch):
    import gzip
    content = '<html>公开来源：正常 gzip 页面</html>'.encode()
    async def send(_transport, request):
        return httpx.Response(200, headers={'Content-Encoding': 'gzip', 'Content-Type': 'text/html'},
            stream=httpx.ByteStream(gzip.compress(content)), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 200
    assert response.json()['content'] == content.decode()
    assert response.json()['digest'] == 'sha256:' + hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize('corruption', ['truncated', 'trailing', 'concatenated', 'invalid'])
async def test_gzip_integrity_and_single_stream_boundary_are_required(research_app, upstream, monkeypatch, corruption):
    import gzip
    compressed = gzip.compress(b'Public evidence')
    compressed = {'truncated': compressed[:-4], 'trailing': compressed + b'trailing',
        'concatenated': compressed + compressed, 'invalid': b'not gzip'}[corruption]
    async def send(_transport, request):
        return httpx.Response(200, headers={'Content-Encoding': 'gzip'},
            stream=httpx.ByteStream(compressed), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'source_encoding_invalid'
    assert 'content' not in response.json()


async def test_unsupported_content_encoding_is_rejected_without_reading(research_app, upstream, monkeypatch):
    class Unreadable(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('Unsupported encoding must be rejected before reading')
            yield b''
    async def send(_transport, request):
        return httpx.Response(200, headers={'Content-Encoding': 'br'}, stream=Unreadable(), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'source_encoding_unsupported'


async def test_gzip_raw_input_limit_applies_even_when_decoded_output_is_small(research_app, upstream, monkeypatch):
    import gzip
    compressed = gzip.compress(b'abc')
    # A legal gzip comment consumes raw bytes while producing no decoded output.
    oversized = compressed[:3] + b'\x10' + compressed[4:10] + b'c' * (2 * 1024 * 1024 + 1) + b'\0' + compressed[10:]
    async def send(_transport, request):
        return httpx.Response(200, headers={'Content-Encoding': 'gzip'},
            stream=httpx.ByteStream(oversized), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    _, _, fetch, _, _, _ = research_app
    response = await fetch()
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'source_too_large'
