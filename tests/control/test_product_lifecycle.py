"""Real Git/Store/Workflow planning; no models, builds or passing quality records are injected.

The local execution readiness fixture supplies target configuration only. These
tests prove planning and registration boundaries, not build/test capability.
"""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import test_products

from agentflow.control.product_diagnostics import diagnose_project
from agentflow.control.product_lifecycle import ProductLifecycle
from agentflow.control.product_models import ProductRequest
from agentflow.execution.models import TargetConfig, ToolRequirement

product_api = test_products.product_api
configure = test_products.configure
STARTER = Path(__file__).resolve().parents[2] / 'src/agentflow/resources/web_api_starter'


async def repository(env, directory, *, foundation=True):
    if foundation:
        shutil.copytree(STARTER, directory)
    else:
        directory.mkdir()
        (directory / 'README.md').write_text('Owner files must remain unchanged.\n')
    await env.service.workflow._git(directory, 'init', '-b', 'main')
    await env.service.workflow._git(directory, 'add', '--all')
    await env.service.workflow._git(directory, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                   'commit', '-m', 'Existing owner code')
    return directory


async def import_product(env, path, output, **changes):
    response = await env.client.post('/api/v1/products', json={
        'name': 'Existing product', 'goal': 'Keep a persistent personal reading list.',
        'creation_mode': 'import', 'project_path': str(path), 'output_directory': str(output),
        'targets': ['api'], **changes}, headers={**env.headers, 'Idempotency-Key': 'import'})
    assert response.status_code == 202, response.text
    return response.json()


def source_bytes(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*')
            if p.is_file() and '.git' not in p.relative_to(path).parts}


def test_static_diagnosis_recognizes_supported_contract_without_running_code(tmp_path):
    source = tmp_path / 'project'
    shutil.copytree(STARTER, source)
    before = source_bytes(source)
    result = diagnose_project(source)
    assert result['diagnosis_kind'] == 'static' and result['execution_supported']
    assert result['detected_targets'] == ['web', 'api']
    assert source_bytes(source) == before and not (source / 'node_modules').exists()
    (source / 'tooling/build.mjs').write_text("throw new Error('custom unsupported build');")
    assert not diagnose_project(source)['execution_supported']


def test_static_diagnosis_unknown_multiplatform_and_malformed_manifests(tmp_path):
    source = tmp_path / 'unknown'
    source.mkdir()
    assert diagnose_project(source)['detected_targets'] == []
    (source / 'package.json').write_text(json.dumps({'devDependencies': {'electron': '33.0.0'}}))
    (source / 'ios/App.xcodeproj').mkdir(parents=True)
    (source / 'ios/App.xcodeproj/project.pbxproj').write_text('SDKROOT = iphoneos;')
    (source / 'android').mkdir()
    (source / 'android/build.gradle').write_text('plugins { id "com.android.application" }')
    result = diagnose_project(source)
    assert set(result['detected_targets']) >= {'ios', 'android', 'macos', 'windows', 'linux'}
    assert not result['execution_supported']
    (source / 'package.json').write_text(json.dumps({'scripts': ['invalid'], 'type': 'module'}))
    (source / 'package-lock.json').write_text('{"packages":[]}')
    assert not diagnose_project(source)['execution_supported']


async def test_import_is_readonly_and_requires_no_model_configuration(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'existing')
    before, head = source_bytes(path), await env.service.workflow._git(path, 'rev-parse', 'HEAD')
    product = await import_product(env, path, tmp_path / 'output')
    assert product['state'] == 'registered' and product['execution_supported'] and product['project_id']
    assert product['targets'] == ['api'] and product['run_id'] is None
    assert source_bytes(path) == before and await env.service.workflow._git(path, 'rev-parse', 'HEAD') == head
    assert not (tmp_path / 'output').exists()
    assert await env.store.list('run') == [] and await env.store.list('model_invocation') == []
    projects = await env.store.list('project')
    assert len(projects) == 1 and projects[0]['import_mode'] == 'snapshot_existing'


async def test_unknown_import_does_not_guess_web_and_native_new_never_starts(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'unknown', foundation=False)
    response = await env.client.post('/api/v1/products', json={'name': 'Unknown product',
        'goal': 'Preserve and inspect an existing project.', 'creation_mode': 'import',
        'project_path': str(path), 'output_directory': str(tmp_path / 'out')},
        headers={**env.headers, 'Idempotency-Key': 'unknown'})
    assert response.status_code == 202, response.text
    product = response.json()
    assert product['targets'] == [] and product['target'] is None and product['state'] == 'blocked'
    detail = (await env.client.get('/api/v1/products/' + product['id'], headers=env.headers)).json()
    assert detail['targets'] == [] and not detail['execution_supported'] and not detail['can_add_change']
    native = await env.client.post('/api/v1/products', json={'name': 'Native product',
        'goal': 'Create native desktop and mobile applications.', 'targets': ['ios', 'macos'],
        'output_directory': str(tmp_path / 'native')}, headers={**env.headers, 'Idempotency-Key': 'native'})
    assert native.status_code == 202 and native.json()['state'] == 'blocked'
    assert not (tmp_path / 'native').exists()
    assert await env.store.list('run') == [] and await env.store.list('model_invocation') == []


