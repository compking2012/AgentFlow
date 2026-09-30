"""Owner source repair across authenticated product and test-runtime repair history."""
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from test_owner_review_source_repair import PATHS, invoke, payload
from test_owner_review_source_repair import env as env
from test_parallel_remediation import CollectedFixtureRuntime, update
from test_review_baseline_recovery import stopped_task

from agentflow.common import DomainError, canonical_digest
from agentflow.control.scheduler import Scheduler
from agentflow.execution.manifests import SourceManifest


async def collect(env, claim, path, text):
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    if path:
        (workspace / path).write_text(text)
    await scheduler._execute_existing(task)
    return await env.store.read('code_snapshot', claim['attempt']['id'])


async def append_dynamic(env, parent, kind, number):
    old = await env.store.read('code_snapshot', parent['attempt_id'])
    identity = kind + '-' + str(number)
    reviewer_id = 'review' if kind == 'runtime' else identity + '-review'
    scopes = ['tests/web.spec.mjs'] if kind == 'runtime' else ['src', 'public']
    step, role = ('integration_test_implementation', 'integration_test') if kind == 'runtime' else ('implementation', 'development')
    manifest = SourceManifest(source_commit=old['commit_oid'], source_tree_oid=old['tree_oid'],
        source_bundle_artifact_version_id='bundle', source_bundle_digest='sha256:' + 'a' * 64,
        test_package_artifact_version_id='tests', test_package_digest='sha256:' + 'b' * 64,
        build_plan_artifact_version_id='build', build_plan_digest='sha256:' + 'c' * 64,
        target_matrix_fingerprint='sha256:' + 'd' * 64, required_app_targets=('web',))
    candidate = await update(env.store, 'candidate', identity + '-candidate', run_id='run', run_input_fingerprint='prior-input-' + identity,
        source_commit=old['commit_oid'], tree_oid=old['tree_oid'], source_repository=old['repository_path'],
        source_manifest={**manifest.model_dump(mode='json'), 'fingerprint': manifest.fingerprint})
    await update(env.store, 'code_snapshot', identity + '-base', run_id='run', work_item_id=identity, generation=0,
        repository_path=old['repository_path'], commit_oid=old['commit_oid'], tree_oid=old['tree_oid'], base_oid=old['commit_oid'], stale=False)
    fields = {k: v for k, v in parent.items() if k not in {'id', 'revision', 'output_fingerprint', 'approved_fingerprint'}}
    fields.update(key=('test-runtime-repair-' if kind == 'runtime' else 'product-repair-') + identity,
        step=step, role=role, dependencies=[parent['id']], generation=1, status='completed', attempt_id=identity + '-attempt',
        write_paths=scopes, fencing_token=1, artifact_ids=[], payload={'repair_base_snapshot_id': identity + '-base',
            **({'test_runtime_repair_id': identity} if kind == 'runtime' else {'product_frozen_repair': True})})
    work = await update(env.store, 'work_item', identity, **fields)
    attempt = await update(env.store, 'attempt', work['attempt_id'], run_id='run', iteration_id='iteration', work_item_id=identity,
        status='completed', generation=1, fencing_token=1, input_fingerprint=work['input_fingerprint'])
    workspace, _ = await stopped_task(env, {'work_item': work, 'attempt': attempt}, old['repository_path'], old['commit_oid'], failed=False)
    changed = 'tests/web.spec.mjs' if kind == 'runtime' else PATHS[0]
    (workspace / changed).write_text(f'export const changed{number} = {number};\n')
    frozen = await env.repository.freeze_workspace(workspace, old['commit_oid'], identity)
    await update(env.store, 'code_snapshot', attempt['id'], run_id='run', work_item_id=identity, generation=1,
        repository_path=str(workspace), commit_oid=frozen['commit_oid'], tree_oid=frozen['tree_oid'], base_oid=old['commit_oid'], stale=False)
    if kind == 'runtime':
        request = {'product_id': 'product', 'candidate_id': candidate['id'], 'reason': 'Fix the exact locator without changing assertions.'}
        await update(env.store, 'product_test_runtime_repair', identity, run_id='run', product_id='product', actor='owner',
            candidate_id=candidate['id'], source_commit=old['commit_oid'], source_tree_oid=old['tree_oid'],
            repair_work_item_id=identity, review_work_item_id=reviewer_id, phase='integration', write_paths=scopes,
            request=request, request_fingerprint=canonical_digest(request),
            evidence=[{'artifact': {'state': 'complete', 'digest': 'sha256:' + 'e' * 64},
                'report': {'raw_digest': 'sha256:' + 'e' * 64, 'execution_status': 'completed', 'quality_result': 'failed',
                    'cases': [{'status': 'failed'}]}}])
    else:
        await update(env.store, 'work_item', reviewer_id, **{**fields, 'key': 'product-repair-review-' + reviewer_id,
            'step': 'code_review', 'role': 'review', 'dependencies': [identity], 'write_paths': [], 'attempt_id': None,
            'status': 'completed', 'quality_result': 'passed', 'payload': {}})
        await update(env.store, 'product_test_repair', identity, run_id='run', product_id='product', candidate_id=candidate['id'],
            preserved_test_source=old['commit_oid'], repair_work_item_id=identity, review_work_item_id=reviewer_id,
            affected_work_item_ids=['execution'], ordinal=number)
    return work


