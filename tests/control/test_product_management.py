"""Owner management over real local Git and workflow planning; no model execution."""
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest
import test_product_language
import test_product_lifecycle
import test_products
from test_model_uncertainty_acknowledgment import unknown as unknown
from test_recovery import env as env
from test_recovery import patch
from test_timeout_recovery import service as timeout_service
from test_timeout_recovery import timed_out as timed_out

from agentflow.common import DomainError
from agentflow.control.product_management import (
    ProductManagement,
    filter_visible_product_runs,
    frozen_product_version,
    guard_product_run,
)

product_api = test_products.product_api


async def registered(env, tmp_path):
    path = await test_product_lifecycle.repository(env, tmp_path / 'source')
    product = await test_product_lifecycle.import_product(env, path, tmp_path / 'output')
    return product, path


async def command(env, product, method='PATCH', suffix='', key='edit', **changes):
    return await env.client.request(method, f"/api/v1/products/{product['id']}{suffix}",
        json={'expected_revision': product['revision'], **changes},
        headers={**env.headers, 'Idempotency-Key': key})


async def put(env, kind, identity, fields):
    def apply(tx):
        current = tx.get(kind, identity)
        return tx.put(kind, identity, {**(current or {}), **fields}, current['revision'] if current else None)
    return await env.store.command('fixture.management', kind + identity + str(fields), {}, apply)


async def test_product_list_projects_management_with_timeout_authorization_records(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    response = await env.client.get('/api/v1/products', headers=env.headers)
    assert response.status_code == 200, response.text
    listed = next(row for row in response.json()['items'] if row['id'] == product['id'])
    assert listed['management']['can_edit']


async def test_product_list_reuses_management_evidence_without_caching_next_request(product_api, tmp_path, monkeypatch):
    env = product_api
    products = []
    for index in range(2):
        path = await test_product_lifecycle.repository(env, tmp_path / f'source-{index}')
        response = await env.client.post('/api/v1/products', json={
            'name': f'Product {index}', 'goal': 'Keep a reading list.', 'creation_mode': 'import',
            'project_path': str(path), 'output_directory': str(tmp_path / f'output-{index}'),
            'targets': ['api']}, headers={**env.headers, 'Idempotency-Key': f'import-{index}'})
        assert response.status_code == 202, response.text
        products.append(response.json())
    counts = Counter()
    original = env.store.list
    from agentflow.models import uncertainty
    acknowledge = uncertainty.acknowledged_invocation_ids

    def counted_acknowledgment(state):
        counts['acknowledgment_validation'] += 1
        return acknowledge(state)

    async def counted(kind):
        counts[kind] += 1
        return await original(kind)

    monkeypatch.setattr(env.store, 'list', counted)
    monkeypatch.setattr(uncertainty, 'acknowledged_invocation_ids', counted_acknowledgment)
    response = await env.client.get('/api/v1/products', headers=env.headers)
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 2
    assert counts['model_invocation'] == 1
    assert counts['acknowledgment_validation'] == 1
    assert all(row['management']['can_edit'] for row in response.json()['items'])
    await put(env, 'product', products[0]['id'], {'restore_reconciliation_required': True})
    counts.clear()
    response = await env.client.get('/api/v1/products', headers=env.headers)
    listed = {row['id']: row for row in response.json()['items']}
    assert not listed[products[0]['id']]['management']['can_edit']
    assert listed[products[1]['id']]['management']['can_edit']
    assert counts['model_invocation'] == 1
    assert counts['acknowledgment_validation'] == 1


async def test_management_read_projection_honors_only_verified_timeout_authorization(timed_out):
    value = timed_out
    assert await timeout_service(value).prepare('run', 'bad')
    await patch(value, 'run', 'run', execution_state='paused')
    management = ProductManagement(SimpleNamespace(store=value.store, launcher=None))
    product = {'id': 'product', 'run_id': 'run', 'targets': ['web']}
    assert (await management.describe(product))['can_edit']

    authorization = (await value.store.list('timeout_recovery'))[0]
    await patch(value, 'timeout_recovery', authorization['id'], authorization_digest='invalid')
    result = await management.describe(product)
    assert not result['can_edit']
    assert 'product_model_calls_unsettled' in {row['code'] for row in result['blocked_reasons']}


async def test_name_and_future_defaults_do_not_restart_or_change_source_and_initial_submit_replays(product_api, tmp_path):
    env = product_api
    product, path = await registered(env, tmp_path)
    original = test_product_lifecycle.source_bytes(path)
    response = await command(env, product, name='My reading list', language='en', max_model_requests=0, review_mode='every_step')
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['name'] == 'My reading list' and updated['language'] == 'en'
    assert updated['max_model_requests'] == 0 and updated['config_revision'] == 1 and not updated['needs_restart']
    assert updated['run_id'] is None and updated['project_id'] == product['project_id']
    assert test_product_lifecycle.source_bytes(path) == original
    assert await env.store.list('run') == await env.store.list('model_invocation') == []
    assert (await command(env, product, name='My reading list', language='en', max_model_requests=0,
                          review_mode='every_step')).json() == updated
    replay = await test_product_lifecycle.import_product(env, path, tmp_path / 'output')
    assert replay['id'] == product['id'] and len(await env.store.list('product')) == 1
    assert len(await env.store.list('product_management_operation')) == 1


@pytest.mark.parametrize(('initial_maximum', 'explicit_default', 'expected'), [(200, True, 0), (0, True, 0), (0, False, 200)])
async def test_product_default_override_preserves_zero_against_fixed_global_default(
        product_api, tmp_path, monkeypatch, initial_maximum, explicit_default, expected):
    env = product_api
    test_products.attach_file_configuration(env, monkeypatch, tmp_path, 'max_model_requests = 200\n')
    await test_products.configure(env)
    path = await test_product_lifecycle.repository(env, tmp_path / 'source')
    product = await test_product_lifecycle.import_product(env, path, tmp_path / 'output',
                                                         max_model_requests=initial_maximum)
    response = await command(env, product, **({'max_model_requests': 0} if explicit_default else {'name': 'Renamed only'}))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert ('max_model_requests' in saved['default_overrides']) is explicit_default
    assert not saved['needs_restart']
    change = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add a filter for books that are already read.', 'expected_revision': saved['revision']},
        headers={**env.headers, 'Idempotency-Key': 'after-product-setting'})
    assert change.status_code == 202, change.text
    assert change.json()['max_model_requests'] == expected
    assert await env.store.list('run') == [] and await env.store.list('model_invocation') == []


