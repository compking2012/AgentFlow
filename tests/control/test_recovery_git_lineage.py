"""Recovery commits retain real ancestry across independent isolated Git clones."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_coding_steps import _stopped_coding_process_proof, execute, task_for
from test_coding_steps import coding_env as coding_env
from test_recovery import patch

from agentflow.common import DomainError
from agentflow.control.recovery import RunRecoveryService
from agentflow.runtime.workspace import WorkspaceManager


@pytest_asyncio.fixture
async def lineage_env(coding_env):
    env = coding_env
    env.settings = env.settings.model_copy(update={'max_coding_steps': 12})
    env.workflow.settings = env.settings
    env.scheduler.settings = env.settings
    env.scheduler.coding_steps.settings = env.settings
    env.recovery = RunRecoveryService(env.store, env.workflow)
    return env


async def saved_step(env, content):
    task = await task_for(env)
    await _stopped_coding_process_proof(env, task)
    result = await execute(env, task, {'summary': 'Verified source contribution', 'status': 'continue',
        'next_action': 'Complete the remaining assigned work'}, content, seconds=1, tools=1)
    assert result['status'] == 'pending'
    return task, await env.store.read('code_snapshot', task['attempt_id'])


async def stopped_without_edit(env):
    task = await task_for(env)
    path = await WorkspaceManager(env.settings.data_dir).create_clone(
        Path(task['workspace']), task['source_commit'], task['attempt_id'])
    task['workspace'] = str(path)
    await _stopped_coding_process_proof(env, task)
    async def invalid_final(_task):
        return {'execution_status': 'failed', 'runtime_failure_code': 'final_schema_invalid',
            'summary': 'Isolated invalid-final fixture; inherited files were not edited', 'artifacts': [],
            'active_seconds': 1, 'observed_tool_calls': 1, 'tool_observation_complete': True}
    env.scheduler.runtime = SimpleNamespace(execute_task=invalid_final)
    await env.scheduler._execute_existing(task)
    result = await env.store.read('work_item', 'code')
    assert result['status'] in {'failed', 'blocked'}
    assert not await env.store.read('code_snapshot', task['attempt_id'])
    assert not (await env.repository.collect_diff(path, task['source_commit']))['has_changes']
    assert not (path / '.git/objects/info/alternates').exists()
    return task


async def recover(env, key):
    run = await env.store.read('run', 'run')
    receipt = await env.recovery.recover('run', {'expected_revision': run['revision'],
        'mode': 'retry', 'work_item_id': 'code'}, key)
    snapshot = await env.store.read('code_snapshot', receipt['checkpoint']['snapshot_id'])
    return receipt, snapshot


def repository_state(env, path):
    return {'head': env.repository._run(path, ['rev-parse', 'HEAD']),
        'refs': env.repository._run(path, ['for-each-ref', '--format=%(refname) %(objectname)']),
        'index': (path / '.git/index').read_bytes(),
        'status': env.repository._run(path, ['status', '--porcelain']),
        'feature': (path / 'feature.py').read_bytes()}


async def assert_ancestry(env, snapshot, path=None, extra=()):
    path = Path(path or snapshot['repository_path'])
    assert env.repository._integrity(path, snapshot['commit_oid']) == snapshot['tree_oid']
    for ancestor in {snapshot['base_oid'], *snapshot['parent_commit_oids'], *extra}:
        assert await env.repository.contains_ancestor(path, ancestor, snapshot['commit_oid']), ancestor


async def test_two_unchanged_recoveries_keep_cumulative_diff_and_all_real_parents(lineage_env):
    env = lineage_env
    first, contribution = await saved_step(env, 'FIRST_TEST = True\n')
    previous = contribution['commit_oid']
    snapshots = []
    for round_number in range(2):
        stopped = await stopped_without_edit(env)
        path = Path(stopped['workspace'])
        assert stopped['source_commit'] == previous
        before = repository_state(env, path)
        budgets = await env.store.list('coding_work_budget')
        _, snapshot = await recover(env, f'unchanged-recovery-{round_number}')
        assert snapshot['base_oid'] == first['source_commit']
        assert snapshot['tree_oid'] == contribution['tree_oid']
        assert env.repository._run(path, ['show', '-s', '--format=%P', snapshot['commit_oid']]).decode().split() == [previous]
        await assert_ancestry(env, snapshot, extra=[contribution['commit_oid']])
        assert repository_state(env, path) == before
        assert await env.store.list('coding_work_budget') == budgets
        snapshots.append(snapshot)
        previous = snapshot['commit_oid']
    final_clone = env.root / 'final-independent-clone'
    await env.repository.clone_snapshot(Path(snapshots[-1]['repository_path']), final_clone, previous)
    assert not (final_clone / '.git/objects/info/alternates').exists()
    assert (final_clone / 'feature.py').read_text() == 'FIRST_TEST = True\n'
    await assert_ancestry(env, snapshots[-1], final_clone,
                          extra=[contribution['commit_oid'], snapshots[0]['commit_oid']])


async def legacy_case(env, *, outside_scope=False):
    first, first_snapshot = await saved_step(env, 'FIRST_TEST = True\n')
    _, source_snapshot = await saved_step(env, 'FIRST_TEST = True\nSECOND_TEST = True\n')
    original_attempt = await stopped_without_edit(env)
    original_path = Path(original_attempt['workspace'])
    receipt, original_record = await recover(env, 'fixture-original-recovery')
    if outside_scope:
        (original_path / 'outside.py').write_text('UNAUTHORIZED = True\n')
    # Simulate the old bug only in this fixture: the whole tree is committed
    # directly to the cumulative base, so its previous source objects are not reachable.
    legacy = await env.repository.freeze_workspace(original_path, first['source_commit'], 'Fixture historical base-parent recovery')
    if outside_scope:
        (original_path / 'outside.py').unlink()
    legacy_record = await patch(env, 'code_snapshot', original_record['id'], commit_oid=legacy['commit_oid'],
        tree_oid=legacy['tree_oid'], base_oid=first['source_commit'],
        parent_commit_oids=[first['source_commit'], first_snapshot['commit_oid'], source_snapshot['commit_oid']])
    old_receipt = await env.store.read('run_recovery', receipt['id'])
    legacy_receipt = await patch(env, 'run_recovery', receipt['id'],
        checkpoint={**old_receipt['checkpoint'], 'commit_oid': legacy['commit_oid']})
    current = await stopped_without_edit(env)
    current_path = Path(current['workspace'])
    assert current['source_commit'] == legacy['commit_oid']
    with pytest.raises(DomainError):
        env.repository._commit(current_path, source_snapshot['commit_oid'])
    assert not await env.repository.contains_ancestor(current_path, source_snapshot['commit_oid'], legacy['commit_oid'])
    # The original source remains independently verifiable in its registered repository.
    assert await env.repository.contains_ancestor(original_path, first_snapshot['commit_oid'], source_snapshot['commit_oid'])
    return {'first': first, 'first_snapshot': first_snapshot, 'source_snapshot': source_snapshot,
        'original_attempt': original_attempt, 'original_path': original_path, 'legacy': legacy_record,
        'receipt': legacy_receipt, 'current': current, 'current_path': current_path}


async def test_legacy_flattened_snapshot_gets_a_new_two_parent_commit_without_rewriting_history(lineage_env):
    env = lineage_env
    case = await legacy_case(env)
    original_state = repository_state(env, case['original_path'])
    current_state = repository_state(env, case['current_path'])
    legacy_bytes = env.repository._run(case['original_path'], ['cat-file', 'commit', case['legacy']['commit_oid']])
    budgets = await env.store.list('coding_work_budget')
    _, repaired = await recover(env, 'repair-legacy-lineage')
    assert repaired['commit_oid'] != case['legacy']['commit_oid']
    assert repaired['tree_oid'] == case['legacy']['tree_oid']
    assert repaired['base_oid'] == case['first']['source_commit']
    parents = env.repository._run(case['current_path'], ['show', '-s', '--format=%P', repaired['commit_oid']]).decode().split()
    assert parents == [case['legacy']['commit_oid'], case['source_snapshot']['commit_oid']]
    await assert_ancestry(env, repaired, extra=[case['first_snapshot']['commit_oid']])
    assert await env.store.read('code_snapshot', case['legacy']['id']) == case['legacy']
    assert await env.store.read('run_recovery', case['receipt']['id']) == case['receipt']
    assert repository_state(env, case['original_path']) == original_state
    assert repository_state(env, case['current_path']) == current_state
    assert env.repository._run(case['original_path'], ['cat-file', 'commit', case['legacy']['commit_oid']]) == legacy_bytes
    assert await env.store.list('coding_work_budget') == budgets
    assert repaired['lineage_repairs'][0]['original_source_commit'] == case['source_snapshot']['commit_oid']
    clone = env.root / 'repaired-independent-clone'
    await env.repository.clone_snapshot(case['current_path'], clone, repaired['commit_oid'])
    assert not (clone / '.git/objects/info/alternates').exists()
    await assert_ancestry(env, repaired, clone,
        extra=[case['legacy']['commit_oid'], case['source_snapshot']['commit_oid'], case['first_snapshot']['commit_oid']])
    assert (clone / 'feature.py').read_text() == 'FIRST_TEST = True\nSECOND_TEST = True\n'


@pytest.mark.parametrize('damage', ['forged_receipt', 'outside_scope', 'unrelated_declared_parent'])
async def test_legacy_lineage_repair_rejects_unproved_authority_changes_or_parent_claims(lineage_env, damage):
    env = lineage_env
    case = await legacy_case(env, outside_scope=damage == 'outside_scope')
    if damage == 'forged_receipt':
        await patch(env, 'run_recovery', case['receipt']['id'], actor='system')
    elif damage == 'unrelated_declared_parent':
        unrelated = await env.repository.freeze_workspace(case['original_path'], case['first']['source_commit'],
                                                          'Unrelated fixture commit with the same tree')
        assert not await env.repository.contains_ancestor(case['original_path'], unrelated['commit_oid'],
                                                          case['source_snapshot']['commit_oid'])
        await patch(env, 'code_snapshot', case['legacy']['id'],
                    parent_commit_oids=[*case['legacy']['parent_commit_oids'], unrelated['commit_oid']])
    kinds = ('run', 'work_item', 'attempt', 'code_snapshot', 'run_recovery', 'coding_work_budget', 'budget_account')
    records = {kind: await env.store.list(kind) for kind in kinds}
    original_state = repository_state(env, case['original_path'])
    current_state = repository_state(env, case['current_path'])
    with pytest.raises(DomainError) as error:
        await recover(env, 'reject-unproven-lineage')
    assert error.value.code == ('write_scope_violation' if damage == 'outside_scope' else 'recovery_checkpoint_invalid')
    assert {kind: await env.store.list(kind) for kind in kinds} == records
    assert repository_state(env, case['original_path']) == original_state
    assert repository_state(env, case['current_path']) == current_state
