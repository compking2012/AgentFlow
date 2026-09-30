"""Measured cache behavior with temporary artifacts; no model or user data access."""
import asyncio
import json
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentflow.common import DomainError
from agentflow.control import stage_context
from agentflow.control.stage_context import StageContext
from agentflow.storage import LocalArtifactStore, Store


class CountedArtifacts(LocalArtifactStore):
    def __init__(self, root):
        super().__init__(root)
        self.counts = Counter()
        self.count_lock = threading.Lock()

    def _verify(self, digest):
        with self.count_lock:
            self.counts['verify'] += 1
        return super()._verify(digest)

    def _read(self, digest):
        with self.count_lock:
            self.counts['read'] += 1
        return super()._read(digest)

    def _verify_stream(self, stream, digest, output=None):
        size = super()._verify_stream(stream, digest, output)
        with self.count_lock:
            self.counts['verified_streams'] += 1
            self.counts['verified_bytes'] += size
        return size


@pytest.fixture
async def system(tmp_path, monkeypatch):
    store = Store(tmp_path / 'controller')
    await store.start()
    artifacts = CountedArtifacts(store.data_dir / 'artifacts')
    normalizations = Counter()
    original = stage_context.document_payload

    def normalize(raw):
        normalizations['calls'] += 1
        return original(raw)

    monkeypatch.setattr(stage_context, 'document_payload', normalize)
    try:
        yield SimpleNamespace(store=store, artifacts=artifacts,
            context=StageContext(store, artifacts, store.data_dir), normalizations=normalizations)
    finally:
        await store.close()


async def document(env, identity='source', raw=b'{"content":"Shared evidence"}', **fields):
    blob = await env.artifacts.put_bytes(raw)
    body = {'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json',
            'step': 'architecture', 'generation': 1, **fields}
    artifact = await env.store.command('fixture.document', identity, {},
        lambda tx: tx.put('artifact', identity, body))
    return artifact, blob


async def build(env, identity='source', *, consumer='implementation', parent_step='architecture', plan=None):
    parent = {'id': identity, 'step': parent_step, 'generation': 1,
              'dependencies': [], 'artifact_ids': [identity]}
    child = {'id': consumer, 'step': 'implementation', 'generation': 1,
             'dependencies': [identity] if plan is None else [], 'artifact_ids': []}
    return await env.context.build({'id': 'run'}, child, plan or {}, {identity: parent, consumer: child})


async def patch_artifact(env, identity, **fields):
    def write(tx):
        current = tx.get('artifact', identity)
        return tx.put('artifact', identity, {**current, **fields}, current['revision'])
    await env.store.command('fixture.patch', str(uuid4()), {}, write)


async def test_repeated_build_verifies_each_time_but_reads_and_normalizes_once(system):
    env = system
    raw = json.dumps({'title': 'Evidence', 'content': '内容' * 15000}).encode()
    await document(env, raw=raw)
    results = [await build(env) for _ in range(6)]
    assert all(result == results[0] for result in results)
    assert env.artifacts.counts == {'verify': 6, 'read': 1,
        'verified_streams': 7, 'verified_bytes': 7 * len(raw)}
    assert env.normalizations['calls'] == 1
    path = results[0]['directory'] / results[0]['documents'][0]['file']
    assert json.loads(path.read_bytes()) == json.loads(raw)
    assert path.stat().st_mode & 0o222 == 0


