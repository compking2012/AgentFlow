"""Multiple failed review facets over one coding owner keep unique source receipts."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from test_parallel_remediation import CollectedFixtureRuntime, update
from test_parallel_review_child_remediation import collect_review
from test_remediation import env as env
from test_review_baseline_recovery import stopped_task

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.recovery import RunRecoveryService, validate_recovery_checkpoint
from agentflow.control.review_checkpoint import validate_review_repair_source
from agentflow.control.scheduler import Scheduler
from agentflow.domain.expansion import StageExpander


@pytest_asyncio.fixture
async def single_source(env, tmp_path):
    env.temporary = tmp_path
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    env.workflow.settings = env.settings
    await update(env.store, 'work_item', 'code', kind='stage', approval_required=False)
    parent = await update(env.store, 'work_item', 'review-work', status='pending', quality_result='unknown',
                          approval_required=False, attempt_id=None, output_fingerprint=None)
    await update(env.store, 'review', 'review-attempt', stale=True)
    source = await env.store.read('code_snapshot', 'failed-snapshot')
    await update(env.store, 'code_snapshot', 'code-attempt',
                 **{key: value for key, value in source.items() if key not in {'id', 'revision'}})
    await update(env.store, 'code_snapshot', 'failed-snapshot', stale=True)
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'code_review', 'unit_test_plan'],
                 work_specs=[{'key': 'code_review', 'step': 'code_review', 'role': 'review'}])
    expanded = await StageExpander(env.store).expand('run', parent['id'], [
        {'key': key, 'goal': 'Review the same complete source', 'write_paths': [], 'review_focus': 'current_code'}
        for key in ('product', 'tests')], str(uuid4()), parent['revision'])
    env.facet_ids = set(expanded['work_item_ids'])
    return env


async def failed_facets(env, round_number):
    claims = [await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4())) for _ in range(2)]
    assert {claim['work_item']['id'] for claim in claims} == env.facet_ids
    findings = [{'path': 'product.py', 'severity': 'blocking',
                 'description': f'Round {round_number}: independent correction {index}'} for index in (1, 2)]
    facets = [await collect_review(env, claim, [finding]) for claim, finding in zip(claims, findings, strict=True)]
    assert all(facet['status'] == 'completed' and facet['quality_result'] == 'failed' for facet in facets)
    return facets, findings


async def assert_batch_sources(env, facets, findings, generation, source_id):
    owner = await env.store.read('work_item', 'code')
    run = await env.store.read('run', 'run')
    source = await env.store.read('code_snapshot', source_id)
    assert owner['generation'] == generation + 1 and owner['status'] == 'pending'
    assert owner['write_paths'] == ['.'] and owner['kind'] == 'stage'
    assert owner['attempt_id'] is None
    records = [row for row in await env.store.list('review_repair')
               if row['review_attempt_id'] in {facet['attempt_id'] for facet in facets}]
    assert len(records) == 2 and len({row['batch_id'] for row in records}) == 1
    assert all(row['batch_repair_work_item_ids'] == ['code'] for row in records)
    assert all(row['repair_work_item_ids'] == ['code'] and row['mode'] == 'single_source' for row in records)
    aliases = [row['checkpoint_alias_ids']['code'] for row in records]
    assert len(set(aliases)) == 2 and owner['payload']['repair_base_snapshot_id'] in aliases
    all_receipts = await env.store.list('review_repair')
    for receipt in records:
        alias = await env.store.read('code_snapshot', receipt['checkpoint_alias_ids']['code'])
        assert sum(row.get('checkpoint_alias_ids', {}).get('code') == alias['id'] for row in all_receipts) == 1
        assert alias['checkpoint_kind'] == 'reviewed_source_repair'
        assert alias['source_snapshot_id'] == alias['source_child_snapshot_id'] == source_id
        assert alias['generation'] == generation
        assert alias['commit_oid'] == source['commit_oid']
        assert alias['source_review_id'] == receipt['review_attempt_id']
        authority = await validate_review_repair_source(env.store, run, owner, alias)
        assert authority['receipt']['id'] == receipt['id']
        assert authority['review']['id'] == receipt['review_attempt_id']
    instruction = owner['payload']['change_expectation']
    for facet, finding in zip(facets, findings, strict=True):
        assert instruction.count(finding['description']) == 1
        assert facet['attempt_id'] in instruction
        receipt = next(row for row in records if row['review_attempt_id'] == facet['attempt_id'])
        assert receipt['findings_by_work_item']['code'] == [finding]
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    path, commit = await scheduler._source(run, owner)
    assert commit == source['commit_oid'] and path == Path(source['repository_path'])
    assert (path / 'keep.txt').read_text() == 'valuable prior implementation\n'
    return owner


async def complete_owner(env, round_number):
    claim = await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'code'
    # Even two source review receipts authorize exactly one coding attempt.
    assert (await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4())))['attempt'] is None
    work, attempt = claim['work_item'], claim['attempt']
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], work)
    path = env.temporary / ('single-owner-' + attempt['id'])
    await env.repository.clone_snapshot(source, path, commit)
    (path / 'product.py').write_text(f'REVIEW_ROUND = {round_number}\n\ndef total(a, b):\n    return a + b\n')
    await scheduler._execute_existing({'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run',
        'step': 'implementation', 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(path), 'source_commit': commit, 'allowed_write_paths': work['write_paths']})
    assert (await env.store.read('work_item', 'code'))['status'] == 'completed'
    snapshot = await env.store.read('code_snapshot', attempt['id'])
    assert (path / 'keep.txt').read_text() == 'valuable prior implementation\n'
    changes = await env.repository.collect_diff(path, commit)
    assert {change['path'] for change in changes['changes']} == {'product.py'}
    return snapshot


@pytest.mark.parametrize('automatic', [False, True], ids=['direct', 'failure-controller'])
async def test_single_owner_gets_both_findings_once_across_two_review_cycles(single_source, automatic):
    env = single_source
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    action = controller.repair if automatic else env.remediation.repair
    source_id, generation = 'code-attempt', 1
    budgets = await env.store.list('budget_account')
    for round_number in (1, 2):
        facets, findings = await failed_facets(env, round_number)
        attempts_before = len([row for row in await env.store.list('attempt') if row['work_item_id'] == 'code'])
        # Alternate the first contender; neither first-review ordering can omit its peer.
        contenders = facets if round_number == 1 else list(reversed(facets))
        await asyncio.gather(*(action(facet['id']) for facet in contenders))
        await assert_batch_sources(env, facets, findings, generation, source_id)
        revisions = [row for row in await env.store.list('work_revision')
                     if row['work_item_id'] == 'code' and row['snapshot']['generation'] == generation]
        assert len(revisions) == 1
        snapshot = await complete_owner(env, round_number)
        assert len([row for row in await env.store.list('attempt') if row['work_item_id'] == 'code']) == attempts_before + 1
        source_id, generation = snapshot['id'], generation + 1
    assert len(await env.store.list('review_repair')) == 4
    claims = [await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4())) for _ in range(2)]
    assert {claim['work_item']['id'] for claim in claims} == env.facet_ids
    for claim in claims:
        assert (await collect_review(env, claim, []))['quality_result'] == 'passed'
    parent = await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4()))
    assert parent['work_item']['id'] == 'review-work'
    assert (await collect_review(env, parent, []))['quality_result'] == 'passed'
    assert (await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4())))['work_item']['id'] == 'unit'
    assert await env.store.list('budget_account') == budgets


@pytest.mark.parametrize('started', [False, True], ids=['prelaunch', 'stopped-no-diff'])
async def test_single_source_batch_recovery_keeps_complete_alias_and_both_corrections(single_source, started):
    env = single_source
    facets, findings = await failed_facets(env, 1)
    assert await env.remediation.repair(facets[0]['id'])
    owner = await assert_batch_sources(env, facets, findings, 1, 'code-attempt')
    correction = owner['payload']['change_expectation']
    source_alias = owner['payload']['repair_base_snapshot_id']
    run = await env.store.read('run', 'run')
    heads = None
    if started:
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
        source, commit = await scheduler._source(run, owner)
        claim = await env.workflow.claim_next('run', 'single-source-fixture', str(uuid4()))
        workspace, _ = await stopped_task(env, claim, source, commit, failed=True)
        heads = [await asyncio.to_thread(env.repository._run, workspace, args)
                 for args in (['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain'])]
        attempt = claim['attempt']
        await env.workflow.finish_attempt(attempt['id'], {'fencing_token': attempt['fencing_token'],
            'input_fingerprint': attempt['input_fingerprint'], 'execution_status': 'failed', 'quality_result': 'unknown',
            'runtime_failure_code': 'worker_timeout'}, str(uuid4()), verified_artifacts=[])
    else:
        await update(env.store, 'work_item', 'code', status='blocked')
    budgets = await env.store.list('budget_account')
    receipts = await env.store.list('review_repair')
    run = await env.store.read('run', 'run')
    recovery = RunRecoveryService(env.store, env.workflow)
    result = await recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': 'code', 'reason': 'Retry the unchanged reviewed source without losing either correction'}, str(uuid4()))
    owner = await env.store.read('work_item', 'code')
    assert owner['generation'] == 3 and owner['payload']['change_expectation'] == correction
    point = await env.store.read('code_snapshot', owner['payload']['recovery_checkpoint_id'])
    assert source_alias in {point.get('source_review_snapshot_id'), point.get('source_repair_snapshot_id')}
    await validate_recovery_checkpoint(env.store, result['run'], owner, point, env.repository)
    source, commit = await Scheduler(env.workflow, env.store, None, None, env.settings)._source(result['run'], owner)
    assert commit == env.snapshot['commit_oid']
    assert (source / 'keep.txt').read_text() == 'valuable prior implementation\n'
    assert (source / 'product.py').read_text().endswith('return a - b\n')
    assert await env.store.list('review_repair') == receipts
    assert await env.store.list('budget_account') == budgets
    if started:
        assert [await asyncio.to_thread(env.repository._run, workspace, args)
                for args in (['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain'])] == heads
    await complete_owner(env, 1)