async def test_dirty_or_unsupported_import_is_registered_but_cannot_run(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'dirty')
    (path / 'owner-note.txt').write_text('Uncommitted owner work')
    product = await import_product(env, path, tmp_path / 'output')
    assert product['state'] == 'blocked' and not product['execution_supported']
    assert product['project_id'] is None and (path / 'owner-note.txt').read_text() == 'Uncommitted owner work'
    response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add a filter without losing existing data.', 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'blocked-change'})
    assert response.status_code == 409
    assert await env.store.list('product_change') == [] and await env.store.list('model_invocation') == []


async def test_change_creates_real_prd_plan_preserves_goal_and_replays_after_revision_changes(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'existing')
    original = source_bytes(path)
    product = await import_product(env, path, tmp_path / 'output')
    await configure(env)
    calls = []
    target = TargetConfig(app_target='api', os_name='Darwin', os_version_constraint='*', cpu_architecture='arm64',
        required_display_protocol='not_required', required_device_mode='not_required',
        build_backend=ToolRequirement(name='npm', version_constraint='>=10'),
        test_backend=ToolRequirement(name='node', version_constraint='>=22.13'))

    async def prepare(**kwargs):
        calls.append(kwargs)
        return {'state': 'ready', 'target_configs': [target.model_dump(mode='json')]}
    env.service.local.prepare = prepare
    body = {'title': 'Read filters', 'description': 'Add a filter for completed and unread books.',
        'acceptance_criteria': 'Changing the filter preserves saved read status after reload.',
        'expected_revision': product['revision'], 'review_mode': 'milestones'}
    endpoint = f"/api/v1/products/{product['id']}/changes"
    response = await env.client.post(endpoint, json=body, headers={**env.headers, 'Idempotency-Key': 'change'})
    assert response.status_code == 202, response.text
    change = response.json()
    assert change['document_baseline'] == {'product_id': product['id'], 'run_id': None, 'documents': {}}
    await env.service.lifecycle.prepare_change(change['id'])
    updated = await env.store.read('product_change', change['id'])
    assert updated['state'] == 'running', updated
    run = await env.store.read('run', updated['run_id'])
    plan = await env.store.read('plan', run['plan_id'])
    assert plan['product_contract']['document_baseline'] == change['document_baseline']
    assert plan['actual_steps'][0] == 'prd' and 'goal' not in plan['actual_steps'] and 'research' not in plan['actual_steps']
    assert {'architecture', 'unit_test_execution', 'integration_test_execution', 'delivery'} <= set(plan['actual_steps'])
    assert plan['approval_steps'] == ['prd', 'architecture', 'delivery']
    assert body['description'] in run['goal'] and body['acceptance_criteria'] in run['goal']
    assert run['parent_run_id'] is None and calls == [{'wait': True, 'required_targets': ['api']}]
    assert len(plan['stage_reused_inputs']['prd']) == 2
    for identity in plan['stage_reused_inputs']['prd']:
        artifact = await env.store.read('artifact', identity)
        assert artifact['quality_result'] == 'not_applicable' and artifact['generation'] == 0
        assert artifact['source_kind'] in {'owner_input', 'static_diagnosis'} and artifact['work_item_id'] is None
        document = json.loads(await env.service.workflow.artifacts.read(artifact['digest']))
        assert set(document) == {'title', 'summary', 'content', 'sources', 'unknowns'}
        if artifact['step'] == 'research':
            assert document['content'].startswith('## 本地工程诊断')
    saved_product = await env.store.read('product', product['id'])
    assert saved_product['goal'] == product['goal'] and saved_product['run_ids'] == [run['id']]
    replay = await env.client.post(endpoint, json=body, headers={**env.headers, 'Idempotency-Key': 'change'})
    assert replay.status_code == 202 and replay.json()['id'] == change['id']
    assert len(await env.store.list('run')) == 1 and len(await env.store.list('product_change')) == 1
    active = await env.client.post(endpoint, json={**body, 'expected_revision': saved_product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'concurrent'})
    assert active.status_code == 409
    assert source_bytes(path) == original
    assert await env.store.list('check') == [] and await env.store.list('model_invocation') == []


async def test_running_preview_blocks_new_requirement_without_stopping_it(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'existing')
    product = await import_product(env, path, tmp_path / 'output')
    async def status(_identity):
        return {'state': 'running', 'url': 'http://127.0.0.1:19999'}
    env.service.launcher = SimpleNamespace(status=status)
    response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add filtering to this existing reading list.', 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'preview-change'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'preview_must_stop'
    assert await env.store.list('product_change') == []


