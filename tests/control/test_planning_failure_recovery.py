"""Planning output faults retain precise evidence without granting runtime authority."""
import shutil
from pathlib import Path

import pytest
from test_failure_remediation import automatic as automatic
from test_recovery import env as env
from test_recovery import patch
from test_role_output_recovery import partial_role as partial_role
from test_role_output_recovery import recover_and_claim

from agentflow.adapters.openhands.output_builder import ResultBuilderStore
from agentflow.control.recovery import restore_role_output_checkpoint
from agentflow.runtime.launcher import atomic_json

DIAGNOSTIC = {'code': 'planning_validation_failed', 'message': 'Invalid planning output', 'details': {
    'origin': 'planning_validation', 'phase': 'plan_validation', 'category': 'correctable_output',
    'issues': [{'code': 'role_write_scope', 'stage_key': 'review', 'child_key': 'review:a',
                'path': '/parallel_work/0/children/0/write_paths', 'message': 'Read-only stage requires empty write_paths'}]}}

async def planning(env, **fields):
    await patch(env, 'work_item', 'bad', step='development_plan', role='architecture_planning', status='blocked',
                runtime_failure_code=None, **fields)
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code=None, status='blocked')

async def test_structured_planning_fault_retries_same_work_with_exact_issues(automatic):
    await planning(automatic, failure_diagnostic=DIAGNOSTIC)
    result = await automatic.automatic.analyze('bad')
    assert result['failure_code'] == 'planning_validation_failed'
    assert result['action'] == 'retry_current'
    assert result['failure_diagnostic'] == DIAGNOSTIC
    assert '/parallel_work/0/children/0/write_paths' in result['repair_instruction']

@pytest.mark.parametrize('code', ['write_scope_violation', 'model_authentication_failed', 'model_request_limit_reached', 'execution_unconfirmed'])
async def test_planning_metadata_cannot_override_runtime_permission_budget_or_identity(automatic, code):
    await planning(automatic, failure_diagnostic=DIAGNOSTIC)
    await patch(automatic, 'work_item', 'bad', runtime_failure_code=code)
    result = await automatic.automatic.analyze('bad')
    assert result['failure_code'] == code
    assert result['action'] == 'needs_attention'

async def test_unknown_controller_error_keeps_diagnosis_and_has_no_false_cooldown(automatic):
    automatic.settings = automatic.settings.model_copy(update={'auto_failure_retry_delay_seconds': 20})
    automatic.workflow.settings = automatic.settings
    diagnostic = {'code': 'unexpected_controller_constraint', 'message': 'Frozen plan version differs', 'details': {'actual': 4}}
    await planning(automatic, failure_diagnostic=diagnostic, blocking_reason=diagnostic['message'])
    result = await automatic.automatic.analyze('bad')
    assert result['failure_code'] == 'controller_validation_failed'
    assert result['failure_diagnostic'] == diagnostic
    assert diagnostic['message'] in result['summary']
    assert result['action'] == 'needs_attention'
    assert not any(item['code'] == 'retry_backoff' for item in result['blockers'])
    assert result['not_before'] is None



async def completed_planning(env):
    shutil.rmtree(env.role_root / '.role_output')
    await planning(env, blocking_reason='Non-coding stages cannot acquire source write permission')
    await patch(env, 'work_item', 'after', key='review', step='code_review', role='review', status='pending', attempt_id=None)
    task = {**env.role_task, 'step': 'development_plan', 'role': 'architecture_planning',
            'output_schema': {'type': 'object', 'required': ['summary', 'content', 'parallel_work']}}
    await patch(env, 'dispatch_context', 'bad-attempt', task=task)
    await patch(env, 'supervised_attempt', 'bad-attempt', state='completed')
    value = {'summary': 'Saved plan', 'content': 'Already completed architecture-backed text. ' * 120,
             'parallel_work': [{'stage_key': 'review', 'children': [
                 {'key': 'a', 'goal': 'Review', 'write_paths': ['src/a.js']},
                 {'key': 'b', 'goal': 'Review', 'write_paths': ['src/b.js']}]}]}
    atomic_json(env.role_root / 'openhands_final.json', value)
    atomic_json(env.role_root / 'role_result.json', {'execution_status': 'completed', 'result': value})
    env.role_task = task
    return value

async def test_exact_legacy_plan_error_collects_every_read_only_child(partial_role):
    from agentflow.control.planning_recovery import planning_failure_diagnostic
    env = partial_role
    await completed_planning(env)
    work, attempt = await env.store.read('work_item', 'bad'), await env.store.read('attempt', 'bad-attempt')
    value = await planning_failure_diagnostic(env.store, env.settings, work, attempt)
    assert value['code'] == 'planning_validation_failed'
    assert [issue['path'] for issue in value['details']['issues']] == [
        '/parallel_work/0/children/0/write_paths', '/parallel_work/0/children/1/write_paths']
    for reason in ('Collected code changes exceeded the assigned file scope', 'role_write_scope', 'arbitrary write permission error'):
        assert await planning_failure_diagnostic(env.store, env.settings, {**work, 'blocking_reason': reason}, attempt) is None