async def test_simultaneous_cache_misses_share_one_read_without_sharing_mutable_metadata(system, monkeypatch):
    env = system
    await document(env)
    original_read, original_verify = env.artifacts.read, env.artifacts.verify
    reading, release, all_verified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    verified = 0
    builders = 8

    async def hold_read(digest):
        reading.set()
        await release.wait()
        return await original_read(digest)

    async def verify(digest):
        nonlocal verified
        result = await original_verify(digest)
        verified += 1
        if verified == builders:
            all_verified.set()
        return result

    monkeypatch.setattr(env.artifacts, 'read', hold_read)
    monkeypatch.setattr(env.artifacts, 'verify', verify)
    tasks = [asyncio.create_task(build(env)) for _ in range(builders)]
    try:
        await asyncio.wait_for(reading.wait(), 10)
        await asyncio.wait_for(all_verified.wait(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert env.artifacts.counts['verify'] == builders
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 1
    assert all(result == results[0] for result in results)
    results[0]['documents'][0]['title'] = 'Caller mutation'
    assert all(result['documents'][0]['title'] != 'Caller mutation' for result in results[1:])


@pytest.mark.parametrize('target', ['artifact', 'context'])
async def test_warm_cache_still_rejects_tampering(system, target):
    env = system
    _, blob = await document(env)
    result = await build(env)
    path = Path(blob['path']) if target == 'artifact' else result['directory'] / result['documents'][0]['file']
    original_size = path.stat().st_size
    path.chmod(0o600)
    path.write_bytes(b'x' * original_size)
    with pytest.raises(DomainError) as error:
        await build(env)
    assert error.value.code == ('artifact_corrupt' if target == 'artifact' else 'context_modified')
    assert env.artifacts.counts['verify'] == 2
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 1


async def test_shared_bytes_keep_each_artifact_title_role_reuse_and_directory_identity(system):
    env = system
    first, _ = await document(env, 'first', name='Architecture.md')
    second, _ = await document(env, 'second', name='Research.md', step='research')
    env.artifacts.counts.clear()  # Duplicate publication intentionally verifies the existing object.
    a = await build(env, 'first', consumer='consumer-a')
    b = await build(env, 'second', consumer='consumer-b', plan={'reused_inputs': ['second']})
    first_entry, second_entry = a['documents'][0], b['documents'][0]
    assert first_entry == {**first_entry, 'artifact_id': first['id'], 'title': 'Architecture.md',
                           'step': 'architecture', 'reused': False}
    assert second_entry == {**second_entry, 'artifact_id': second['id'], 'title': 'Research.md',
                            'step': 'research', 'reused': True}
    assert first_entry['file'] == second_entry['file'] and a['directory'] != b['directory']
    assert env.artifacts.counts['verify'] == 2
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 1


@pytest.mark.parametrize('change', [{'stale': True}, {'generation': 2}])
async def test_warm_cache_cannot_bypass_current_artifact_version(system, change):
    env = system
    await document(env)
    await build(env)
    await patch_artifact(env, 'source', **change)
    with pytest.raises(DomainError) as error:
        await build(env)
    assert error.value.code == 'stale_input'
    assert env.artifacts.counts['verify'] == env.artifacts.counts['read'] == 1
    assert env.normalizations['calls'] == 1


@pytest.mark.parametrize('limit_kind', ['raw', 'normalized'])
async def test_warm_cache_cannot_bypass_reduced_document_size_limits(system, limit_kind):
    env = system
    raw = b'{"content":"valid bounded data"}' if limit_kind == 'raw' else b'\x01' * 30
    await document(env, raw=raw)
    result = await build(env)
    normalized = (result['directory'] / result['documents'][0]['file']).read_bytes()
    env.context.MAX_DOCUMENT_BYTES = len(raw) - 1 if limit_kind == 'raw' else len(raw) + 1
    assert len(normalized) > env.context.MAX_DOCUMENT_BYTES
    with pytest.raises(DomainError) as error:
        await build(env)
    assert error.value.code == 'context_requires_partition'
    expected = 1 if limit_kind == 'raw' else 2
    assert env.artifacts.counts['verify'] == 2
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == expected


async def test_changed_normalization_version_does_not_reuse_the_prior_transform(system):
    env = system
    await document(env)
    first = await build(env)
    env.context.NORMALIZATION_VERSION += 1
    assert await build(env) == first
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 2


@pytest.mark.parametrize('capacity', ['entries', 'bytes'])
async def test_lru_evicts_the_least_recent_document_with_bounded_storage(system, capacity):
    env = system
    samples = {name: json.dumps({'content': name * 100}, separators=(',', ':')).encode() for name in 'abc'}
    size = len(samples['a'])
    for name, raw in samples.items():
        await document(env, name, raw=raw)
    env.context.CACHE_MAX_DOCUMENTS = 2 if capacity == 'entries' else 128
    env.context.CACHE_MAX_BYTES = 16 * 1024 * 1024 if capacity == 'entries' else 2 * size + size // 2
    for name in 'abacab':  # Touch a; c must evict b, and b must subsequently be re-read.
        result = await build(env, name)
        data = (result['directory'] / result['documents'][0]['file']).read_bytes()
        assert data == samples[name]
        assert len(env.context._documents) <= env.context.CACHE_MAX_DOCUMENTS
        assert env.context._document_bytes <= env.context.CACHE_MAX_BYTES
    assert env.artifacts.counts['verify'] == 6
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 4


async def test_document_larger_than_cache_budget_remains_readable_without_being_cached(system):
    env = system
    raw = b'{"content":"too large for this cache only"}'
    await document(env, raw=raw)
    env.context.CACHE_MAX_BYTES = len(raw) - 1
    assert await build(env) == await build(env)
    assert env.artifacts.counts['verify'] == env.artifacts.counts['read'] == 2
    assert env.normalizations['calls'] == 2
    assert not env.context._documents and env.context._document_bytes == 0


async def test_cancelled_cache_miss_releases_waiters_and_allows_a_new_builder(system, monkeypatch):
    env = system
    await document(env)
    original_read, original_verify = env.artifacts.read, env.artifacts.verify
    first_read, second_verified = asyncio.Event(), asyncio.Event()
    calls = verified = 0

    async def read(digest):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_read.set()
            await asyncio.Event().wait()
        return await original_read(digest)

    async def verify(digest):
        nonlocal verified
        result = await original_verify(digest)
        verified += 1
        if verified == 2:
            second_verified.set()
        return result

    monkeypatch.setattr(env.artifacts, 'read', read)
    monkeypatch.setattr(env.artifacts, 'verify', verify)
    tasks = [asyncio.create_task(build(env))]
    try:
        await asyncio.wait_for(first_read.wait(), 10)
        tasks.append(asyncio.create_task(build(env)))
        await asyncio.wait_for(second_verified.wait(), 10)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        result = await asyncio.wait_for(tasks[1], 10)
        assert await build(env) == result
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert env.artifacts.counts['verify'] == 3
    assert env.artifacts.counts['read'] == env.normalizations['calls'] == 1