async def test_critical_edit_marks_a_new_configuration_without_executing_or_reusing_old_inputs(product_api, tmp_path):
    env = product_api
    product, path = await registered(env, tmp_path)
    before = test_product_lifecycle.source_bytes(path)
    response = await command(env, product, goal='Build a personal collection manager with lending records.', targets=['web', 'api'])
    assert response.status_code == 200, response.text
    current = response.json()
    assert current['needs_restart'] and current['config_revision'] == 2
    assert current['run_config_revision'] is None and current['run_id'] is None
    assert current['target'] == 'web' and current['targets'] == ['web', 'api']
    detail = (await env.client.get('/api/v1/products/' + product['id'], headers=env.headers)).json()
    assert detail['management']['can_restart'] and not detail['can_add_change']
    for kind in ('run', 'plan', 'artifact', 'model_invocation', 'product_change'):
        assert await env.store.list(kind) == []
    assert test_product_lifecycle.source_bytes(path) == before
    # Ordinary incremental requirements cannot bypass the required full run.
    denied = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add another filter for the lending list.', 'expected_revision': current['revision']},
        headers={**env.headers, 'Idempotency-Key': 'must-restart'})
    assert denied.status_code == 409 and denied.json()['error']['code'] == 'product_restart_required'


async def test_delete_and_restore_preserve_source_history_and_directory_ownership(product_api, tmp_path):
    env = product_api
    product, path = await registered(env, tmp_path)
    before = test_product_lifecycle.source_bytes(path)
    deleted = await command(env, product, 'DELETE', key='delete', reason='No longer shown in my products')
    assert deleted.status_code == 200, deleted.text
    tombstone = deleted.json()
    assert tombstone['deleted_at'] and tombstone['project_id'] == product['project_id']
    assert (await env.client.get('/api/v1/products', headers=env.headers)).json()['items'] == []
    trash = (await env.client.get('/api/v1/products?view=deleted', headers=env.headers)).json()['items']
    assert len(trash) == 1 and trash[0]['management']['can_restore'] and not trash[0]['management']['can_edit']
    assert len((await env.client.get('/api/v1/products?view=all', headers=env.headers)).json()['items']) == 1
    assert (await command(env, tombstone, name='Cannot change deleted product', key='deleted-edit')).status_code == 409
    assert (await command(env, tombstone, 'POST', '/restart', key='deleted-restart')).status_code == 409
    assert (await command(env, product, 'DELETE', key='delete', reason='No longer shown in my products')).json() == tombstone
    restored = await command(env, tombstone, 'POST', '/restore', key='restore')
    assert restored.status_code == 200 and restored.json()['deleted_at'] is None
    assert restored.json()['run_id'] is None and restored.json()['state'] == product['state']
    assert (await command(env, tombstone, 'POST', '/restore', key='restore')).json() == restored.json()
    assert test_product_lifecycle.source_bytes(path) == before
    assert len(await env.store.list('project')) == 1 and await env.store.list('model_invocation') == []
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize(('kind', 'fields', 'code'), [
    ('run', {'execution_state': 'running'}, 'product_run_active'),
    ('attempt', {'status': 'execution_unknown'}, 'product_attempt_active'),
    ('supervised_attempt', {'state': 'running'}, 'product_attempt_active'),
    ('model_invocation', {'state': 'reserved', 'amount_micros': 0}, 'product_model_calls_unsettled'),
    ('model_invocation', {'state': 'dispatching', 'amount_micros': 0}, 'product_model_calls_unsettled'),
    ('model_invocation', {'state': 'uncertain', 'amount_micros': 0}, 'product_model_calls_unsettled'),
    ('node_job', {'state': 'queued'}, 'product_node_jobs_active'),
    ('node_job', {'state': 'execution_unknown'}, 'product_node_jobs_active'),
])
async def test_busy_product_has_specific_blockers_for_edit_delete_and_restart(product_api, tmp_path, kind, fields, code):
    env = product_api
    product, _ = await registered(env, tmp_path)
    await put(env, 'run', 'old-run', {'execution_state': 'paused', 'iteration_id': 'iteration'})
    product = await env.service._change(product['id'], run_id='old-run', run_ids=['old-run'])
    await put(env, kind, 'old-run' if kind == 'run' else 'busy', {'run_id': 'old-run', **fields})
    detail = await env.service.detail(product['id'])
    assert not detail['management']['can_edit']
    assert code in {item['code'] for item in detail['management']['blocked_reasons']}
    for method, suffix, changes in [('PATCH', '', {'name': 'Renamed'}), ('DELETE', '', {}), ('POST', '/restart', {})]:
        result = await command(env, product, method, suffix, key=method, **changes)
        assert result.status_code == 409 and result.json()['error']['code'] == code
    assert await env.store.list('product_management_operation') == []