async def test_complete_planning_final_recovers_new_draft_preserves_original_and_budget(partial_role):
    env = partial_role
    value = await completed_planning(env)
    before = {str(path.relative_to(env.role_root)): path.read_bytes() for path in env.role_root.rglob('*') if path.is_file()}
    upstream = await env.store.read('work_item', 'upstream')
    accounts = await env.store.list('budget_account')
    receipt, work, task = await recover_and_claim(env)
    # Optional planning schema enrichment must not throw away a valid legacy draft.
    task = {**task, 'output_schema': {**task['output_schema'], 'properties': {'parallel_work': {'type': 'array'}}}}
    imported = await restore_role_output_checkpoint(env.store, env.settings, work, task)
    builder = ResultBuilderStore(task)
    assert builder.resolve(imported['builders'][0]['result_ref']) == value
    assert imported['mode'] == 'revise_planning_output'
    assert not (Path(task['artifact_dir']) / 'openhands_final.json').exists()
    assert {str(path.relative_to(env.role_root)): path.read_bytes() for path in env.role_root.rglob('*') if path.is_file()} == before
    assert await env.store.read('work_item', 'upstream') == upstream
    assert await env.store.list('budget_account') == accounts
    assert receipt['role_output_checkpoints'][0]['source_attempt_id'] == 'bad-attempt'

async def test_planning_retry_archives_half_submitted_children_and_keeps_upstream_usage(partial_role):
    env = partial_role
    await completed_planning(env)
    parents = {}
    for step in ('prd', 'requirements', 'architecture'):
        parents[step] = await patch(env, 'work_item', step, run_id='run', project_id=env.project['id'], key=step,
            step=step, role='product', status='completed', quality_result='passed', generation=1, fencing_token=1,
            input_fingerprint=step, dependencies=[], write_paths=[], payload={}, artifact_ids=[], required=True)
    await patch(env, 'work_item', 'bad', dependencies=list(parents))
    children = []
    for n in range(3):
        identity = f'half-child-{n}'
        children.append(identity)
        await patch(env, 'work_item', identity, run_id='run', project_id=env.project['id'], key=identity,
            step='implementation', role='coding', status='pending', generation=1, fencing_token=0,
            input_fingerprint=identity, dependencies=['bad'], write_paths=[f'src/{n}.js'], payload={},
            kind='stage_child', parent_stage_id='after', artifact_ids=[], required=True)
    await patch(env, 'work_item', 'after', kind='aggregation', role='system', dependencies=children,
        original_dependencies=['bad'], original_write_paths=['src'], expanded_child_ids=children, expansion_fingerprint='old-expansion')
    await patch(env, 'stage_expansion', 'old-expansion', run_id='run', stage_id='after', child_ids=children, stale=False)
    for n in range(24):
        await patch(env, 'model_invocation', f'paid-call-{n}', run_id='run', attempt_id='bad-attempt', state='settled')
    usages = await env.store.list('model_invocation')
    run = await env.store.read('run', 'run')
    receipt = await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'bad'}, 'fix-plan')
    for identity in children:
        assert (await env.store.read('work_item', identity))['archived']
    stage = await env.store.read('work_item', 'after')
    assert stage['kind'] == 'stage' and stage['dependencies'] == ['bad']
    for step, value in parents.items():
        assert await env.store.read('work_item', step) == value
    assert await env.store.list('model_invocation') == usages
    work = await env.store.read('work_item', 'bad')
    assert 'role_write_scope' in work['payload']['recovery_instruction']
    assert work['payload']['planning_recovery_diagnostic']['details']['issues']
    assert work['approval_required'] and receipt['execution'] == 'fresh_attempt'

async def test_permission_blocker_has_no_retry_countdown_even_for_retryable_output(automatic):
    await planning(automatic, failure_diagnostic=DIAGNOSTIC)
    automatic.settings = automatic.settings.model_copy(update={'auto_failure_retry_delay_seconds': 20})
    automatic.workflow.settings = automatic.settings
    await patch(automatic, 'work_item', 'after', status='waiting_approval')
    result = await automatic.automatic.analyze('bad')
    assert result['status'] == 'blocked' and result['not_before'] is None
    assert not any(row['code'] == 'retry_backoff' for row in result['blockers'])

async def test_worker_planning_error_classifies_structured_details_only(automatic):
    from agentflow.runtime.failures import classify_role_error
    value = {'type': 'DomainError', 'message': 'Plan invalid', 'runtime_failure_code': 'planning_validation_failed',
             'failure_details': DIAGNOSTIC['details']}
    assert classify_role_error(value) == 'planning_validation_failed'
    assert classify_role_error({**value, 'failure_details': {'origin': 'runtime'}}) != 'planning_validation_failed'

