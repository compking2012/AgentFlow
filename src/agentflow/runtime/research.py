"""Task-authorized public GET broker; workers retain controller-only network access."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import zlib
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request

from agentflow.common import DomainError, utc_now

_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
_IDENTITY = ('attempt_id', 'run_id', 'iteration_id', 'fencing_token', 'input_fingerprint')


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast:
        return False
    # Transition/tunnel forms must not conceal a private IPv4 destination.
    if isinstance(address, ipaddress.IPv6Address):
        if (address.sixtofour is not None or address.teredo is not None or address.scope_id is not None
                or address in ipaddress.ip_network('::ffff:0:0:0/96')):
            return False
        if address.ipv4_mapped is not None:
            return _public_address(str(address.ipv4_mapped))
        if address in ipaddress.ip_network('64:ff9b::/96'):
            return _public_address(str(ipaddress.IPv4Address(int(address) & 0xffffffff)))
    return True


def _url(value: str, hosts: list[str]):
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192
                or any(ord(c) < 33 or ord(c) == 127 for c in value) or '\\' in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None:
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if '%' in host:
            raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if port not in {80, 443}:
            raise ValueError
        authority = f'[{host}]' if ':' in host else host
        if parsed.port:
            authority += f':{port}'
        normalized = urlunsplit((parsed.scheme, authority, parsed.path or '/', parsed.query, ''))
        httpx.URL(normalized)  # Reject a malformed HTTP authority before DNS.
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise DomainError('forbidden_url', 'Only public HTTP(S) URLs on ports 80/443 are supported', 403) from None
    if hosts and host not in {h.lower().rstrip('.') for h in hosts}:
        raise DomainError('network_not_authorized', 'Source is outside the configured research hosts', 403)
    return normalized, host, port, authority


async def _resolve(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            answers = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise DomainError('source_unavailable', 'Research source DNS lookup failed', 422) from None
        addresses = sorted({answer[4][0] for answer in answers})
    if not addresses or not all(_public_address(address) for address in addresses):
        raise DomainError('forbidden_url', 'Private, loopback and special addresses are forbidden', 403)
    return addresses


class ResearchBroker:
    def __init__(self, models, settings):
        self.models = models
        self.store = models.store
        self.settings = settings

    async def _authorize(self, token: str):
        context = await self.models.authorize_attempt(token, 'chat_completions')
        record = await self.store.read('dispatch_context', context.attempt_id)
        task = record.get('task', {}) if record else {}
        if (not self.settings.research_public_web_enabled or task.get('role') != 'research'
                or task.get('allow_public_web') is not True
                or any(task.get(key) != getattr(context, key) for key in _IDENTITY)):
            raise DomainError('network_not_authorized', 'Public research access is not enabled for this task', 403)
        return context, task

    async def fetch(self, token: str, url: str) -> dict:
        context, task = await self._authorize(token)
        initial, _, _, _ = _url(url, task.get('allowed_web_hosts', []))
        # A durable per-attempt ceiling also protects against concurrent/direct
        # endpoint use outside the SDK's aggregate tool-call counter.
        def reserve(tx):
            previous = tx.get('research_usage', context.attempt_id)
            count = previous['requests'] if previous else 0
            if count >= min(context.max_tool_calls, task.get('max_tool_calls', 0)):
                raise DomainError('tool_limit_exceeded', 'Research request quota exhausted', 429)
            return tx.put('research_usage', context.attempt_id, {'requests': count + 1},
                          expected_revision=previous['revision'] if previous else None)
        await self.store.command('research.reserve', str(uuid4()), {'attempt_id': context.attempt_id}, reserve)
        try:
            async with asyncio.timeout(30):
                return await self._read(token, context, task, initial)
        except (TimeoutError, httpx.HTTPError):
            raise DomainError('source_unavailable', 'Public research source could not be read within its limits', 422) from None

    async def _read(self, token, context, task, initial):
        current, redirects = initial, []
        for hop in range(_MAX_REDIRECTS + 1):
            current, host, port, authority = _url(current, task.get('allowed_web_hosts', []))
            addresses = await _resolve(host, port)
            # DNS and prior responses yield: recheck revocation/fence immediately
            # before sending, and never accept caller-supplied network authority.
            latest, fresh_task = await self._authorize(token)
            if (any(getattr(latest, key) != getattr(context, key) for key in _IDENTITY)
                    or fresh_task != task):
                raise DomainError('stale_task_token', 'Research authority changed before HTTP send', 403)
            address = addresses[0]
            destination = f'[{address}]' if ':' in address else address
            parts = urlsplit(current)
            target = urlunsplit((parts.scheme, f'{destination}:{port}', parts.path, parts.query, ''))
            # Fresh client for every hop: no cookies, ambient proxy, auth headers,
            # connection reuse or DNS re-resolution between validation and connect.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as client:
                async with client.stream('GET', target,
                        headers={'Host': authority, 'User-Agent': 'AgentFlow-PublicResearchReader',
                                 'Accept-Encoding': 'identity'},
                        extensions={'sni_hostname': host}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get('location')
                        if not location or hop == _MAX_REDIRECTS:
                            raise DomainError('source_unavailable', 'Research source exceeded redirect limits', 422)
                        redirects.append(current)
                        try:
                            if any(ord(c) < 33 or ord(c) == 127 for c in location):
                                raise ValueError
                            current = urljoin(current, location)
                        except ValueError:
                            raise DomainError('forbidden_url', 'Research redirect is not a valid public URL', 403) from None
                        continue
                    if not 200 <= response.status_code < 300:
                        raise DomainError('source_unavailable', 'Research source did not return a successful response', 422)
                    # HTTPX's decoded iterator can inflate an entire compressed
                    # chunk before our check. Keep raw and decoded input bounded;
                    # zlib's max_length caps allocation before it returns output.
                    encoding = response.headers.get('content-encoding', '').strip().lower()
                    if encoding not in {'', 'identity', 'gzip'}:
                        raise DomainError('source_encoding_unsupported',
                                          'Research source encoding must be identity or gzip', 422)
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
                    body = bytearray()
                    raw_size = 0
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        raw_size += len(chunk)
                        if raw_size > _MAX_SOURCE_BYTES:
                            raise DomainError('source_too_large', 'Research source exceeds size limit', 422)
                        if decoder is not None:
                            try:
                                chunk = decoder.decompress(chunk, _MAX_SOURCE_BYTES - len(body) + 1)
                            except zlib.error:
                                raise DomainError('source_encoding_invalid', 'Research gzip source is invalid', 422) from None
                            if decoder.unused_data:
                                raise DomainError('source_encoding_invalid', 'Research gzip source has trailing data', 422)
                        if len(body) + len(chunk) > _MAX_SOURCE_BYTES:
                            raise DomainError('source_too_large', 'Research source exceeds size limit', 422)
                        body.extend(chunk)
                    if decoder is not None and (not decoder.eof or decoder.unconsumed_tail):
                        raise DomainError('source_encoding_invalid', 'Research gzip source is incomplete', 422)
            return {'url': initial, 'final_url': current, 'redirects': redirects, 'accessed_at': utc_now(),
                    'digest': 'sha256:' + hashlib.sha256(body).hexdigest(),
                    'content': bytes(body).decode('utf-8', errors='replace')}
        raise AssertionError('Unreachable redirect limit')


def create_research_router(models, settings):
    router = APIRouter(prefix='/internal/v1/research', tags=['research'])
    broker = ResearchBroker(models, settings)

    @router.post('/fetch')
    async def fetch(request: Request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 8192:
            raise DomainError('unauthorized', 'A scoped task credential is required', 401)
        token = authorization[7:]
        await broker._authorize(token)
        raw = await request.body()
        try:
            payload = json.loads(raw) if len(raw) <= 16384 else None
            if not isinstance(payload, dict) or set(payload) != {'url'} or not isinstance(payload['url'], str):
                raise ValueError
        except (ValueError, UnicodeError):
            raise DomainError('invalid_request', 'Research accepts only one bounded URL', 422) from None
        return await broker.fetch(token, payload['url'])

    return router
