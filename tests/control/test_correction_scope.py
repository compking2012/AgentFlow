"""A rework instruction belongs to its selected work, not every invalidated stage."""
from uuid import uuid4

import pytest
from test_recovery import env as recovery_env
from test_recovery import patch, stopped_workspace
from test_remediation import env as review_env

from agentflow.common import DomainError
from agentflow.control.recovery import RecoveryRequest, _recovery_command
from agentflow.control.scheduler import Scheduler

__all__ = ['recovery_env', 'review_env']


async def test_review_repair_does_not_turn_downstream_test_work_into_product_repair(review_env):
    env = review_env
    await patch(env, 'run', 'run', goal='Implement addition and verify it independently.')
    unit = await env.store.read('work_item', 'unit')
    await patch(env, 'work_item', 'unit-code', **{k: v for k, v in unit.items() if k not in {'id', 'revision'}})
    await patch(env, 'work_item', 'unit-code', key='unit_test_implementation', step='unit_test_implementation',
                dependencies=['unit'], write_paths=['tests'],
                payload={'change_expectation': 'An earlier product repair leaked here.',
                         'recovery_instruction': 'Old execution advice', 'goal': 'Implement all planned unit cases'})
    accounts = await env.store.list('budget_account')
    assert await env.remediation.repair('review-work')
    code = await env.store.read('work_item', 'code')
    assert 'Addition subtracts' in code['payload']['change_expectation']
    for identity in ('review-work', 'unit', 'unit-code'):
        work = await env.store.read('work_item', identity)
        assert 'change_expectation' not in work['payload']
        assert 'recovery_instruction' not in work['payload']
    work = await env.store.read('work_item', 'unit-code')
    prompt = await Scheduler(env.workflow, env.store, None, None, env.settings)._prompt(
        await env.store.read('run', 'run'), work, env.snapshot['commit_oid'])
    assert 'Implement independent executable unit tests' in prompt
    assert 'Required correction: \n' in prompt
    assert 'Assigned subtask: Implement all planned unit cases' in prompt
    assert await env.store.list('budget_account') == accounts


async def test_retry_preserves_only_selected_work_correction_and_execution_guidance(recovery_env):
    env = recovery_env
    await patch(env, 'work_item', 'bad', payload={'change_expectation': 'Complete market evidence.'})
    await patch(env, 'work_item', 'after', payload={'change_expectation': 'Complete market evidence.',
                                                 'recovery_instruction': 'Prior attempt advice', 'goal': 'Write PRD'})
    run = await env.store.read('run', 'run')
    await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': 'bad', 'reason': 'Continue the stopped research.'}, str(uuid4()))
    root = await env.store.read('work_item', 'bad')
    assert root['payload']['change_expectation'] == 'Complete market evidence.'
    assert root['payload']['recovery_instruction'] == 'Continue the stopped research.'
    after = await env.store.read('work_item', 'after')
    assert after['payload']['goal'] == 'Write PRD'
    assert 'change_expectation' not in after['payload']
    assert 'recovery_instruction' not in after['payload']


@pytest.mark.parametrize('expand', [False, True])
async def test_revision_applies_correction_to_selected_stage_children_only(recovery_env, expand):
    env = recovery_env
    await patch(env, 'work_item', 'bad', kind='aggregation')
    after = await env.store.read('work_item', 'after')
    await patch(env, 'work_item', 'child', **{k: v for k, v in after.items() if k not in {'id', 'revision'}})
    await patch(env, 'work_item', 'child', key='research:segment', step='research', kind='stage_child',
                parent_stage_id='bad', dependencies=['upstream'])
    items = [row for row in await env.store.list('work_item') if row['run_id'] == 'run']
    def apply(tx):
        return {'affected': sorted(env.workflow._invalidate(tx, items, {'bad'}, 'Research this segment.',
                                                            expand_roots=expand))}
    result = await env.store.command('fixture.revise', str(uuid4()), {}, apply)
    for identity in ('bad', 'child'):
        work = await env.store.read('work_item', identity)
        if identity == 'bad' or expand:
            assert work['payload']['change_expectation'] == 'Research this segment.'
        else:
            assert identity not in result['affected']
    assert 'change_expectation' not in (await env.store.read('work_item', 'after'))['payload']


