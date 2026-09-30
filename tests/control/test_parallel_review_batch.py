"""Current sealed review facets contribute distinct findings to one physical repair."""
from uuid import uuid4

from test_parallel_remediation import complete_repair, finding, update
from test_parallel_remediation import parallel_env as parallel_env
from test_parallel_review_child_remediation import collect_review

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.recovery import RunRecoveryService, _ReadState
from agentflow.control.remediation import review_repair_target
from agentflow.control.review_checkpoint import validate_review_repair_source
from agentflow.control.scheduler import Scheduler
from agentflow.domain.expansion import StageExpander


async def prepare_facets(env):
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'code_review', 'unit_test_plan'],
        work_specs=[{'key': 'code_review', 'step': 'code_review', 'role': 'review'}])
    parent = await update(env.store, 'work_item', 'review-work', status='pending', quality_result='unknown',
                          attempt_id=None, output_fingerprint=None)
    await update(env.store, 'review', 'review-attempt-1', stale=True)
    expanded = await StageExpander(env.store).expand('run', parent['id'], [
        {'key': name, 'goal': 'Review current code', 'write_paths': [], 'review_focus': 'current_code'}
        for name in ('facet-a', 'facet-b')], str(uuid4()), parent['revision'])
    return expanded['work_item_ids']


async def finish_facets(env, issues):
    results = []
    for findings in issues:
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        results.append(await collect_review(env, claim, findings))
    return results


async def test_failed_facet_waits_for_pending_peer_without_invalidating_it(parallel_env):
    env = parallel_env
    ids = await prepare_facets(env)
    first = (await finish_facets(env, [[finding('a')]]))[0]
    peer = next(identity for identity in ids if identity != first['id'])
    assert (await env.store.read('work_item', peer))['status'] == 'pending'
    before = await env.store.list('work_item')
    assert await env.remediation.repair(first['id']) is None
    assert not await env.store.list('review_repair')
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    waiting = await controller.repair(first['id'])
    assert waiting['status'] == 'blocked'
    assert 'review_peers_incomplete' in {row['code'] for row in waiting['blockers']}
    assert first['id'] in controller._deferred
    assert await env.store.list('work_item') == before
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == peer


async def test_same_source_failed_facets_union_owners_once_with_per_review_alias_provenance(parallel_env):
    env = parallel_env
    await prepare_facets(env)
    first, second = await finish_facets(env, [[finding('a'), finding('b')], [finding('b'), finding('c')]])
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    target = review_repair_target(_ReadState(state), state['run'][0], first)
    assert target['root_work_item_ids'] == ['module-a', 'module-b', 'module-c']
    original_d = await env.store.read('work_item', 'module-d')
    result = await env.remediation.repair(first['id'])
    assert result is not None
    receipts = await env.store.list('review_repair')
    assert len(receipts) == 2 and len({row['batch_id'] for row in receipts}) == 1
    by_review = {row['review_attempt_id']: row for row in receipts}
    assert set(by_review) == {first['attempt_id'], second['attempt_id']}
    assert by_review[first['attempt_id']]['repair_work_item_ids'] == ['module-a', 'module-b']
    assert by_review[second['attempt_id']]['repair_work_item_ids'] == ['module-b', 'module-c']
    assert all(row['batch_repair_work_item_ids'] == ['module-a', 'module-b', 'module-c'] for row in receipts)
    run = await env.store.read('run', 'run')
    for identity in ('module-a', 'module-b', 'module-c'):
        work = await env.store.read('work_item', identity)
        assert work['generation'] == 2 and work['status'] == 'pending'
        assert work['write_paths'] == [env.paths[identity]]
        aliases = [row['checkpoint_alias_ids'][identity] for row in receipts if identity in row['checkpoint_alias_ids']]
        assert work['payload']['repair_base_snapshot_id'] in aliases
        for alias_id in aliases:
            alias = await env.store.read('code_snapshot', alias_id)
            authority = await validate_review_repair_source(env.store, run, work, alias)
            assert authority['review']['id'] == alias['source_review_id']
        if identity == 'module-b':
            assert len(set(aliases)) == 2
            assert first['attempt_id'] in work['payload']['change_expectation']
            assert second['attempt_id'] in work['payload']['change_expectation']
    assert await env.store.read('work_item', 'module-d') == original_d
    assert await env.remediation.repair(second['id']) is None
    aggregate = await complete_repair(env, ['a', 'b', 'c'], 1)
    first2, second2 = await finish_facets(env, [[finding('a')], [finding('d')]])
    assert first2['generation'] == first['generation'] + 1
    assert second2['generation'] == second['generation'] + 1
    again = await env.remediation.repair(second2['id'])
    assert again is not None and again['batch_repair_work_item_ids'] == ['module-a', 'module-d']
    assert len(await env.store.list('review_repair')) == 4
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    run = await env.store.read('run', 'run')
    for identity in ('module-a', 'module-d'):
        work = await env.store.read('work_item', identity)
        source, commit = await scheduler._source(run, work)
        assert commit == aggregate['commit_oid']
        assert all((source / path).is_file() for path in env.paths.values())
        alias = await env.store.read('code_snapshot', work['payload']['repair_base_snapshot_id'])
        await validate_review_repair_source(env.store, run, work, alias)


async def test_parallel_reviews_of_single_producer_have_unique_source_aliases(parallel_env):
    env = parallel_env
    await update(env.store, 'work_item', 'implementation', kind='stage', dependencies=[], write_paths=['src'])
    await prepare_facets(env)
    first, second = await finish_facets(env, [[finding('a')], [finding('b')]])
    result = await env.remediation.repair(first['id'])
    assert result is not None and result['batch_repair_work_item_ids'] == ['implementation']
    work = await env.store.read('work_item', 'implementation')
    assert work['generation'] == 2
    receipts = await env.store.list('review_repair')
    assert len(receipts) == 2
    aliases = [row['checkpoint_alias_ids']['implementation'] for row in receipts]
    assert len(set(aliases)) == 2 and work['payload']['repair_base_snapshot_id'] in aliases
    run = await env.store.read('run', 'run')
    for alias_id in aliases:
        alias = await env.store.read('code_snapshot', alias_id)
        assert alias['checkpoint_kind'] == 'reviewed_source_repair'
        proof = await validate_review_repair_source(env.store, run, work, alias)
        assert proof['review']['id'] == alias['source_review_id']
    await update(env.store, 'work_item', work['id'], status='blocked')
    recovery = RunRecoveryService(env.store, env.workflow)
    await recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': work['id']}, 'single-source-batch-prelaunch-retry')
    work = await env.store.read('work_item', work['id'])
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    source, commit = await scheduler._source(await env.store.read('run', 'run'), work)
    assert commit == env.aggregate['commit_oid']
    assert all((source / path).is_file() for path in env.paths.values())
