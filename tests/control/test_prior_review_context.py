"""A review facet rechecks its own prior failure after a sibling triggers repair."""
import json
from uuid import uuid4

import pytest
from test_parallel_remediation import complete_repair, finding, update
from test_parallel_remediation import parallel_env as parallel_env
from test_parallel_review_child_remediation import collect_review
from test_stage_context import context_system as context_system
from test_stage_context import work

from agentflow.control.scheduler import Scheduler
from agentflow.control.stage_context import StageContext
from agentflow.domain.expansion import StageExpander


async def prior_result(store, identity, *, generation=1, quality='failed', run_id='run', work_id='review',
                       findings=None, attempt_changes=None, dispatch_changes=None, with_dispatch=False):
    findings = findings if findings is not None else [finding('b')] if quality == 'failed' else []
    def seed(tx):
        attempt = {'run_id': run_id, 'work_item_id': work_id, 'generation': generation,
            'fencing_token': generation, 'input_fingerprint': 'input-' + identity,
            'status': 'completed', 'quality_result': quality, **(attempt_changes or {})}
        tx.put('attempt', identity, attempt)
        tx.put('review', identity, {'run_id': run_id, 'work_item_id': work_id, 'generation': generation,
            'reviewer_id': identity, 'reviewed_commit': 'a' * 40, 'quality_result': quality,
            'blocking_findings': findings, 'stale': True})
        if with_dispatch:
            tx.put('dispatch_context', identity, {'task': {'attempt_id': identity, 'run_id': run_id,
                'work_item_id': work_id, 'fencing_token': generation, 'input_fingerprint': 'input-' + identity,
                'step': 'code_review', 'source_commit': 'a' * 40, **(dispatch_changes or {})}})
        return {}
    await store.command('fixture.prior-review', identity, {}, seed)


async def history_context(context_system, *, generation=3):
    store, artifacts, settings = context_system
    item = work('review', 'code_review', generation=generation, artifact_ids=[])
    return await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, item, {}, {item['id']: item})


def prior_documents(context):
    return [entry for entry in context['documents'] if entry.get('evidence_kind') == 'prior_review']


async def test_other_failed_facet_keeps_its_own_findings_after_first_facet_repairs(parallel_env):
    env = parallel_env
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'code_review', 'unit_test_plan'],
        work_specs=[{'key': 'code_review', 'step': 'code_review', 'role': 'review'}])
    parent = await update(env.store, 'work_item', 'review-work', status='pending', quality_result='unknown',
                          attempt_id=None, output_fingerprint=None)
    await update(env.store, 'review', 'review-attempt-1', stale=True)
    expanded = await StageExpander(env.store).expand('run', 'review-work', [
        {'key': 'tests', 'goal': 'Inspect workspace invariants', 'write_paths': [], 'review_focus': 'current_code'},
        {'key': 'product', 'goal': 'Inspect user navigation', 'write_paths': [], 'review_focus': 'current_code'}],
        str(uuid4()), parent['revision'])
    ids = {row['expansion_key']: row['id'] for row in expanded['items']}
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    claims = {claim['work_item']['id']: claim for claim in claims}
    first = await collect_review(env, claims[ids['tests']], [finding('a')])
    other = await collect_review(env, claims[ids['product']], [finding('a'), finding('b')])
    prior = await env.store.read('review', other['attempt_id'])
    repair = await env.remediation.repair(first['id'])
    assert repair['batch_repair_work_item_ids'] == ['module-a', 'module-b']
    assert (await env.store.read('review', prior['id']))['stale']
    assert 'Correct module b' in (await env.store.read('work_item', other['id']))['payload']['change_expectation']
    first_expectation = (await env.store.read('work_item', first['id']))['payload']['change_expectation']
    assert 'Correct module a' in first_expectation and 'Correct module b' not in first_expectation
    assert 'current review phase contract takes precedence' in first_expectation
    await complete_repair(env, ['a', 'b'], 1)
    item = await env.store.read('work_item', other['id'])
    run = await env.store.read('run', 'run')
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    _, commit = await scheduler._source(run, item)
    await update(env.store, 'run', 'run', goal='Preserve all reviewed behavior')
    run = await env.store.read('run', 'run')
    before = await env.store.list('review')
    prompt = await scheduler._prompt(run, item, commit)
    assert 'Correct module b behavior' in prompt
    context = await scheduler.stage_context.build(run, item, await env.store.read('plan', 'plan'),
        {row['id']: row for row in await env.store.list('work_item')})
    documents = prior_documents(context)
    assert len(documents) == 1 and documents[0]['review_id'] == prior['id']
    evidence = json.loads((context['directory'] / documents[0]['file']).read_text())
    assert evidence['reviews'][0]['blocking_findings'] == [finding('a'), finding('b')]
    assert evidence['reviews'][0]['source_commit'] == prior['reviewed_commit']
    assert evidence['reviews'][0]['generation'] == 1
    assert evidence['requires_current_source_recheck'] is True
    assert await env.store.list('review') == before


