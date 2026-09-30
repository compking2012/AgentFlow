from copy import deepcopy

from agentflow.control.review_contract_view import review_contract_view


class Store:
    def __init__(self, state):
        self.state = state

    async def list(self, kind):
        return deepcopy(self.state.get(kind, []))


def fixture():
    batch = {'id': 'batch', 'run_id': 'run', 'stage_id': 'review', 'state': 'repairing',
             'created_at': '2026-09-29', 'work_specs': {'triage': {}, 'migration': {}, 'fix': {}},
             'context': {'findings': [{'finding_id': 'f1'}, {'finding_id': 'f2'}]},
             'actions': [{'classification': 'test_contract_migration', 'reason': 'Old emoji expectation', 'repair_paths': ['tests/api.mjs']},
                         {'classification': 'production_fix', 'reason': 'Preserve original legend', 'repair_paths': ['public/index.html']}]}
    work = [dict(id=identity, run_id='run', step=step, role=role, status=status, quality_result=quality,
                 payload={'review_contract_task': 'batch', 'review_contract_kind': kind})
            for identity, kind, step, role, status, quality in [
                ('triage', 'triage', 'review_disposition', 'review', 'completed', 'not_applicable'),
                ('migration', 'test_contract_migration', 'integration_test_implementation', 'integration_test', 'pending', 'unknown'),
                ('fix', 'production_fix', 'implementation', 'development', 'running', 'unknown')]]
    return {'review_contract_repair': [batch], 'work_item': work}


async def test_tasks_preserve_real_status_and_one_original_stage():
    state = fixture()
    result = await review_contract_view(Store(state), 'run')
    assert set(result) == {'review'}
    view = result['review']
    assert view['state'] == 'repairing'
    assert {task['id'] for task in view['tasks']} == {'triage', 'migration', 'fix'}
    migration = next(t for t in view['tasks'] if t['id'] == 'migration')
    assert migration['status'] == 'pending' and migration['status_label'] == '待执行'
    assert migration['quality_result'] == 'unknown'
    assert '测试迁移' in view['summary'] and '生产修复' in view['summary']


async def test_coverage_task_has_distinct_label_file_scope_and_count():
    state = fixture()
    batch = state['review_contract_repair'][0]
    batch['work_specs']['coverage'] = {}
    batch['actions'].extend([{'classification': 'test_coverage_extension'}, {'classification': 'test_coverage_extension'}])
    state['work_item'].append({'id': 'coverage', 'run_id': 'run', 'step': 'unit_test_implementation',
        'role': 'unit_test', 'write_paths': ['tests/unit.test.mjs'], 'status': 'pending',
        'payload': {'review_contract_task': 'batch', 'review_contract_kind': 'test_coverage_extension'}})
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert '测试覆盖补齐 2 项' in view['summary']
    assert next(t for t in view['tasks'] if t['id'] == 'coverage')['name'] == '测试覆盖补齐 · tests/unit.test.mjs'


async def test_attention_reasons_and_foreign_tasks_are_not_leaked():
    state = fixture()
    state['review_contract_repair'][0].update(state='needs_attention', actions=[
        {'classification': 'needs_clarification', 'reason': '缺少已接受需求，无法决定期望值'}])
    state['work_item'][1]['run_id'] = 'foreign'
    state['work_item'][2]['payload']['review_contract_task'] = 'other-batch'
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert view['reasons'] == ['缺少已接受需求，无法决定期望值']
    assert {t['id'] for t in view['tasks']} == {'triage'}
    assert set(view['unavailable_work_item_ids']) == {'migration', 'fix'}


async def test_latest_batch_wins_and_review_quality_is_never_upgraded():
    state = fixture()
    old = {**deepcopy(state['review_contract_repair'][0]), 'id': 'old', 'created_at': '2026-09-28'}
    state['review_contract_repair'].append(old)
    batch = state['review_contract_repair'][0]
    batch.update(state='reviewing', review_work_ids=['review-child'])
    state['work_item'].append({'id': 'review-child', 'run_id': 'run', 'parent_stage_id': 'review',
                              'step': 'code_review', 'role': 'review', 'status': 'completed', 'quality_result': 'failed'})
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert view['batch_id'] == 'batch'
    task = next(t for t in view['tasks'] if t['id'] == 'review-child')
    assert task['quality_result'] == 'failed'
    assert task['kind'] == 'review'


async def test_approval_and_diagnostic_failures_have_readable_precise_reasons():
    state = fixture()
    batch = state['review_contract_repair'][0]
    batch['state'] = 'awaiting_approval'
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert '审批' in view['summary'] and view['issues'][0]['code'] == 'human_approval_pending'
    batch.update(state='needs_attention', actions=[], failure_diagnostic={
        'code': 'review_diagnostic_failed', 'message': '原有 API 用例仍失败', 'details': [{'case_id': 'api-1'}]})
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert view['summary'] == '原有 API 用例仍失败'
    assert view['issues'][0]['code'] == 'review_diagnostic_failed'
    assert 'case_id' not in view['summary']


async def test_diagnostic_attention_uses_core_reasons_and_never_hides_new_batch():
    state = fixture()
    batch = state['review_contract_repair'][0]
    batch.update(state='needs_attention', actions=[], reasons=[{'code': 'review_repair_limit_reached',
                 'message': '诊断返工已达到现有审查返工次数上限。'}])
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert view['issues'][0]['code'] == 'review_repair_limit_reached'
    assert view['summary'] == '诊断返工已达到现有审查返工次数上限。'
    old = {**deepcopy(batch), 'id': 'old', 'state': 'superseded', 'revision': 999}
    state['review_contract_repair'].append(old)
    assert (await review_contract_view(Store(state), 'run'))['review']['batch_id'] == 'batch'
    batch['state'] = 'diagnostic_failed'
    batch['reasons'] = []
    view = (await review_contract_view(Store(state), 'run'))['review']
    assert view['label'] == '诊断未通过'
    assert view['issues'][0]['code'] == 'review_diagnostic_failed'
