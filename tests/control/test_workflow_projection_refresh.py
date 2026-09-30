"""A refreshing document must not hide the running workflow or replay history scans."""
import asyncio
from collections import Counter

import pytest
from test_current_documents import revise_and_complete
from test_readable_workflow import complete_document
from test_workflow import flow as flow

from agentflow.common import DomainError
from agentflow.control.presentation import RunPresentationService


async def test_workflow_survives_generation_change_during_document_read(flow, monkeypatch):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    original = RunPresentationService.document
    changed = False

    async def change_before_read(self, item, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            current = await service.run_detail(run['id'])
            await service.revise(run['id'], {'expected_revision': current['revision'],
                'work_item_ids': [work['id']], 'reason': 'Concurrent rework'}, 'race-revise')
        return await original(self, item, **kwargs)

    monkeypatch.setattr(RunPresentationService, 'document', change_before_read)
    presenter = RunPresentationService(store, artifacts, service.settings)
    stage = (await presenter.workflow(run['id']))['stages'][0]
    assert stage['id'] == work['id']
    assert stage['tasks'][0]['artifacts'] == []
    assert stage['tasks'][0]['artifact_notice']
    assert stage['output'] is None and stage['artifact_notice']
    current = await store.read('work_item', work['id'])
    assert current['generation'] == work['generation'] + 1
    assert current['status'] == 'pending'
    refreshed = (await presenter.workflow(run['id']))['stages'][0]
    assert refreshed['status'] == 'pending'
    assert not refreshed.get('artifact_notice')


async def test_history_tables_are_read_once_per_view_and_refreshed_next_view(flow, monkeypatch):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    counts = Counter()
    original = store.list

    async def counted(kind):
        counts[kind] += 1
        return await original(kind)

    monkeypatch.setattr(store, 'list', counted)
    presenter = RunPresentationService(store, artifacts, service.settings)
    before = (await presenter.workflow(run['id']))['stages'][0]['output']
    assert counts['work_revision'] == 1
    assert counts['work_item'] == 1
    assert counts['artifact'] == 1
    await revise_and_complete(flow, run, work, '# Updated requirement\n\nNew acceptance boundary.')
    counts.clear()
    after = (await presenter.workflow(run['id']))['stages'][0]['output']
    assert after['digest'] != before['digest']
    assert counts['work_revision'] == 1
    assert counts['work_item'] == 1
    assert counts['artifact'] == 1


async def test_workflow_does_not_suppress_artifact_integrity_errors(flow, monkeypatch):
    service, store, artifacts, _, _ = flow
    run, *_ = await complete_document(flow)

    async def corrupted(self, item, **kwargs):
        raise DomainError('artifact_digest_mismatch', 'Invalid artifact bytes')

    monkeypatch.setattr(RunPresentationService, 'document', corrupted)
    with pytest.raises(DomainError) as error:
        await RunPresentationService(store, artifacts, service.settings).workflow(run['id'])
    assert error.value.code == 'artifact_digest_mismatch'


async def test_concurrent_views_share_work_but_next_request_reads_fresh_state(flow, monkeypatch):
    from agentflow.control.presentation import WorkflowViewRequests

    service, store, artifacts, _, _ = flow
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def build(self, identity):
        calls.append(identity)
        entered.set()
        await release.wait()
        return {'run_id': identity, 'revision': len(calls)}

    monkeypatch.setattr(RunPresentationService, 'workflow', build)
    views = WorkflowViewRequests(store, artifacts, service.settings)
    first = asyncio.create_task(views.workflow('run'))
    await entered.wait()
    second = asyncio.create_task(views.workflow('run'))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)['revision'] == 1
    assert (await views.workflow('run'))['revision'] == 2
    assert (await views.workflow('other'))['run_id'] == 'other'
    await views.close()


async def test_failed_view_is_not_reused_and_shutdown_drains_pending_views(flow, monkeypatch):
    from agentflow.control.presentation import WorkflowViewRequests

    service, store, artifacts, _, _ = flow
    entered = asyncio.Event()

    async def failed(self, identity):
        raise DomainError('artifact_digest_mismatch', identity)

    monkeypatch.setattr(RunPresentationService, 'workflow', failed)
    views = WorkflowViewRequests(store, artifacts, service.settings)
    with pytest.raises(DomainError):
        await views.workflow('run')

    async def pending(self, identity):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RunPresentationService, 'workflow', pending)
    waiter = asyncio.create_task(views.workflow('run'))
    await entered.wait()
    await views.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
