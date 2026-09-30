"""Owner maintenance migrates current document copies without starting a run."""
from types import SimpleNamespace

import httpx
from test_project_documents import seed_run
from test_workflow import flow as flow

from agentflow.control.api import create_app
from agentflow.control.product_routes import product_router
from agentflow.control.project_workflow import _document_output


async def test_document_migration_requires_owner_and_is_repeatable(flow, tmp_path):
    workflow, store, artifacts, _, _ = flow
    _, work, output = await seed_run(flow, tmp_path)
    before = await store.read('run', work['run_id'])
    app = create_app(workflow.settings, store=store, artifacts=artifacts)
    app.include_router(product_router(SimpleNamespace(store=store, workflow=workflow, settings=workflow.settings)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=workflow.settings.origin) as client:
        endpoint = '/api/v1/products/p1/documents/migrate'
        assert (await client.post(endpoint, headers={'Origin': workflow.settings.origin,
            'Idempotency-Key': 'unauthorized-migration'})).status_code == 401
        session = await client.post('/api/v1/session', json={'bootstrap_token': app.state.tokens.bootstrap_code},
            headers={'Origin': workflow.settings.origin, 'Idempotency-Key': 'session'})
        headers = {'Authorization': 'Bearer ' + session.json()['owner_token'],
                   'Origin': workflow.settings.origin, 'Idempotency-Key': 'migrate-documents'}
        first = await client.post(endpoint, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()['conflicts'] == []
        contents = {p.relative_to(output).as_posix(): p.read_bytes() for p in (output / 'documents').rglob('*.md')}
        assert list(contents) == ['documents/prd/产品需求文档 PRD.md']
        again = await client.post(endpoint, headers=headers)
        assert again.status_code == 200
        assert {p.relative_to(output).as_posix(): p.read_bytes() for p in (output / 'documents').rglob('*.md')} == contents
        missing = await client.post('/api/v1/products/missing/documents/migrate', headers=headers)
        assert missing.status_code == 404
    assert await store.read('run', work['run_id']) == before
    assert await store.list('model_invocation') == []


async def test_inherited_document_download_url_uses_real_readable_api(flow, tmp_path):
    workflow, store, artifacts, _, _ = flow
    presenter, work, _ = await seed_run(flow, tmp_path)
    doc = await presenter.document(work)
    descriptor = _document_output(await store.read('readable_artifact', doc['artifact_id']))
    app = create_app(workflow.settings, store=store, artifacts=artifacts)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=workflow.settings.origin) as client:
        session = await client.post('/api/v1/session', json={'bootstrap_token': app.state.tokens.bootstrap_code},
            headers={'Origin': workflow.settings.origin, 'Idempotency-Key': 'session'})
        response = await client.get(descriptor['download_url'], headers={
            'Authorization': 'Bearer ' + session.json()['owner_token'], 'Origin': workflow.settings.origin})
        assert response.status_code == 200, response.text
        assert response.content == await artifacts.read(doc['digest'])
        assert 'attachment' in response.headers['content-disposition']
