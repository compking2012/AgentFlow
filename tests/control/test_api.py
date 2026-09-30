import uuid

import httpx
import pytest
import pytest_asyncio

from agentflow.control.api import create_app
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest_asyncio.fixture
async def api(tmp_path):
    settings = Settings(data_dir=tmp_path / "control")
    store = Store(settings.data_dir)
    await store.start()
    app = create_app(settings, store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=settings.origin) as client:
        response = await client.post("/api/v1/session", json={"bootstrap_token": app.state.tokens.bootstrap_code},
            headers={"Origin": settings.origin, "Idempotency-Key": "bootstrap"})
        assert response.status_code == 201, response.text
        token = response.json()["owner_token"]
        headers = {"Authorization": "Bearer " + token, "Origin": settings.origin, "Idempotency-Key": str(uuid.uuid4())}
        yield app, client, headers, tmp_path
    await store.close()


async def test_management_requires_explicit_owner_token_and_never_sets_cookies(api):
    app, client, headers, _ = api
    response = await client.get("/api/v1/projects", headers=headers)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    for forged in [{}, {"Cookie": "owner_token=anything"}, {"Authorization": "Bearer invalid"}]:
        assert (await client.get("/api/v1/projects", headers=forged)).status_code == 401
    attempt = app.state.tokens.issue("agentflow_attempt", {"llm:chat"}, "attempt", 60)
    assert (await client.get("/api/v1/projects", headers={"Authorization": "Bearer " + attempt})).status_code == 403


@pytest.mark.parametrize("bad_headers", [{"Host": "evil.example"}, {"Origin": "http://localhost:3000"}, {"Origin": "null"}])
async def test_dns_rebinding_and_project_preview_origins_are_rejected(api, bad_headers):
    _, client, headers, _ = api
    assert (await client.get("/api/v1/projects", headers={**headers, **bad_headers})).status_code == 403


