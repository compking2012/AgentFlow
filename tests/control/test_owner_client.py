"""Owner CLI transport behavior using an HTTP transport fixture, no network."""
import httpx
import pytest

from agentflow.common import DomainError
from agentflow.control.owner_client import OwnerClient


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_transport_failures_are_private_explicitly_uncertain_and_never_automatically_resent(failure):
    client = OwnerClient('http://127.0.0.1:8317', 'private-owner-fixture')
    await client.client.aclose()
    calls = []
    async def transport(request):
        calls.append(request)
        raise failure('diagnostic contains private-api-key-fixture', request=request)
    client.client = httpx.AsyncClient(base_url=client.origin, transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(DomainError) as raised:
            await client.request('POST', '/api/v1/product_setup/models', {'api_key': 'private-api-key-fixture'}, key='one-intent')
        assert raised.value.code == 'owner_connection_interrupted'
        assert '可能已被接受' in raised.value.message
        assert 'private' not in str(raised.value)
        assert len(calls) == 1 and calls[0].headers['Idempotency-Key'] == 'one-intent'
    finally:
        await client.close()


async def test_launch_has_extended_timeout_and_response_must_be_readable_json():
    client = OwnerClient('http://127.0.0.1:8317', 'owner-fixture')
    await client.client.aclose()
    calls = []
    async def transport(request):
        calls.append(request)
        return httpx.Response(200, content=b'incomplete response')
    client.client = httpx.AsyncClient(base_url=client.origin, transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(DomainError) as raised:
            await client.request('POST', '/api/v1/products/product/launch', {})
        assert raised.value.code == 'owner_response_invalid'
        assert calls[0].extensions['timeout']['read'] == 300
        assert calls[0].extensions['timeout']['connect'] == 10
    finally:
        await client.close()


@pytest.mark.parametrize('path', ['https://other.invalid/api/v1/products', '/api/v1/../../health',
                                 '/api/v1/\\other.invalid', '/api/v1/products#fragment'])
async def test_invalid_owner_paths_never_send_credentials(path):
    client = OwnerClient('http://127.0.0.1:8317', 'owner-fixture')
    try:
        with pytest.raises(DomainError) as raised:
            await client.request('GET', path)
        assert raised.value.code == 'invalid_owner_path'
    finally:
        await client.close()


@pytest.mark.parametrize('always_fail', [False, True])
async def test_read_only_status_polling_reconnects_but_is_bounded(always_fail):
    client = OwnerClient('http://127.0.0.1:8317', 'owner-fixture')
    await client.client.aclose()
    calls = []
    async def transport(request):
        calls.append(request)
        if always_fail or len(calls) == 1:
            raise httpx.ReadError('idle connection closed', request=request)
        return httpx.Response(200, json={'state': 'running'})
    client.client = httpx.AsyncClient(base_url=client.origin, transport=httpx.MockTransport(transport))
    try:
        if always_fail:
            with pytest.raises(DomainError, match='连接中断'):
                await client.request('GET', '/api/v1/products/product')
            assert len(calls) == 3
        else:
            assert await client.request('GET', '/api/v1/products/product') == {'state': 'running'}
            assert len(calls) == 2
        assert all(request.method == 'GET' for request in calls)
    finally:
        await client.close()
