"""Durable small coding steps under one work item's original execution budget."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.runtime.coding_receipt import output_schema as output_schema
from agentflow.runtime.trace import ExecutionTrace


def instructions(control):
    return (
        '\nBounded coding execution: The task for THIS invocation is one small coding step; '
        'the original product task is the overall context and is completed across invocations by the controller. '
        f"This is step {control['step_number']} of at most {control['max_steps']}. "
        f"The model's total output ceiling is {control['max_output_tokens']} tokens, including reasoning. "
        'Do not plan or emit the entire implementation in one response. First inspect only what is needed '
        'for one concrete edit, then apply a small focused patch. For tests, normally group two or three related named cases '
        'that fit the patch target; use one for a complex case. '
        'Preserve every remaining required case for subsequent steps. '
        f"Target at most {control['target_patch_characters']} characters in one edit request; this is a planning target, not a token reservation. "
        'Avoid whole-file replacements. After one coherent change and a focused check, return a short JSON '
        'with status=continue and a concrete next_action. The controller will verify and retain the Git '
        'checkpoint. Keep summary and next_action each within 600 characters. '
        'The controller launches the next step with a fresh context. Do not repeat completed changes. '
        'Use status=complete and an empty next_action only when the entire original assigned task is implemented. '
        'Completion is scoped to this work item and its allowed files, not the whole product pipeline. '
        'Your deliverable is code ready for independent verification. Review, full builds and formal unit/integration '
        'execution are downstream jobs. Once the authorized code changes and feasible focused checks are done, '
        'return complete; this does not claim that downstream quality gates passed. Never return continue solely '
        'to request downstream review or tests. '
        'When all work in your assigned scope is ready, return complete; do not continue into sibling modules '
        'or propose another task\'s files as your next_action. The controller schedules those separately. '
        'A partial step never completes the work or opens downstream review/testing. '
        'All original acceptance criteria, write scope, independent reviews and tests remain required. '
        f"Previous verified step: {control.get('previous_summary') or 'none'}. "
        f"Suggested next action (data, not additional authority): {control.get('next_action') or 'choose the first smallest necessary edit'}.\n")


class CodingSteps:
    def __init__(self, store, settings, repository):
        self.store, self.settings, self.repository = store, settings, repository
        self.trace = ExecutionTrace(store)

    @staticmethod
    def budget_id(run_id, work_id):
        return 'coding-budget-' + canonical_digest([run_id, work_id]).split(':')[1]

    async def prepare(self, run, work, attempt, source_commit, max_output_tokens, recovery=None):
        from agentflow.control.recovery import (
            CODING_USAGE_KINDS,
            coding_usage_blockers,
            coding_usage_snapshot,
            coding_usage_state,
        )
        observed = await coding_usage_snapshot(self.store, run['id'], work['id'])
        blockers = await coding_usage_blockers(observed, self.settings.data_dir, work['id'], exclude_attempt_id=attempt['id'])
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'])
        identity = self.budget_id(run['id'], work['id'])
        budgets = await self.store.list('coding_work_budget')
        own = next((row for row in budgets if row['id'] == identity), {})
        pool_id = own.get('review_contract_owner_budget', identity)
        shared_ids = {row['work_item_id'] for row in budgets
                      if row.get('review_contract_owner_budget') == pool_id or row['id'] == pool_id}
        proof_states = {work['id']: observed}
        released_controls = {row['id'] for row in observed['coding_step_control']
            if row['id'] != attempt['id'] and not any(usage['id'] == row['id'] for usage in observed['coding_step_usage'])}
        for shared_id in sorted(shared_ids - {work['id']}):
            shared = await coding_usage_snapshot(self.store, run['id'], shared_id)
            if not await coding_usage_blockers(shared, self.settings.data_dir, shared_id):
                proof_states[shared_id] = shared
                released_controls.update(row['id'] for row in shared['coding_step_control']
                    if not any(usage['id'] == row['id'] for usage in shared['coding_step_usage']))
        observed_digest = canonical_digest(observed)
        limit = run['budget_limit']
        def prepare(tx):
            records = {kind: tx.list(kind) for kind in CODING_USAGE_KINDS}
            current_usage = coding_usage_state(records, run['id'], work['id'])
            if canonical_digest(current_usage) != observed_digest:
                raise DomainError('coding_budget_uncertain', '准备期间编码计量或执行身份发生变化，未发起新的模型调用。')
            if any(coding_usage_state(records, run['id'], shared_id) != proof
                   for shared_id, proof in proof_states.items() if shared_id != work['id']):
                raise DomainError('coding_budget_uncertain', '原作者预留释放证明在准备期间发生变化。')
            current = tx.get('work_item', work['id'])
            if (not current or current.get('attempt_id') != attempt['id']
                    or current['fencing_token'] != attempt['fencing_token'] or current['status'] != 'running'
                    or current['run_id'] != run['id'] or attempt.get('run_id') != run['id']
                    or attempt.get('work_item_id') != work['id'] or current['generation'] != attempt['generation']):
                raise DomainError('stale_attempt', 'Coding step no longer owns the work')
            old = tx.get('coding_step_control', attempt['id'])
            if old:
                return old
            budget = tx.get('coding_work_budget', identity)
            if not budget:
                recovered_id = current.get('payload', {}).get('recovery_checkpoint_id')
                recovered = tx.get('code_snapshot', recovered_id) if recovered_id else None
                if recovered_id and (not recovered or recovered.get('work_item_id') != work['id']
                        or recovered.get('run_id') != run['id'] or recovered.get('commit_oid') != source_commit
                        or current['payload'].get('repair_base_snapshot_id') != recovered_id):
                    raise DomainError('invalid_coding_checkpoint', '编码恢复来源不属于当前工作。')
                base_commit = recovered['base_oid'] if recovered else source_commit
                budget = tx.put('coding_work_budget', identity, {'run_id': run['id'], 'work_item_id': work['id'],
                    'base_commit': base_commit, 'max_steps': self.settings.max_coding_steps,
                    'max_active_seconds': limit['max_active_seconds'], 'max_tool_calls': limit['max_tool_calls'],
                    'active_seconds': 0.0, 'observed_tool_calls': 0, 'step_count': 0, 'uncertain': False})
            seconds = budget['max_active_seconds'] - budget['active_seconds']
            tools = budget['max_tool_calls'] - budget['observed_tool_calls']
            if budget['uncertain']:
                raise DomainError('coding_budget_uncertain', '本工作存在尚未核对的执行用量，核对完成后才能继续。')
            exhausted = []
            if tools <= 0:
                exhausted.append(f"工具调用已用 {budget['observed_tool_calls']} / {budget['max_tool_calls']} 次")
            if seconds <= 0:
                exhausted.append(f"执行时长已用 {budget['active_seconds']:.1f} / {budget['max_active_seconds']:.1f} 秒")
            if budget['step_count'] >= budget['max_steps']:
                exhausted.append(f"小步次数已用 {budget['step_count']} / {budget['max_steps']} 次")
            if exhausted:
                raise DomainError('coding_budget_exhausted', '；'.join(exhausted) + '。请调整本步骤执行额度后从已保存的进度重试。')
            from agentflow.control.review_contract_binding import owner_budget_allowance
            owner_allowance = owner_budget_allowance(tx, current, budget, released_controls=released_controls)
            if owner_allowance:
                seconds = min(seconds, owner_allowance['max_active_seconds'])
                tools = min(tools, owner_allowance['max_tool_calls'])
            checkpoint_id = current.get('payload', {}).get('coding_step_checkpoint_id')
            checkpoint = tx.get('coding_step_checkpoint', checkpoint_id) if checkpoint_id else None
            if checkpoint_id and (not checkpoint or checkpoint['work_item_id'] != work['id']
                    or checkpoint['run_id'] != run['id'] or checkpoint['next_generation'] != work['generation']
                    or checkpoint['commit_oid'] != source_commit or checkpoint['budget_id'] != identity):
                raise DomainError('invalid_coding_checkpoint', '编码进度与当前工作版本不一致。')
            recovered_id = current.get('payload', {}).get('recovery_checkpoint_id')
            recovered = tx.get('code_snapshot', recovered_id) if recovered_id else None
            if recovered_id and (not recovered or recovered.get('work_item_id') != work['id']
                    or recovered.get('run_id') != run['id'] or recovered.get('commit_oid') != source_commit
                    or current['payload'].get('repair_base_snapshot_id') != recovered_id):
                raise DomainError('invalid_coding_checkpoint', '编码恢复来源不属于当前工作。')
            # A revision may legitimately replace an upstream source. Counters
            # remain shared, while only an authorized continuation inherits its diff base.
            base_commit = checkpoint['base_commit'] if checkpoint else recovered['base_oid'] if recovered else source_commit
            restored_progress = None
            if recovered and not checkpoint:
                source_control = tx.get('coding_step_control', recovered.get('source_attempt_id', 'missing'))
                if source_control:
                    source_context = tx.get('dispatch_context', recovered['source_attempt_id'])
                    if (source_control.get('run_id') != run['id'] or source_control.get('work_item_id') != work['id']
                            or source_control.get('budget_id') != identity or not source_context
                            or source_context.get('task', {}).get('coding_step') != source_control
                            or source_control['generation'] != recovered.get('source_generation')
                            or source_control['fencing_token'] != recovered.get('source_fencing_token')
                            or source_control['input_fingerprint'] != recovered.get('source_input_fingerprint')):
                        raise DomainError('invalid_coding_checkpoint', '恢复来源的编码执行范围无法核验。')
                    # A response-only recovery after earlier successful steps
                    # must retain their cumulative diff instead of demanding a rewrite.
                    base_commit = source_control['base_commit']
                point_id = recovered.get('source_coding_checkpoint_id')
                if point_id:
                    restored_progress = tx.get('coding_step_checkpoint', point_id)
                    if (not restored_progress or not source_control
                            or restored_progress.get('attempt_id') != recovered['source_attempt_id']
                            or restored_progress.get('work_item_id') != work['id']
                            or restored_progress.get('run_id') != run['id']
                            or restored_progress.get('generation') != recovered['source_generation']
                            or restored_progress.get('commit_oid') != source_commit
                            or restored_progress.get('tree_oid') != recovered['tree_oid']
                            or restored_progress.get('budget_id') != identity
                            or restored_progress.get('base_commit') != base_commit):
                        raise DomainError('invalid_coding_checkpoint', '恢复的小步进度与原编码检查点不一致。')
            progress = checkpoint or restored_progress or {}
            factor = (recovery or {}).get('step_reduction_factor', progress.get('step_reduction_factor', 1))
            factor = factor if type(factor) in {int, float} and 0 < factor <= 1 else 1
            return tx.put('coding_step_control', attempt['id'], {
                'version': 1, 'run_id': run['id'], 'work_item_id': work['id'], 'attempt_id': attempt['id'],
                'generation': work['generation'], 'fencing_token': attempt['fencing_token'],
                'input_fingerprint': attempt['input_fingerprint'], 'source_commit': source_commit,
                'budget_id': identity, 'base_commit': base_commit, 'step_number': budget['step_count'] + 1,
                'max_steps': budget['max_steps'], 'max_active_seconds': seconds, 'max_tool_calls': tools,
                'max_output_tokens': max_output_tokens, 'target_patch_characters': max(256, int(min(8000, max_output_tokens // 2) * factor)),
                'step_reduction_factor': factor,
                'prior_checkpoint_id': checkpoint_id,
                'previous_summary': progress.get('summary'),
                'next_action': (recovery or {}).get('next_action') or progress.get('next_action'),
                'bounded_recovery_id': (recovery or {}).get('recovery_id'), 'created_at': utc_now()})
        return await self.store.command('coding.step.prepare', attempt['id'],
            {'work_item_id': work['id'], 'source_commit': source_commit, 'max_output_tokens': max_output_tokens}, prepare)

    def _binding(self, tx, task, *, accounting=False):
        control = tx.get('coding_step_control', task['attempt_id'])
        item = tx.get('work_item', task['work_item_id'])
        recollecting = False
        if task.get('recollection_id'):
            from agentflow.control.coding_result_recovery import guard_recollection
            guard_recollection(tx, task['recollection_id'], attempt_id=task['attempt_id'],
                               work_item_id=task['work_item_id'], task=task)
            recollecting = True
        if (not control or not item or control != task.get('coding_step')
                or control['run_id'] != task['run_id'] or control['work_item_id'] != task['work_item_id']
                or item['run_id'] != task['run_id'] or control['attempt_id'] != task['attempt_id']
                or item.get('attempt_id') != task['attempt_id']
                or item['status'] not in ({'failed', 'blocked'} if recollecting
                    else {'running', 'cancel_requested'} if accounting else {'running'})
                or any(item.get(k) != control[k] for k in ('generation', 'fencing_token', 'input_fingerprint'))):
            raise DomainError('stale_coding_step', '编码小步回执与当前执行身份不一致。')
        return control, item

    async def account(self, task, result):
        if not task.get('coding_step'):
            return
        seconds, calls = result.get('active_seconds'), result.get('observed_tool_calls')
        known = (type(seconds) in {int, float} and math.isfinite(seconds) and seconds >= 0
                 and type(calls) is int and calls >= 0 and result.get('tool_observation_complete') is True)
        existing = await self.store.read('coding_step_usage', task['attempt_id'])
        control = await self.store.read('coding_step_control', task['attempt_id'])
        if (existing and control == task['coding_step']
                and existing.get('run_id') == task['run_id'] and existing.get('work_item_id') == task['work_item_id']
                and existing.get('budget_id') == control['budget_id'] and existing.get('known') is known
                and existing.get('active_seconds') == (seconds if known else None)
                and existing.get('observed_tool_calls') == (calls if known else None)):
            # A verified reconciliation may have replaced an unknown receipt.
            # Recollection neither rewrites its history nor charges it again.
            return
        def account(tx):
            control, _ = self._binding(tx, task, accounting=True)
            budget = tx.get('coding_work_budget', control['budget_id'])
            if tx.get('coding_step_usage', task['attempt_id']):
                return {}
            tx.put('coding_step_usage', task['attempt_id'], {'run_id': task['run_id'],
                'work_item_id': task['work_item_id'], 'budget_id': budget['id'], 'known': known,
                'active_seconds': seconds if known else None, 'observed_tool_calls': calls if known else None})
            tx.put('coding_work_budget', budget['id'], {**budget, 'step_count': budget['step_count'] + 1,
                'active_seconds': budget['active_seconds'] + (seconds if known else 0),
                'observed_tool_calls': budget['observed_tool_calls'] + (calls if known else 0),
                'uncertain': budget['uncertain'] or not known}, budget['revision'])
            if budget.get('review_contract_owner_budget'):
                from agentflow.control.review_contract_binding import charge_owner_budget
                charge_owner_budget(tx, task, budget, known=known, seconds=seconds, calls=calls)
            return {}
        await self.store.command('coding.step.account', task['attempt_id'], {'known': known,
            'active_seconds': seconds if known else None, 'observed_tool_calls': calls if known else None}, account)

    async def validate_source(self, run, item):
        identity = item.get('payload', {}).get('coding_step_checkpoint_id')
        if not identity:
            return
        checkpoint = await self.store.read('coding_step_checkpoint', identity)
        snapshot = await self.store.read('code_snapshot', checkpoint['snapshot_id']) if checkpoint else None
        if (not checkpoint or not snapshot or checkpoint['run_id'] != run['id']
                or checkpoint['work_item_id'] != item['id'] or checkpoint['next_generation'] != item['generation']
                or snapshot.get('stale') or snapshot['work_item_id'] != item['id']
                or item['payload'].get('repair_base_snapshot_id') != snapshot['id']
                or snapshot['commit_oid'] != checkpoint['commit_oid'] or snapshot['tree_oid'] != checkpoint['tree_oid']):
            raise DomainError('invalid_coding_checkpoint', '编码进度检查点缺失或版本不一致。')
        tree = await asyncio.to_thread(self.repository._integrity, Path(snapshot['repository_path']), snapshot['commit_oid'])
        if tree != checkpoint['tree_oid']:
            raise DomainError('invalid_coding_checkpoint', '编码检查点的文件内容无法核验。')

    async def collect(self, task, content, snapshot):
        """Return True for a saved partial step; normal completion stays with WorkflowService."""
        if not task.get('coding_step'):
            return False
        try:
            Draft202012Validator(output_schema()).validate(content)
        except ValidationError as error:
            raise DomainError('final_schema_invalid', '编码小步须返回简短且完整的进度回执。') from error
        if not content['summary'].strip() or len(content['summary']) > 600 or len(content['next_action']) > 600:
            raise DomainError('final_schema_invalid', '编码进度摘要和下一步指令各不得超过 600 字符。')
        if content['status'] == 'continue' and not content['next_action'].strip():
            raise DomainError('final_schema_invalid', '继续执行必须给出下一小步。')
        if content['status'] == 'complete':
            # Completed work can include a downstream handoff note. Preserve it
            # in the original receipt, but never schedule it as another coding
            # action or infer that the dependent module/tests have completed.
            def validate_complete(tx):
                control, item = self._binding(tx, task, accounting=True)
                if item['status'] == 'cancel_requested':
                    return {}
                budget = tx.get('coding_work_budget', control['budget_id'])
                usage = tx.get('coding_step_usage', task['attempt_id'])
                if (not usage or not usage.get('known') or budget['uncertain']
                        or budget['active_seconds'] > budget['max_active_seconds']
                        or budget['observed_tool_calls'] > budget['max_tool_calls']
                        or budget['step_count'] > budget['max_steps']):
                    raise DomainError('coding_budget_exhausted', '编码完成回执不能绕过共享执行预算或未知用量。')
                from agentflow.control.review_contract_binding import delegated_owner_budget
                owner = delegated_owner_budget(tx, item, budget)
                if owner and (owner['uncertain'] or owner['active_seconds'] > owner['max_active_seconds']
                        or owner['observed_tool_calls'] > owner['max_tool_calls'] or owner['step_count'] > owner['max_steps']):
                    raise DomainError('coding_budget_exhausted', '返工完成回执不能绕过原作者的累计执行额度。')
                return {}
            await self.store.command('coding.step.validate_complete', task['attempt_id'],
                {'result': content, 'commit_oid': snapshot['commit_oid']}, validate_complete)
            return False
        if not snapshot['diff']['has_changes']:
            raise DomainError('coding_no_progress', '编码小步没有产生实际代码进展，停止重复执行。')
        def advance(tx):
            latest = tx.get('work_item', task['work_item_id'])
            if (latest and latest.get('attempt_id') == task['attempt_id'] and latest['fencing_token'] == task['fencing_token']
                    and latest['status'] == 'cancel_requested'):
                return {'cancelled': True}
            control, item = self._binding(tx, task)
            attempt = tx.get('attempt', task['attempt_id'])
            saved = tx.get('code_snapshot', task['attempt_id'])
            budget = tx.get('coding_work_budget', control['budget_id'])
            usage = tx.get('coding_step_usage', task['attempt_id'])
            if (not saved or saved['commit_oid'] != snapshot['commit_oid'] or budget['uncertain']
                    or not usage or not usage.get('known') or budget['step_count'] != control['step_number']):
                raise DomainError('invalid_coding_checkpoint', '当前代码或执行用量无法核验，未推进下一小步。')
            identity = 'coding-step-' + task['attempt_id']
            checkpoint = tx.put('coding_step_checkpoint', identity, {'run_id': task['run_id'],
                'work_item_id': item['id'], 'attempt_id': task['attempt_id'], 'generation': item['generation'],
                'next_generation': item['generation'] + 1, 'snapshot_id': saved['id'], 'budget_id': budget['id'],
                'commit_oid': saved['commit_oid'], 'tree_oid': saved['tree_oid'], 'base_commit': control['base_commit'],
                'step_reduction_factor': control['step_reduction_factor'],
                'summary': content['summary'], 'next_action': content['next_action'], 'created_at': utc_now()})
            tx.put('attempt', attempt['id'], {**attempt, 'status': 'completed', 'execution_status': 'completed',
                'quality_result': 'unknown', 'finished_at': utc_now(), 'summary': content['summary'],
                'coding_step_complete': True, 'work_complete': False}, attempt['revision'])
            tx.put('work_revision', 'coding-step-' + attempt['id'], {'work_item_id': item['id'],
                'snapshot': item, 'reason': '已保存有界编码进展，继续原工作中的下一小步。'})
            payload = {**item.get('payload', {}), 'repair_base_snapshot_id': saved['id'],
                       'coding_step_checkpoint_id': checkpoint['id']}
            for name in ('recovery_checkpoint_id', 'bounded_coding_recovery'):
                payload.pop(name, None)
            revised = {**item, 'status': 'pending', 'generation': item['generation'] + 1,
                'fencing_token': item['fencing_token'] + 1, 'attempt_id': None, 'artifact_ids': [],
                'quality_result': 'unknown', 'output_fingerprint': None, 'approved_fingerprint': None,
                'payload': payload}
            from agentflow.control.recovery import inherit_recovery_model_binding
            inherit_recovery_model_binding(tx, item, revised, '继续已核验的编码小步，模型与原授权不变。')
            tx.put('work_item', item['id'], revised, item['revision'])
            tx.event('coding.step_checkpoint', {'attempt_id': attempt['id'], 'work_item_id': item['id'],
                'checkpoint_id': identity, 'next_generation': revised['generation']}, run_id=task['run_id'])
            return checkpoint
        result = await self.store.command('coding.step.advance', task['attempt_id'],
            {'result': content, 'commit_oid': snapshot['commit_oid']}, advance)
        if result.get('cancelled'):
            return False
        await self.trace.emit(task['attempt_id'], 'status', '本步代码已保存，继续下一小步',
            content['summary'] + '\n下一小步：' + content['next_action'], key=result['id'])
        return True
