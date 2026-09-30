"""Read-only, human-readable projection of work inside the original review stage."""
from __future__ import annotations

import asyncio

_LABELS = {'triaging': '审查归属分析', 'awaiting_approval': '返工方案待人工审核', 'repairing': '审查返工', 'reviewing': '修复后独立复审',
           'diagnostic_failed': '诊断未通过', 'needs_attention': '返工需要处理', 'completed': '审查返工已结束'}
_KINDS = {'triage': '归属分析', 'test_contract_migration': '测试迁移', 'test_coverage_extension': '测试覆盖补齐', 'production_fix': '生产修复',
          'assembly': '修复源码汇总', 'validation': '诊断验证', 'review': '独立复审'}
_ORDER = {kind: ordinal for ordinal, kind in enumerate(_KINDS)}
_STATUS = {'pending': '待执行', 'queued': '待执行', 'running': '执行中', 'completed': '已完成',
           'failed': '执行失败', 'waiting_execution': '等待节点执行', 'execution_unknown': '执行结果待核对',
           'blocked': '已阻塞', 'waiting_approval': '待人工审核', 'paused': '已暂停',
           'cancelled': '已取消', 'cancel_requested': '正在停止'}


def review_contract_outcome(batch):
    """An existing batch is not evidence that a new repair was scheduled."""
    state = batch.get('state')
    blockers = []
    if state == 'awaiting_approval':
        outcome = 'awaiting_approval'
        blockers.append({'code': 'human_approval_pending', 'message': '审查归属分析已提交，等待人工审批后才能安排修复。'})
    elif state in {'needs_attention', 'diagnostic_failed'}:
        outcome = 'needs_attention'
        reasons = [a['reason'] for a in batch.get('actions', []) if isinstance(a.get('reason'), str) and a['reason'].strip()]
        blockers.extend(row for row in [*batch.get('blockers', []), *batch.get('reasons', [])]
                        if isinstance(row, dict) and row.get('code') and row.get('message'))
        diagnostic = batch.get('failure_diagnostic')
        if isinstance(diagnostic, dict) and diagnostic.get('code') and diagnostic.get('message'):
            blockers.append(diagnostic)
        if not blockers and state == 'diagnostic_failed':
            blockers.append({'code': 'review_diagnostic_failed', 'message': '既有用例诊断未通过，等待核验失败归属和下一次修复。'})
        if not blockers:
            blockers.append({'code': 'review_contract_needs_attention',
                             'message': '；'.join(dict.fromkeys(reasons)) or '审查返工需要核对需求或诊断结果，尚未安排下一次修复。'})
    elif state in {'triaging', 'repairing', 'reviewing'}:
        outcome = 'scheduled'
    elif state == 'completed':
        outcome = 'completed'
    else:
        outcome = 'blocked'
        blockers.append({'code': 'review_contract_state_invalid', 'message': '审查返工批次已失效或状态无法核验。'})
    return {'outcome': outcome, 'receipt': batch, 'blockers': blockers, 'retryable': False}


def _task(work, kind):
    label = _KINDS.get(kind, '审查辅助任务')
    paths = work.get('write_paths', [])
    name = label + (' · ' + '、'.join(paths) if kind in {'test_contract_migration', 'test_coverage_extension', 'production_fix'} and paths else '')
    return {'id': work['id'], 'work_item_id': work['id'], 'kind': kind, 'name': name,
            'step': work.get('step', ''), 'role': work.get('role', ''),
            'status': work.get('status', 'unknown'), 'status_label': _STATUS.get(work.get('status'), '状态待确认'),
            'quality_result': work.get('quality_result', 'unknown'), 'artifacts': [],
            **({'blocking_reason': work['blocking_reason']} if work.get('blocking_reason') else {})}


async def review_contract_view(store, run_id):
    batches, work = await asyncio.gather(store.list('review_contract_repair'), store.list('work_item'))
    by_id = {item['id']: item for item in work if item.get('run_id') == run_id and not item.get('archived')}
    selected = {}
    for batch in sorted(batches, key=lambda b: (b.get('created_at', ''), b.get('revision', 0), b['id'])):
        if (batch.get('run_id') == run_id and batch.get('stage_id') and not batch.get('stale')
                and batch.get('state') != 'superseded'):
            selected[batch['stage_id']] = batch
    views = {}
    for stage_id, batch in selected.items():
        ids = set(batch.get('work_specs', {}))
        ids.update(batch.get('repair_work_item_ids', []))
        ids.update(batch[key] for key in ('triage_work_item_id', 'assembly_work_item_id', 'validation_work_item_id') if batch.get(key))
        tasks, unavailable = [], []
        for identity in sorted(ids):
            item = by_id.get(identity)
            if not item or item.get('payload', {}).get('review_contract_task') != batch['id']:
                unavailable.append(identity)
                continue
            tasks.append(_task(item, item.get('payload', {}).get('review_contract_kind', 'unknown')))
        state = batch.get('state', 'unknown')
        if state in {'reviewing', 'completed'}:
            for identity in batch.get('review_work_ids', []):
                item = by_id.get(identity)
                if (identity not in ids and item and item.get('step') == 'code_review'
                        and (identity == stage_id or item.get('parent_stage_id') == stage_id)):
                    tasks.append(_task(item, 'review'))
        tasks.sort(key=lambda task: (_ORDER.get(task['kind'], 99), task['id']))
        actions = batch.get('actions', [])
        counts = {kind: sum(a.get('classification') == kind for a in actions)
                  for kind in ('test_contract_migration', 'test_coverage_extension', 'production_fix', 'needs_clarification')}
        finding_count = len(batch.get('context', {}).get('findings', []))
        summary = '、'.join(f'{_KINDS[kind]} {counts[kind]} 项' for kind in ('test_contract_migration', 'test_coverage_extension', 'production_fix') if counts[kind])
        if state == 'triaging':
            summary = f'{finding_count} 项阻断意见等待归属结论'
        elif state == 'reviewing':
            summary = (summary + '；' if summary else '') + '独立复审以实际审查结果为准'
        reasons = list(dict.fromkeys(a['reason'] for a in actions if isinstance(a.get('reason'), str) and a['reason'].strip()
                                    and (state == 'needs_attention' or a.get('classification') == 'needs_clarification')))
        outcome = review_contract_outcome(batch)
        if state in {'needs_attention', 'awaiting_approval', 'diagnostic_failed'}:
            reasons = list(dict.fromkeys([*reasons, *(row['message'] for row in outcome['blockers'])]))
            summary = '；'.join(reasons)
        if not summary:
            summary = '本轮辅助任务见下方详情'
        views[stage_id] = {'batch_id': batch['id'], 'state': state, 'label': _LABELS.get(state, '审查返工状态待确认'),
                           'summary': summary, 'tasks': tasks, 'reasons': reasons,
                           'finding_count': finding_count, 'unavailable_work_item_ids': unavailable, 'issues': outcome['blockers']}
    return views