@pytest_asyncio.fixture
async def chain(env):
    await update(env.store, 'run', 'run', goal='Preserve completed product and test behavior')
    await update(env.store, 'plan', 'plan', product_contract={'stack': 'node_web_api', 'product_id': 'product'})
    first = await invoke(env)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    await collect(env, claim, PATHS[0], 'export const owner = 1;\n')
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    await collect(env, claim, None, None)
    await update(env.store, 'work_item', 'execution', status='blocked')
    tip = await env.store.read('work_item', first['repair_work_item_id'])
    tip = await append_dynamic(env, tip, 'product', 1)
    tip = await append_dynamic(env, tip, 'product', 2)
    tip = await append_dynamic(env, tip, 'runtime', 3)
    review = await env.store.read('work_item', 'review')
    await update(env.store, 'work_item', 'review', dependencies=[tip['id']], status='pending', quality_result='unknown',
        attempt_id=None, payload={'test_runtime_repair_id': tip['id']}, generation=review['generation'] + 1)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, [
        {'severity': 'blocking', 'path': PATHS[0], 'description': 'The original product defect remains'}]), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    _, task = await stopped_task(env, claim, source, commit, failed=False)
    await scheduler._execute_existing(task)
    env.dynamic_tip, env.dynamic_commit = tip, commit
    return env


async def test_owner_can_repair_original_source_after_two_product_and_runtime_repairs(chain):
    env = chain
    preserved = {kind: await env.store.list(kind) for kind in ('product_test_repair', 'product_test_runtime_repair', 'budget_account', 'attempt')}
    original = await env.store.read('work_item', env.dynamic_tip['id'])
    result = await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='owner-after-runtime')
    assert result['source_commit'] == env.dynamic_commit
    assert result['producer_work_item_id'] == env.dynamic_tip['id']
    assert {kind: await env.store.list(kind) for kind in preserved} == preserved
    assert await env.store.read('work_item', env.dynamic_tip['id']) == original
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    snapshot = await collect(env, claim, PATHS[0], 'export const sourceFixed = true;\n')
    assert (Path(snapshot['repository_path']) / 'tests/web.spec.mjs').read_text() == 'export const changed3 = 3;\n'
    review_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    _, commit = await scheduler._source(review_claim['run'], review_claim['work_item'])
    prompt = await scheduler._prompt(review_claim['run'], review_claim['work_item'], commit)
    assert 'Controller test-runtime review contract' not in prompt
    assert commit in prompt


@pytest.mark.parametrize('damage', ['product_marker_only', 'runtime_marker_only', 'candidate', 'runtime_actor', 'scope', 'base_alias'])
async def test_dynamic_repair_authority_requires_actual_receipts_and_bound_baselines(chain, damage):
    env = chain
    if damage == 'product_marker_only':
        await update(env.store, 'product_test_repair', 'product-1', repair_work_item_id='other')
    elif damage == 'runtime_marker_only':
        await update(env.store, 'product_test_runtime_repair', 'runtime-3', repair_work_item_id='other')
    elif damage == 'candidate':
        await update(env.store, 'candidate', 'product-1-candidate', run_id='other')
    elif damage == 'runtime_actor':
        await update(env.store, 'product_test_runtime_repair', 'runtime-3', actor='model')
    elif damage == 'scope':
        await update(env.store, 'work_item', 'product-1', write_paths=['.'])
    else:
        await update(env.store, 'code_snapshot', 'product-1-base', commit_oid=env.project['base_commit'])
    with pytest.raises(DomainError):
        await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='invalid-dynamic')


@pytest.mark.parametrize('damage', ['actor', 'source'])
async def test_runtime_review_cannot_drop_scope_for_forged_owner_takeover(chain, damage):
    env = chain
    result = await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='owner-handoff')
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    await collect(env, claim, PATHS[0], 'export const fixed = true;\n')
    review_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    await update(env.store, 'review_source_repair', result['id'], **(
        {'actor': 'model'} if damage == 'actor' else {'source_commit': env.project['base_commit']}))
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    _, commit = await scheduler._source(review_claim['run'], review_claim['work_item'])
    with pytest.raises(DomainError):
        await scheduler._prompt(review_claim['run'], review_claim['work_item'], commit)


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['work','scope','source'])
async def test_owner_takeover_rejects_mismatched_current_dispatch(chain,damage):
    env=chain
    await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='takeover-dispatch')
    claim=await env.workflow.claim_next('run','fixture',str(uuid4()))
    await collect(env,claim,PATHS[0],'export const fixed = true;\n')
    review_claim=await env.workflow.claim_next('run','fixture',str(uuid4()))
    context=await env.store.read('dispatch_context',claim['attempt']['id'])
    changed={'work_item_id':'foreign-work'} if damage=='work' else (
        {'allowed_write_paths':['.']} if damage=='scope' else {'source_commit':env.project['base_commit']})
    await update(env.store,'dispatch_context',context['id'],task={**context['task'],**changed})
    scheduler=Scheduler(env.workflow,env.store,None,None,env.settings)
    _,commit=await scheduler._source(review_claim['run'],review_claim['work_item'])
    with pytest.raises(DomainError):
        await scheduler._prompt(review_claim['run'],review_claim['work_item'],commit)