async def test_idempotent_repository_creation_and_clean_import(api):
    _, client, headers, tmp = api
    payload = {"name": "Tickets", "local_path": str(tmp / "tickets"), "import_mode": "initialize_managed",
               "dirty_worktree_policy": "require_clean"}
    response = await client.post("/api/v1/projects", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    replay = await client.post("/api/v1/projects", json=payload, headers=headers)
    assert replay.json() == response.json()
    conflict = await client.post("/api/v1/projects", json={**payload, "name": "Other"}, headers=headers)
    assert conflict.status_code == 409
    assert (tmp / "tickets/.git").is_dir()
    assert not (tmp / "tickets/.agentflow-init").exists()


async def test_invalid_payloads_do_not_write_state(api):
    app, client, headers, _ = api
    before = await app.state.store.list("project")
    response = await client.post("/api/v1/projects", json={"name": "Missing required fields"}, headers=headers)
    assert response.status_code == 422
    assert await app.state.store.list("project") == before
    no_origin = {"Authorization": headers["Authorization"], "Idempotency-Key": "x"}
    assert (await client.post("/api/v1/projects", json={}, headers=no_origin)).status_code == 403


async def test_public_plan_contract_accepts_zero_request_limit_but_rejects_negative_and_boolean(api):
    from test_workflow import plan_payload

    app, client, headers, tmp = api
    response = await client.post('/api/v1/projects', json={'name': 'Unlimited plan', 'local_path': str(tmp / 'unlimited'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, headers=headers)
    assert response.status_code == 201
    payload = plan_payload(response.json())
    payload['runtime_bindings']['role_model_profile_id'] = str(uuid.uuid4())
    payload['budget_limit']['max_model_requests'] = 0
    response = await client.post('/api/v1/run_plans', json=payload,
        headers={**headers, 'Idempotency-Key': 'zero-plan'})
    assert response.status_code == 201, response.text
    assert response.json()['budget_limit']['max_model_requests'] == 0
    before = await app.state.store.list('plan')
    for invalid in [-1, False, 0.0, '0']:
        payload['budget_limit']['max_model_requests'] = invalid
        response = await client.post('/api/v1/run_plans', json=payload,
            headers={**headers, 'Idempotency-Key': f'bad-plan-{invalid}'})
        assert response.status_code == 422
    assert await app.state.store.list('plan') == before


async def test_executor_endpoints_are_not_exposed_on_owner_listener(api):
    _, client, headers, _ = api
    response = await client.post("/executor/v1/jobs/claim", json={}, headers=headers)
    assert response.status_code == 404


async def test_logout_revokes_existing_owner_access(api):
    _, client, headers, _ = api
    assert (await client.delete("/api/v1/session", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/projects", headers=headers)).status_code == 401


async def test_browser_continuation_cannot_control_products_and_needs_exact_owner_origin(api):
    app, client, headers, _ = api
    code = app.state.tokens.new_bootstrap()
    response = await client.post('/api/v1/session', json={'bootstrap_token': code, 'browser_session': True},
        headers={**headers, 'Idempotency-Key': 'browser-open'})
    assert response.status_code == 201 and 'set-cookie' not in response.headers
    owner, ticket = response.json()['owner_token'], response.json()['browser_session_token']
    assert owner != ticket
    ticket_headers = {**headers, 'Authorization': 'Bearer ' + ticket}
    assert (await client.get('/api/v1/products', headers=ticket_headers)).status_code == 403
    before = await app.state.store.list('run')
    for changed in [{**ticket_headers, 'Origin': 'http://127.0.0.1:9999'},
                    {key: value for key, value in ticket_headers.items() if key != 'Origin'},
                    {**ticket_headers, 'Authorization': 'Bearer ' + owner}]:
        assert (await client.post('/api/v1/session/resume', json={}, headers=changed)).status_code == 403
    response = await client.post('/api/v1/session/resume', json={}, headers=ticket_headers)
    assert response.status_code == 200 and 'set-cookie' not in response.headers
    current = {**headers, 'Authorization': 'Bearer ' + response.json()['owner_token']}
    assert (await client.get('/api/v1/meta', headers=current)).status_code == 200
    assert await app.state.store.list('run') == before
    assert (await client.delete('/api/v1/session', headers=current)).status_code == 204
    assert (await client.post('/api/v1/session/resume', json={}, headers=ticket_headers)).status_code == 401


async def test_oversized_and_malformed_requests_fail_before_domain_commands(api):
    _, client, headers, _ = api
    response = await client.post("/api/v1/projects", content=b"x" * (2 * 1024 * 1024 + 1), headers=headers)
    assert response.status_code == 413
    response = await client.post("/api/v1/projects", content=b"{not json", headers=headers)
    assert response.status_code == 422


async def test_task_trace_is_owner_only_and_scoped_to_its_work(api):
    from agentflow.runtime.trace import ExecutionTrace
    app, client, headers, _ = api
    def seed(tx):
        tx.put('work_item', 'trace-work', {'run_id': 'trace-run', 'attempt_id': 'trace-attempt'})
        tx.put('attempt', 'trace-attempt', {'run_id': 'trace-run', 'work_item_id': 'trace-work',
            'generation': 1, 'status': 'running', 'started_at': '2026-09-23T00:00:00Z'})
        return {}
    await app.state.store.command('fixture', 'trace-seed', {}, seed)
    await ExecutionTrace(app.state.store).emit('trace-attempt', 'llm_output', '模型返回', '<script>plain text</script>')
    path = '/api/v1/attempts/trace-attempt/trace'
    assert (await client.get(path)).status_code == 401
    response = await client.get(path, headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['items'][0]['content'] == '<script>plain text</script>'
    attempts = '/api/v1/runs/trace-run/work_items/trace-work/attempts'
    assert (await client.get(attempts, headers=headers)).json()['current_attempt_id'] == 'trace-attempt'
    assert (await client.get(attempts.replace('trace-run', 'wrong-run'), headers=headers)).status_code == 404
    assert (await client.get(path + '?after=1&before=2', headers=headers)).status_code == 422