@pytest.mark.parametrize('evidence', ['unknown_state', 'attempt_budget'])
async def test_planning_code_never_hides_unknown_execution_or_attempt_budget(automatic, evidence):
    await planning(automatic, failure_diagnostic=DIAGNOSTIC)
    await patch(automatic, 'work_item', 'bad', runtime_failure_code='planning_validation_failed')
    if evidence == 'unknown_state':
        await patch(automatic, 'attempt', 'bad-attempt', status='execution_unknown')
        expected = 'execution_unconfirmed'
    else:
        await patch(automatic, 'attempt', 'bad-attempt', runtime_failure_code='model_request_limit_reached')
        expected = 'model_request_limit_reached'
    result = await automatic.automatic.analyze('bad')
    assert result['failure_code'] == expected
    assert result['action'] == 'needs_attention'

@pytest.mark.parametrize('damage', ['target_schema', 'source_bytes', 'receipt_bytes'])
async def test_completed_planning_import_refuses_changed_evidence(partial_role, damage):
    from agentflow.common import DomainError
    env = partial_role
    await completed_planning(env)
    _, work, task = await recover_and_claim(env)
    if damage == 'target_schema':
        task = {**task, 'output_schema': {'type': 'object', 'required': ['new_mandatory_field']}}
    elif damage == 'source_bytes':
        atomic_json(env.role_root / 'openhands_final.json', {'changed': True})
    else:
        atomic_json(env.role_root / 'role_result.json', {'changed': True})
    with pytest.raises(DomainError) as error:
        await restore_role_output_checkpoint(env.store, env.settings, work, task)
    assert error.value.code == 'invalid_role_output_checkpoint'
    assert not await env.store.list('role_output_import')

async def test_sealed_complete_planning_draft_can_recover_under_new_schema(partial_role):
    env = partial_role
    value = await completed_planning(env)
    # A previously sealed builder remains exactly as it was; the new attempt
    # receives a separately authorized source draft from its completed final.
    env.role_task['max_output_tokens'] = 8192
    await patch(env, 'dispatch_context', 'bad-attempt', task=env.role_task)
    builder = ResultBuilderStore(env.role_task)
    initial = builder.begin({**value, 'content': ''}, {'content': 'string'}, 'sealed-plan')
    sealed = builder.append(initial['result_ref'], 'content', 'body', 0, 'Saved complete body.', True)
    value['content'] = 'Saved complete body.'
    atomic_json(env.role_root / 'openhands_final.json', value)
    atomic_json(env.role_root / 'role_result.json', {'execution_status': 'completed', 'result': value})
    before = {str(path.relative_to(env.role_root)): path.read_bytes() for path in env.role_root.rglob('*') if path.is_file()}
    _, work, task = await recover_and_claim(env)
    imported = await restore_role_output_checkpoint(env.store, env.settings, work, task)
    assert ResultBuilderStore(task).resolve(imported['builders'][0]['result_ref']) == value
    assert builder.resolve(sealed['result_ref']) == value
    assert {str(path.relative_to(env.role_root)): path.read_bytes() for path in env.role_root.rglob('*') if path.is_file()} == before

@pytest.mark.parametrize('code', ['stale_planning_contract', 'revision_conflict', 'stale_producer'])
async def test_only_explicit_planning_contract_change_can_refresh_and_retry(automatic, code):
    diagnostic = {'code': code, 'message': '规划目标阶段版本已变化，需重新读取并核验。', 'details': None}
    await planning(automatic, failure_diagnostic=diagnostic)
    await patch(automatic, 'work_item', 'bad', runtime_failure_code='controller_validation_failed')
    result = await automatic.automatic.analyze('bad')
    if code == 'stale_planning_contract':
        assert result['failure_code'] == 'planning_state_changed' and result['action'] == 'retry_current'
        assert '重新读取' in result['repair_instruction']
        assert '字段错误' not in result['repair_instruction']
    else:
        assert result['failure_code'] == 'controller_validation_failed' and result['action'] == 'needs_attention'
    assert result['failure_diagnostic'] == diagnostic

@pytest.mark.parametrize('failure', ['validation', 'state_changed'])
async def test_automatic_planning_recovery_preserves_full_diagnosis_without_reason_size_limit(automatic, failure):
    import copy
    diagnostic = copy.deepcopy(DIAGNOSTIC) if failure == 'validation' else {
        'code': 'stale_planning_contract', 'message': 'Planning snapshot changed', 'details': None}
    if failure == 'validation':
        diagnostic['details']['issues'] = [dict(DIAGNOSTIC['details']['issues'][0], child_key=f'review:{n}') for n in range(30)]
    await planning(automatic, failure_diagnostic=diagnostic)
    result = await automatic.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    work = await automatic.store.read('work_item', 'bad')
    assert work['generation'] == 2 and work['payload']['planning_recovery_diagnostic'] == diagnostic
    if failure == 'validation':
        assert 'review:29' in work['payload']['recovery_instruction']
    else:
        assert '重新读取' in work['payload']['recovery_instruction']
