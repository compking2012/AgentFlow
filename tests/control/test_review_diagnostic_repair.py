"""Real failed diagnostics return to read-only triage without advancing normal tests."""
from copy import deepcopy

from test_review_contract_repair import schedule, setup_batch
from test_workflow import flow as flow

from agentflow.control.review_contract_repair import ReviewContractRepair


async def failed_diagnostic(flow):
    _, result = await setup_batch(flow)
    await schedule(flow, result)
    store = flow[1]
    batch = await store.read('review_contract_repair', 'batch')
    def seed(tx):
        assembly = tx.get('work_item', batch['assembly_work_item_id'])
        tx.put('work_item', assembly['id'], {**assembly, 'status': 'completed', 'attempt_id': 'merged'}, assembly['revision'])
        source = tx.put('code_snapshot', 'merged', {'run_id': 'run', 'work_item_id': assembly['id'],
            'generation': 1, 'repository_path': flow[3]['local_path'], 'commit_oid': 'merged',
            'tree_oid': 'merged-tree', 'base_oid': 'commit', 'stale': False})
        diagnostic = tx.put('review_diagnostic', 'diagnostic', {'run_id': 'run', 'state': 'failed',
            'source': source, 'reports': [{'job_id': 'job', 'node_result_id': 'node-result',
                'normalized_report': {'cases': [{'case_id': 'api.spec.mjs::regression::',
                    'status': 'failed', 'message': 'Expected preserved legend', 'attempt': 0}]}}],
            'affected_cases': [{'case_id': 'case', 'path': 'tests/api.spec.mjs',
                'framework_case_ids': ['api.spec.mjs::regression::']}], 'jobs': ['job'], 'blockers': ['suite_failed']})
        tx.put('node_result', 'node-result', {'job_id': 'job', 'assessment_state': 'validated'})
        tx.put('node_job', 'job', {'run_id': 'run', 'parent_work_item_id': batch['validation_work_item_id'],
            'state': 'failed', 'result_id': 'node-result'})
        validation = tx.get('work_item', batch['validation_work_item_id'])
        tx.put('work_item', validation['id'], {**validation, 'status': 'completed',
            'quality_result': 'failed', 'attempt_id': 'validation-attempt'}, validation['revision'])
        tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'diagnostic_failed',
            'failed_diagnostic_id': diagnostic['id']}, batch['revision'])
        return source
    source = await store.command('fixture', 'failed-diagnostic', {}, seed)
    return source


async def test_failed_diagnostic_creates_fresh_readonly_analysis_once(flow, monkeypatch):
    source = await failed_diagnostic(flow)
    core = ReviewContractRepair(flow[1], flow[0], nodes=object())
    async def prepare(*args, **kwargs):
        return {'manifests': [], 'cases': [], 'execution_spec': {}}
    monkeypatch.setattr(core.diagnostics, 'prepare_builtin_suite', prepare)
    # Source inventory is already frozen on the previous accepted batch.
    await core.reconcile()
    await core.reconcile()
    batches = await flow[1].list('review_contract_repair')
    assert len(batches) == 2
    old = next(row for row in batches if row['id'] == 'batch')
    new = next(row for row in batches if row['id'] != 'batch')
    assert old['state'] == 'superseded' and new['state'] == 'triaging'
    assert new['context']['snapshot'] == source
    assert new['context']['findings'][0]['path'] == 'tests/api.spec.mjs'
    assert new['context']['findings'][0]['diagnostic_id'] == 'diagnostic'
    assert (await flow[1].read('work_item', new['triage_work_item_id']))['write_paths'] == []
    assert (await flow[1].read('work_item', 'api-tests'))['status'] == 'pending'
    assert (await flow[1].read('work_item', old['validation_work_item_id']))['required'] is False
    assert (await flow[1].read('work_item', old['validation_work_item_id'])).get('archived') is True
    assert not await flow[1].list('check')


async def test_diagnostic_reports_cannot_be_substituted_after_triage(flow, monkeypatch):
    await failed_diagnostic(flow)
    core = ReviewContractRepair(flow[1], flow[0], nodes=object())
    async def prepare(*args, **kwargs):
        return {'manifests': [], 'cases': [], 'execution_spec': {}}
    monkeypatch.setattr(core.diagnostics, 'prepare_builtin_suite', prepare)
    await core.reconcile()
    batch = next(row for row in await flow[1].list('review_contract_repair') if row['id'] != 'batch')
    changed = deepcopy(await flow[1].read('review_diagnostic', 'diagnostic'))
    changed['reports'] = []
    await flow[1].command('fixture', 'replace-report', {}, lambda tx: tx.put('review_diagnostic', 'diagnostic', changed, changed['revision']))
    import pytest

    from agentflow.common import DomainError
    with pytest.raises(DomainError, match='诊断'):
        await flow[1].command('fixture', 'verify', {}, lambda tx: core._verify_context(tx, batch) or {})


async def test_cancelled_diagnostic_drains_nodes_before_finishing(flow):
    await failed_diagnostic(flow)
    store, service = flow[1], flow[0]
    batch = await store.read('review_contract_repair', 'batch')
    validation_id = batch['validation_work_item_id']
    def seed(tx):
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'execution_state': 'cancelling'}, run['revision'])
        work = tx.get('work_item', validation_id)
        tx.put('work_item', validation_id, {**work, 'status': 'cancel_requested'}, work['revision'])
        tx.put('attempt', 'validation-attempt', {'run_id': 'run', 'work_item_id': validation_id,
            'status': 'waiting_execution', 'fencing_token': work['fencing_token'], 'generation': work['generation'],
            'input_fingerprint': work['input_fingerprint']})
        job = tx.get('node_job', 'job')
        tx.put('node_job', 'job', {**job, 'state': 'running'}, job['revision'])
        current = tx.get('review_contract_repair', 'batch')
        tx.put('review_contract_repair', 'batch', {**current, 'state': 'repairing'}, current['revision'])
        return {}
    await store.command('fixture', 'cancel-running', {}, seed)
    class Nodes:
        async def cancel_job(self, job_id, reason, key):
            job = await store.read('node_job', job_id)
            await store.command('fixture', key, {}, lambda tx: tx.put('node_job', job_id, {**job, 'state': 'stopping'}, job['revision']))
    core = ReviewContractRepair(store, service, nodes=Nodes())
    await core.reconcile()
    assert (await store.read('node_job', 'job'))['state'] == 'stopping'
    assert (await store.read('work_item', validation_id))['status'] == 'cancel_requested'
    job = await store.read('node_job', 'job')
    await store.command('fixture', 'node-stopped', {}, lambda tx: tx.put('node_job', 'job', {**job, 'state': 'cancelled'}, job['revision']))
    await core.reconcile()
    assert (await store.read('work_item', validation_id))['status'] == 'cancelled'
