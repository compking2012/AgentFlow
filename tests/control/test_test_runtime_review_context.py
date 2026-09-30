"""Controller-frozen runtime-repair diffs use the owner's candidate baseline."""
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from test_execution_pipeline import fixture
from test_parallel_remediation import CollectedFixtureRuntime
from test_product_test_runtime_repair import failed_candidate, patch, request_repair

from agentflow.common import DomainError
from agentflow.control.scheduler import Scheduler


@asynccontextmanager
async def reviewed_runtime_repair(tmp_path):
    async with fixture(tmp_path, app_targets=('web',)) as env:
        (env.source / 'public').mkdir()
        (env.source / 'public/layers.mjs').write_text('export const zIndex = 500; // already accepted\n')
        request = await failed_candidate(env, phase='integration', environment_error=False)
        request['reason'] = 'Disambiguate the exact control and wait for observed initialization; preserve assertions and thresholds.'
        receipt = await request_repair(env, request)
        candidate = await env.store.read('candidate', receipt['candidate_id'])
        await patch(env, 'run', 'run', execution_state='running')
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert claim['work_item']['id'] == receipt['repair_work_item_id']
        scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
        source, base = await scheduler._source(claim['run'], claim['work_item'])
        workspace = tmp_path / 'runtime-repair'
        await env.repository.clone_snapshot(source, workspace, base)
        target = workspace / 'tests/web.spec.mjs'
        target.write_text(target.read_text() + "\n// Locate the intended control only.\nconst control = page.getByRole('button', { name: '保存', exact: true });\n")
        work, attempt = claim['work_item'], claim['attempt']
        task = {'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run', 'iteration_id': 'iteration',
            'step': work['step'], 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'workspace': str(workspace), 'source_commit': base, 'allowed_write_paths': work['write_paths']}
        await patch(env, 'dispatch_context', attempt['id'], task=task)
        await scheduler._execute_existing(task)
        assert (await env.store.read('work_item', work['id']))['status'] == 'completed'
        review_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert review_claim['work_item']['id'] == receipt['review_work_item_id']
        env.scheduler, env.candidate_record, env.repair_receipt = scheduler, candidate, receipt
        env.review_claim = review_claim
        yield env


async def context_for(env, *, source_commit=None):
    claim = env.review_claim
    _, commit = await env.scheduler._source(claim['run'], claim['work_item'])
    prompt = await env.scheduler._prompt(claim['run'], claim['work_item'], source_commit or commit)
    directory = env.scheduler._context_roots[(claim['work_item']['id'], claim['work_item']['generation'])]
    documents = [json.loads(path.read_text()) for path in directory.glob('*.json')]
    evidence = [document for document in documents if isinstance(document, dict) and document.get('kind') == 'test_runtime_review_diff']
    return prompt, directory, evidence


async def test_runtime_review_has_complete_candidate_to_head_diff_not_early_plan_source(tmp_path):
    async with reviewed_runtime_repair(tmp_path) as env:
        before = {kind: await env.store.list(kind) for kind in ('review', 'work_item', 'attempt', 'candidate', 'budget_account')}
        prompt, directory, evidence = await context_for(env)
        assert len(evidence) == 1, 'The reviewer must receive a real controller-generated diff, not only a commit name'
        data = evidence[0]
        assert data['base_commit'] == env.candidate_record['source_commit']
        assert data['base_commit'] != env.snapshot['commit_oid']
        producer = await env.store.read('work_item', env.repair_receipt['repair_work_item_id'])
        assert data['head_commit'] == (await env.store.read('code_snapshot', producer['attempt_id']))['commit_oid']
        assert data['changed_paths'] == ['tests/web.spec.mjs']
        assert 'exact: true' in data['patch'] and 'zIndex' not in data['patch']
        assert data['complete'] is True and data['authorized_write_paths'] == ['tests/web.spec.mjs']
        assert 'read_context' in prompt and 'candidate baseline' in prompt
        assert 'locator disambiguation' in prompt and 'initialization' in prompt
        assert 'assertions' in prompt and 'thresholds' in prompt
        assert all(path.stat().st_mode & 0o222 == 0 for path in directory.glob('*.json'))
        assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('damage', ['candidate_base', 'receipt_actor', 'receipt_review', 'scope', 'attempt_fence', 'head_tree', 'source_commit'])