async def test_owner_can_correct_one_failed_task_while_preserving_recovery_guards(recovery_env):
    env = recovery_env
    await patch(env, 'work_item', 'bad', payload={'change_expectation': 'Inherited wrong-stage instruction'})
    before = await env.store.list('budget_account')
    request = {'expected_revision': 1, 'mode': 'retry', 'work_item_id': 'bad',
               'task_correction': 'Research every planned market segment.', 'reason': 'Repair wrong-stage instruction.'}
    receipt = await env.service.recover('run', request, 'correct-scope')
    work = await env.store.read('work_item', 'bad')
    assert work['payload']['change_expectation'] == request['task_correction']
    assert receipt['task_correction'] == request['task_correction'] and receipt['actor'] == 'owner'
    assert work['approval_required'] and work['write_paths'] == []
    assert (await env.store.list('budget_account')) == before
    assert 'change_expectation' not in (await env.store.read('work_item', 'after'))['payload']
    assert await env.service.recover('run', request, 'correct-scope') == receipt


@pytest.mark.parametrize('options', [{'mode': 'continue'}, {'work_item_id': None}, {'task_correction': '  '}])
async def test_correction_requires_an_explicit_single_retry_target(recovery_env, options):
    env = recovery_env
    request = {'expected_revision': 1, 'mode': 'retry', 'work_item_id': 'bad',
               'task_correction': 'Correct task instructions.', **options}
    before = await env.store.list('work_item')
    with pytest.raises(DomainError):
        await env.service.recover('run', request, str(uuid4()))
    assert await env.store.list('work_item') == before


def test_default_recovery_command_retains_legacy_idempotency_identity():
    value = _recovery_command('run', RecoveryRequest(expected_revision=1, mode='retry'))
    assert 'task_correction' not in value


async def test_owner_correction_keeps_partial_code_checkpoint_and_original_workspace(recovery_env):
    env = recovery_env
    path, _, _, _ = await stopped_workspace(env, project_layout=True)
    await patch(env, 'work_item', 'bad', payload={'change_expectation': 'Wrong inherited requirement.'})
    repository = env.service.repository
    original = repository._run(path, ['status', '--porcelain'])
    accounts = await env.store.list('budget_account')
    receipt = await env.service.recover('run', {'expected_revision': 1, 'mode': 'retry', 'work_item_id': 'bad',
        'task_correction': 'Keep the verified partial implementation and complete the original requirements.'}, 'scope-and-code')
    work = await env.store.read('work_item', 'bad')
    source, commit = await Scheduler(env.workflow, env.store, None, None, env.settings)._source(
        await env.store.read('run', 'run'), work)
    assert receipt['checkpoint']['kind'] == 'stopped_workspace'
    assert repository._run(source, ['show', commit + ':src/keep.js']) == b'export const valuable = 42;\n'
    assert repository._run(path, ['status', '--porcelain']) == original
    assert (path / 'src/keep.js').read_text() == 'export const valuable = 42;\n'
    assert work['write_paths'] == ['src'] and work['approval_required']
    assert await env.store.list('budget_account') == accounts


async def test_automatic_recovery_cannot_replace_task_correction(recovery_env):
    env = recovery_env
    before = await env.store.list('work_item')
    with pytest.raises(DomainError, match='纠正任务指令'):
        await env.service._recover('run', {'expected_revision': 1, 'mode': 'retry', 'work_item_id': 'bad',
            'task_correction': 'An automatic actor may not replace scope.'}, 'automatic-correction', analysis_id='analysis')
    assert await env.store.list('work_item') == before