async def test_registration_cannot_be_retried_as_a_new_template(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'owner-project', foundation=False)
    before = source_bytes(path)
    imported = await import_product(env, path, tmp_path / 'output')
    response = await env.client.post(f"/api/v1/products/{imported['id']}/retry",
        headers={**env.headers, 'Idempotency-Key': 'cannot-initialize'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'product_registration_only'
    await env.service._change(imported['id'], state='preparing')
    await env.service.start()
    try:
        for _ in range(100):
            value = await env.store.read('product', imported['id'])
            if value['state'] == 'blocked':
                break
            await asyncio.sleep(.01)
        assert value['state'] == 'blocked'
    finally:
        await env.service.close()
    await env.service._prepare_product(imported['id'])
    assert source_bytes(path) == before and not (tmp_path / 'output').exists()
    assert await env.store.list('run') == [] and await env.store.list('model_invocation') == []


async def test_failed_change_preparation_retries_same_change_and_real_plan(product_api, tmp_path):
    env = product_api
    path = await repository(env, tmp_path / 'existing')
    product = await import_product(env, path, tmp_path / 'output')
    await configure(env)
    async def blocked(**_):
        return {'state': 'blocked', 'message': 'Local tooling is unavailable'}
    env.service.local.prepare = blocked
    response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add a persistent read filter to existing books.', 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'change'})
    change = response.json()
    await env.service.lifecycle.prepare_change(change['id'])
    assert (await env.store.read('product_change', change['id']))['state'] == 'blocked'
    endpoint = f"/api/v1/products/{product['id']}/retry"
    first = await env.client.post(endpoint, headers={**env.headers, 'Idempotency-Key': 'retry-change'})
    replay = await env.client.post(endpoint, headers={**env.headers, 'Idempotency-Key': 'retry-change'})
    assert first.status_code == replay.status_code == 202 and first.json() == replay.json()
    assert (await env.store.read('product_change', change['id']))['preparation_generation'] == 2
    async def ready(**_):
        return {'state': 'ready', 'target_configs': [{'app_target': 'api', 'target_config_id': 'planning-only'}]}
    env.service.local.prepare = ready
    await env.service.lifecycle.prepare_change(change['id'])
    changed = await env.store.read('product_change', change['id'])
    assert changed['state'] == 'running' and changed['run_id']
    assert len(await env.store.list('product_change')) == len(await env.store.list('run')) == 1
    assert (await env.store.read('product', product['id']))['goal'] == product['goal']


async def test_stage_inputs_prefer_canonical_completed_parent_not_child_or_proposal():
    # Pure selection fixture: no controller records or quality passes are written.
    artifacts = []
    work = []
    for step in ('goal', 'research'):
        for label, parent, readable in [('draft', None, False), ('final', None, False),
                                         ('proposal', None, False), ('child', 'parent', False), ('rendered', None, True)]:
            identity = step + '-' + label
            work.append({'id': identity, 'generation': 1, 'status': 'completed',
                         'quality_result': 'unknown', 'parent_stage_id': parent})
            artifacts.append({'id': identity, 'run_id': 'run', 'work_item_id': identity, 'step': step,
                'generation': 1, 'revision': 1, 'digest': 'sha256:' + 'a' * 64, 'created_at': 'same timestamp',
                'media_type': 'application/json', 'readable': readable,
                'name': 'work-proposal.json' if label == 'proposal' else 'draft.json' if label == 'draft' else 'openhands_final.json'})
    async def listed(kind):
        return {'artifact': artifacts, 'work_item': work, 'delivery': []}[kind]
    lifecycle = ProductLifecycle(SimpleNamespace(store=SimpleNamespace(list=listed)))
    refs = await lifecycle._stage_inputs({'id': 'product', 'run_ids': ['run'], 'run_id': 'run'})
    assert [ref['artifact_version_id'] for ref in refs['prd']] == ['goal-final', 'research-final']
    assert refs['architecture'] == []


async def test_legacy_product_without_new_fields_or_fingerprint_replays(product_api, tmp_path):
    env = product_api
    await configure(env)
    request = ProductRequest(name='Legacy product', goal='Preserve an older product submission without duplicating it.',
                             output_directory=str(tmp_path / 'legacy'))
    product = await env.service.submit(request, 'legacy')
    def legacy_record(tx):
        row = tx.get('product', product['id'])
        value = {k: v for k, v in row.items() if k not in
                 {'request_fingerprint', 'request_fingerprint_version', 'creation_mode', 'targets', 'project_path'}}
        return tx.put('product', row['id'], value, row['revision'])
    await env.store.command('legacy-fixture', 'before-lifecycle-fields', {}, legacy_record)
    replay = await env.service.submit(request, 'legacy')
    assert replay['id'] == product['id']
    assert len(await env.store.list('product')) == 1 and await env.store.list('model_invocation') == []