async def test_runtime_review_rejects_unbound_or_stale_diff_evidence(tmp_path, damage):
    async with reviewed_runtime_repair(tmp_path) as env:
        producer = await env.store.read('work_item', env.repair_receipt['repair_work_item_id'])
        receipt_id = producer['payload']['test_runtime_repair_id']
        if damage == 'candidate_base':
            await patch(env, 'candidate', env.candidate_record['id'], source_commit=env.snapshot['commit_oid'])
        elif damage == 'receipt_actor':
            await patch(env, 'product_test_runtime_repair', receipt_id, actor='model')
        elif damage == 'receipt_review':
            await patch(env, 'product_test_runtime_repair', receipt_id, review_work_item_id='other-review')
        elif damage == 'scope':
            await patch(env, 'work_item', producer['id'], write_paths=['.'])
        elif damage == 'attempt_fence':
            await patch(env, 'attempt', producer['attempt_id'], fencing_token=999)
        elif damage == 'head_tree':
            await patch(env, 'code_snapshot', producer['attempt_id'], tree_oid='0' * 40)
        with pytest.raises(DomainError):
            await context_for(env, source_commit=env.snapshot['commit_oid'] if damage == 'source_commit' else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['empty_evidence','report_digest'])
async def test_incomplete_original_failure_evidence_cannot_claim_complete(tmp_path,damage):
    async with reviewed_runtime_repair(tmp_path) as env:
        identity=env.repair_receipt['repair_work_item_id']
        receipt=await env.store.read('product_test_runtime_repair',identity)
        evidence=receipt['evidence']
        if damage=='empty_evidence':
            evidence=[]
        else:
            evidence[0]['report']['raw_digest']='sha256:'+'0'*64
        await patch(env,'product_test_runtime_repair',identity,evidence=evidence)
        with pytest.raises(DomainError):
            await context_for(env)

@pytest.mark.asyncio
async def test_source_alias_removed_does_not_erase_owner_baseline(tmp_path):
    async with reviewed_runtime_repair(tmp_path) as env:
        identity=env.repair_receipt['repair_work_item_id']
        work=await env.store.read('work_item',identity)
        payload=dict(work['payload'])
        payload.pop('repair_base_snapshot_id',None)
        await patch(env,'work_item',identity,payload=payload)
        _,_,evidence=await context_for(env)
        assert evidence[0]['base_commit']==env.candidate_record['source_commit']
        assert evidence[0]['complete'] is True

@pytest.mark.asyncio
async def test_empty_delta_does_not_claim_pass_or_resolution(tmp_path):
    async with reviewed_runtime_repair(tmp_path) as env:
        identity=env.repair_receipt['repair_work_item_id']
        work=await env.store.read('work_item',identity)
        snapshot=await env.store.read('code_snapshot',work['attempt_id'])
        from pathlib import Path
        repository=Path(snapshot['repository_path'])
        base=env.candidate_record['source_commit']
        original=env.repository._run(repository,['show',base+':tests/web.spec.mjs'])
        (repository/'tests/web.spec.mjs').write_bytes(original)
        frozen=await env.repository.freeze_workspace(repository,snapshot['commit_oid'],'restore candidate test tree')
        await patch(env,'code_snapshot',snapshot['id'],commit_oid=frozen['commit_oid'],tree_oid=frozen['tree_oid'])
        prompt,_,evidence=await context_for(env)
        assert evidence[0]['changed_paths']==[] and evidence[0]['patch']==''
        assert evidence[0]['base_equals_head_tree'] is True
        assert evidence[0]['quality_result']=='not_assessed'
        assert 'An empty diff only proves no change' in prompt
        assert (await env.store.read('work_item',env.review_claim['work_item']['id']))['quality_result']=='unknown'