async def test_preview_and_preparation_block_all_editing_including_legacy_language(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    async def status(_identity):
        return {'state': 'running', 'url': 'http://127.0.0.1:12345'}
    env.service.launcher = SimpleNamespace(status=status)
    result = await command(env, product, name='Rename during preview')
    assert result.status_code == 409 and result.json()['error']['code'] == 'preview_must_stop'
    result = await env.client.post(f"/api/v1/products/{product['id']}/language",
        json={'language': 'en', 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'language-preview'})
    assert result.status_code == 409 and result.json()['error']['code'] == 'preview_must_stop'
    env.service.launcher = None
    product = await env.service._change(product['id'], state='preparing')
    assert (await command(env, product, 'DELETE', key='preparing-delete')).json()['error']['code'] == 'product_preparing'


async def test_restart_creates_full_fresh_iteration_and_preserves_imported_source_and_old_run(product_api, tmp_path):
    env = product_api
    product, path = await registered(env, tmp_path)
    original, head = test_product_lifecycle.source_bytes(path), await env.service.workflow._git(path, 'rev-parse', 'HEAD')
    await test_products.configure(env)
    test_product_language.planning_target(env)
    first = await command(env, product, 'POST', '/restart', key='first-run')
    assert first.status_code == 202, first.text
    assert first.json()['kind'] == 'restart' and first.json()['run_id'] is None
    assert await env.store.list('run') == []
    await env.service.lifecycle.prepare_change(first.json()['id'])
    product = await env.store.read('product', product['id'])
    first_run = await env.store.read('run', product['run_id'])
    first_plan = await env.store.read('plan', first_run['plan_id'])
    assert first_plan['actual_steps'][0] == 'goal' and first_plan['actual_steps'][-1] == 'delivery'
    assert first_plan['reused_inputs'] == [] and first_plan['product_contract']['config_revision'] == 1
    assert first_plan['base_commit'] == head
    await env.service.workflow.control_run(first_run['id'], {'expected_revision': first_run['revision'],
        'action': 'cancel', 'reason': 'Planning-only fixture: stop before any agent executes'}, 'cancel-first')
    await env.service._advance(product)
    product = await env.store.read('product', product['id'])
    changed = await command(env, product, key='goal-update', goal='Build a reading list with lending and due-date tracking.')
    assert changed.status_code == 200, changed.text
    product = changed.json()
    assert product['needs_restart'] and product['run_config_revision'] == 1 and product['config_revision'] == 2
    second = await command(env, product, 'POST', '/restart', key='second-run')
    assert second.status_code == 202, second.text
    await env.service.lifecycle.prepare_change(second.json()['id'])
    current = await env.store.read('product', product['id'])
    second_run = await env.store.read('run', current['run_id'])
    second_plan = await env.store.read('plan', second_run['plan_id'])
    assert first_run['id'] != second_run['id'] and first_run['iteration_id'] != second_run['iteration_id']
    assert current['run_ids'] == [first_run['id'], second_run['id']] and current['initial_run_id'] == first_run['id']
    assert not current['needs_restart'] and current['run_config_revision'] == current['config_revision'] == 2
    assert second_plan['actual_steps'][0] == 'goal' and second_plan['reused_inputs'] == []
    assert second_plan.get('stage_reused_inputs', {}) == {}
    assert second_run['goal'] == product['goal'] and second_run['parent_run_id'] is None
    assert second_plan['product_contract']['base_run_id'] == first_run['id']
    assert second_plan['product_contract']['product_snapshot']['goal'] == product['goal']
    assert all(item['status'] == 'pending' and item['generation'] == 1
               for item in await env.store.list('work_item') if item['run_id'] == second_run['id'])
    assert await env.store.read('plan', first_plan['id']) == first_plan
    assert test_product_lifecycle.source_bytes(path) == original
    assert await env.service.workflow._git(path, 'rev-parse', 'HEAD') == head
    assert await env.store.list('model_invocation') == []
    assert (await command(env, product, 'POST', '/restart', key='second-run')).json()['id'] == second.json()['id']


async def test_background_advance_cannot_resurrect_deleted_or_superseded_configuration(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    await put(env, 'run', 'old-run', {'execution_state': 'completed', 'delivery_ids': [], 'iteration_id': 'i'})
    product = await env.service._change(product['id'], run_id='old-run', run_ids=['old-run'])
    changed = await command(env, product, goal='A different product goal that needs a complete fresh run.')
    assert changed.status_code == 200
    current = changed.json()
    await env.service._advance(product)
    assert await env.store.read('product', product['id']) == current
    removed = (await command(env, current, 'DELETE', key='remove')).json()
    await env.service._advance(current)
    await env.service._prepare_product(product['id'])
    assert await env.store.read('product', product['id']) == removed
    assert len(await env.store.list('run')) == 1


async def test_paused_product_is_not_automatically_repaired_or_marked_running_while_editable(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    await put(env, 'run', 'paused-run', {'execution_state': 'paused', 'delivery_ids': []})
    product = await env.service._change(product['id'], run_id='paused-run', state='blocked')
    async def forbidden(*_):
        raise AssertionError('Owner-paused work must not be automatically repaired')
    env.service.test_repair.attempt = forbidden
    env.service.review_repair.repair = forbidden
    await env.service._advance(product)
    assert await env.store.read('product', product['id']) == product
    assert (await env.service.detail(product['id']))['management']['can_edit']


async def test_new_registered_product_can_select_supported_target_and_explicitly_start_fresh(product_api, tmp_path):
    env = product_api
    response = await env.client.post('/api/v1/products', json={'name': 'New registration',
        'goal': 'Build a personal reading list application.', 'targets': ['ios'],
        'output_directory': str(tmp_path / 'new-output')}, headers={**env.headers, 'Idempotency-Key': 'native'})
    assert response.status_code == 202 and response.json()['project_id'] is None
    changed = await command(env, response.json(), targets=['api'])
    assert changed.status_code == 200 and changed.json()['needs_restart']
    await test_products.configure(env)
    test_product_language.planning_target(env)
    started = await command(env, changed.json(), 'POST', '/restart', key='new-api-run')
    assert started.status_code == 202, started.text
    await env.service.lifecycle.prepare_change(started.json()['id'])
    product = await env.store.read('product', changed.json()['id'])
    assert product['state'] == 'running', product
    plan = await env.store.read('plan', product['plan_id'])
    assert plan['actual_steps'][0] == 'goal' and plan['product_contract']['config_revision'] == 2
    project = await env.store.read('project', product['project_id'])
    assert project['local_path'] == str(tmp_path / 'new-output/repository')
    assert await env.store.list('model_invocation') == []


async def test_revision_conflicts_and_concurrent_edits_do_not_double_apply(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    results = await asyncio.gather(command(env, product, name='First', key='one'),
                                   command(env, product, name='Second', key='two'))
    assert sorted(response.status_code for response in results) == [200, 409]
    assert len(await env.store.list('product_management_operation')) == 1
    assert (await command(env, product, goal='Different payload under an existing key', key='one')).status_code == 409


@pytest.mark.parametrize('fields', [{}, {'name': None}, {'goal': 'short'}, {'targets': []}, {'targets': ['api', 'api']},
    {'max_model_requests': False}, {'max_model_requests': 2001}, {'project_path': '/tmp/not-allowed'}])
async def test_invalid_edits_are_rejected_before_mutation(product_api, tmp_path, fields):
    env = product_api
    product, _ = await registered(env, tmp_path)
    result = await command(env, product, **fields)
    assert result.status_code == 422
    assert await env.store.list('product_management_operation') == []


async def test_run_visibility_and_owner_guards_preserve_unowned_low_level_runs(product_api, tmp_path):
    env = product_api
    product, _ = await registered(env, tmp_path)
    await put(env, 'plan', 'owned', {'product_contract': {'product_id': product['id'], 'config_revision': 1}})
    run = await put(env, 'run', 'owned-run', {'plan_id': 'owned', 'execution_state': 'completed'})
    other = await put(env, 'run', 'low-level', {'execution_state': 'completed'})
    product = await env.service._change(product['id'], run_id=run['id'])
    deleted = await command(env, product, 'DELETE', key='delete')
    assert deleted.status_code == 200
    assert await filter_visible_product_runs(env.store, [run, other]) == [other]
    with pytest.raises(DomainError) as error:
        await env.store.command('fixture.guard', 'deleted', {}, lambda tx: guard_product_run(tx, run) or {})
    assert error.value.code == 'product_deleted'
    restored = (await command(env, deleted.json(), 'POST', '/restore', key='restore')).json()
    await command(env, restored, goal='A changed goal that invalidates the old run.', key='critical')
    with pytest.raises(DomainError) as error:
        await env.store.command('fixture.guard', 'stale', {}, lambda tx: guard_product_run(tx, run) or {})
    assert error.value.code == 'product_restart_required'


def test_history_metadata_uses_frozen_snapshot_without_changing_ownership_paths():
    product = {'id': 'product', 'name': 'New name', 'goal': 'New goal', 'target': 'api', 'targets': ['api'],
               'output_directory': '/owned/output', 'project_id': 'original', 'language': 'en'}
    run = {'id': 'old-run', 'goal': 'Old goal', 'display_name': 'Old name'}
    plan = {'product_contract': {'change_id': 'old-change', 'product_snapshot': {
        'name': 'Frozen name', 'goal': 'Frozen goal', 'target': 'web', 'targets': ['web'],
        'language': 'zh-CN', 'output_directory': '/forbidden', 'project_id': 'different'}}}
    snapshot = frozen_product_version(product, run, plan)
    assert snapshot['name'] == 'Frozen name' and snapshot['goal'] == 'Frozen goal' and snapshot['target'] == 'web'
    assert snapshot['output_directory'] == product['output_directory'] and snapshot['project_id'] == 'original'
    assert snapshot['run_id'] == 'old-run' and snapshot['current_change_id'] == 'old-change'