async def test_latest_accepted_pass_stops_repeating_an_older_failure(context_system):
    store, _, _ = context_system
    await prior_result(store, 'old-failed', generation=1)
    await prior_result(store, 'latest-passed', generation=2, quality='passed')
    assert not prior_documents(await history_context(context_system))


async def test_consecutive_failures_keep_distinct_issues_but_isolate_run_and_work(context_system):
    store, _, _ = context_system
    await prior_result(store, 'old-failed', generation=1, findings=[finding('a')])
    await prior_result(store, 'latest-failed', generation=2, findings=[finding('b')], with_dispatch=True)
    await prior_result(store, 'other-work', generation=2, work_id='other', findings=[finding('c')])
    await prior_result(store, 'other-run', generation=2, run_id='other', findings=[finding('d')])
    await prior_result(store, 'current-generation', generation=3, findings=[finding('e')])
    result = await history_context(context_system)
    docs = prior_documents(result)
    assert len(docs) == 1 and docs[0]['review_id'] == 'latest-failed'
    data = json.loads((result['directory'] / docs[0]['file']).read_text())
    assert [(row['review_id'], row['blocking_findings']) for row in data['reviews']] == [
        ('old-failed', [finding('a')]), ('latest-failed', [finding('b')])]


@pytest.mark.parametrize('attempt_changes,dispatch_changes', [
    ({'status': 'blocked'}, {}), ({'quality_result': 'passed'}, {}), ({'run_id': 'other'}, {}),
    ({'work_item_id': 'other'}, {}), ({'generation': 99}, {}),
    ({}, {'source_commit': 'b' * 40}), ({}, {'fencing_token': 99}),
    ({}, {'input_fingerprint': 'different'}), ({}, {'attempt_id': 'different'}),
])
async def test_unaccepted_or_mismatched_review_records_do_not_become_history(context_system, attempt_changes, dispatch_changes):
    store, _, _ = context_system
    await prior_result(store, 'invalid-history', with_dispatch=True,
        attempt_changes=attempt_changes, dispatch_changes=dispatch_changes)
    assert not prior_documents(await history_context(context_system))


async def test_large_prior_review_stays_complete_and_its_phase_never_changes(context_system):
    store, artifacts, settings = context_system
    findings = [{'severity': 'blocking', 'path': 'tests/unit.test.mjs',
                 'description': 'OLD FUTURE PLACEHOLDER OPINION ' + 'evidence ' * 6000}]
    await prior_result(store, 'old-tests-opinion', findings=findings)
    rows = [work('impl', 'implementation', artifact_ids=[]),
        work('review', 'code_review', ['impl'], generation=2, artifact_ids=[], key='code_review'),
        work('future-tests', 'unit_test_implementation', ['review'], artifact_ids=[])]
    context = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, rows[1], {}, {row['id']: row for row in rows})
    docs = prior_documents(context)
    assert len(docs) == 1
    file = context['directory'] / docs[0]['file']
    assert json.loads(file.read_text())['reviews'][0]['blocking_findings'] == findings
    assert file.stat().st_mode & 0o222 == 0
    assert len(context['text']) < 16000
    assert context['review_phase_contract']['required_test_phases'] == []
    assert context['review_phase_contract']['deferred_test_phases'] == ['unit']
    assert 'current review phase contract takes precedence' in context['text']
    assert 'not an automatic failed result' in context['text']


async def test_passed_review_is_the_boundary_for_later_failed_followups(context_system):
    store, _, _ = context_system
    await prior_result(store, 'cleared-failure', generation=1, findings=[finding('a')])
    await prior_result(store, 'accepted-pass', generation=2, quality='passed', with_dispatch=True)
    await prior_result(store, 'new-failure', generation=3, findings=[finding('b')])
    await prior_result(store, 'latest-failure', generation=4, findings=[finding('c')])
    result = await history_context(context_system, generation=5)
    docs = prior_documents(result)
    assert len(docs) == 1
    data = json.loads((result['directory'] / docs[0]['file']).read_text())
    assert data['since_passed_review_id'] == 'accepted-pass'
    assert [(row['review_id'], row['blocking_findings']) for row in data['reviews']] == [
        ('new-failure', [finding('b')]), ('latest-failure', [finding('c')])]
