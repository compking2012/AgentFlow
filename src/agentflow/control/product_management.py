"""Owner product settings, recoverable deletion and explicit full workflow restarts."""
from __future__ import annotations

import asyncio
from uuid import NAMESPACE_URL, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.product_lifecycle import effective_targets
from agentflow.control.product_models import DEFAULT_PRODUCT_LANGUAGE
from agentflow.models.uncertainty import ACK_KINDS

_SNAPSHOT_FIELDS = ('name', 'goal', 'target', 'targets', 'language', 'review_mode', 'max_model_requests',
                    'max_active_seconds', 'max_tool_calls')
_RECORD_KINDS = (*ACK_KINDS, 'product', 'plan', 'node_job', 'product_change', 'product_launch')


def product_snapshot(product):
    return {**{key: product[key] for key in _SNAPSHOT_FIELDS if key in product},
            'targets': effective_targets(product), 'language': product.get('language', DEFAULT_PRODUCT_LANGUAGE)}


def product_for_run(tx, run):
    plan = tx.get('plan', run.get('plan_id')) if run.get('plan_id') else None
    identity = (plan or {}).get('product_contract', {}).get('product_id')
    if identity:
        return tx.get('product', identity)
    matches = [product for product in tx.list('product') if run['id'] == product.get('run_id')
               or run['id'] in (product.get('run_ids') or [])]
    if len(matches) > 1:
        raise DomainError('product_ownership_ambiguous', '运行关联了多个产品，需要先核对产品归属。')
    return matches[0] if matches else None


def guard_product_run(tx, run):
    """Use before owner resume/revision/recovery; historical reads remain available."""
    product = product_for_run(tx, run)
    if not product:
        return
    if product.get('deleted_at'):
        raise DomainError('product_deleted', '该产品已移入回收站，请先恢复产品。')
    plan = tx.get('plan', run.get('plan_id')) if run.get('plan_id') else None
    version = (plan or {}).get('product_contract', {}).get('config_revision', 1)
    if product.get('needs_restart') or version != product.get('config_revision', 1):
        raise DomainError('product_restart_required', '产品关键配置已改变，请从产品管理入口从头运行。')
    if product.get('run_id') and product['run_id'] != run['id']:
        raise DomainError('historical_product_run', '这是产品的历史运行，请使用当前运行或显式从头运行。')


async def filter_visible_product_runs(store, runs):
    products = {product['id']: product for product in await store.list('product')}
    plans = {plan['id']: plan for plan in await store.list('plan')}
    hidden = {identity for product in products.values() if product.get('deleted_at')
              for identity in [*(product.get('run_ids') or []), product.get('run_id')] if identity}
    return [run for run in runs if run['id'] not in hidden and not products.get(
        plans.get(run.get('plan_id'), {}).get('product_contract', {}).get('product_id'), {}).get('deleted_at')]


def frozen_product_version(product, run, plan):
    """Apply reader-facing metadata only; never replace ownership or output paths."""
    contract = (plan or {}).get('product_contract', {})
    snapshot = contract.get('product_snapshot') or {}
    initial = product.get('initial_product_snapshot') or product
    legacy = {'name': contract.get('display_name') or run.get('display_name') or initial['name'],
              'goal': run.get('goal', initial['goal']), 'target': contract.get('target', initial.get('target')),
              'targets': contract.get('targets', effective_targets(initial)),
              'language': contract.get('language', DEFAULT_PRODUCT_LANGUAGE)}
    return {**product, **legacy, **{key: snapshot[key] for key in _SNAPSHOT_FIELDS if key in snapshot},
            'run_id': run['id'], 'current_change_id': contract.get('change_id')}


class _ReadView:
    def __init__(self, records):
        self.records = records
        self._acknowledged = None

    def list(self, kind):
        return self.records[kind]

    def get(self, kind, identity):
        return next((record for record in self.records[kind] if record['id'] == identity), None)

    def acknowledged_invocations(self):
        from agentflow.models.uncertainty import acknowledged_invocation_ids, acknowledgment_state
        if self._acknowledged is None:
            self._acknowledged = acknowledged_invocation_ids(acknowledgment_state(self))
        return self._acknowledged


class ProductManagement:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    @staticmethod
    def _blockers(tx, product, preview=None, *, acknowledged=None):
        blockers = []
        def add(code, message):
            if not any(item['code'] == code for item in blockers):
                blockers.append({'code': code, 'message': message})
        plans = {p['id'] for p in tx.list('plan') if p.get('product_contract', {}).get('product_id') == product['id']}
        run_ids = set(product.get('run_ids') or []) | ({product['run_id']} if product.get('run_id') else set())
        run_ids |= {r['id'] for r in tx.list('run') if r.get('plan_id') in plans}
        runs = [run for run in tx.list('run') if run['id'] in run_ids]
        if any(run.get('execution_state') not in {'completed', 'cancelled', 'paused', 'blocked', 'failed'} for run in runs):
            add('product_run_active', '请先暂停或结束当前产品运行，不能在调度中修改或删除产品。')
        if product.get('state') == 'preparing' or any(c.get('product_id') == product['id']
                and not c.get('run_id') and c.get('state') == 'preparing' for c in tx.list('product_change')):
            add('product_preparing', '产品正在准备运行，请等待准备结束后再操作。')
        attempts = [a for a in tx.list('attempt') if a.get('run_id') in run_ids]
        attempt_ids = {a['id'] for a in attempts}
        if any(a.get('status') not in {'completed', 'cancelled', 'failed', 'blocked'} for a in attempts):
            add('product_attempt_active', '仍有活跃或状态未知的 Agent 任务，请先确认执行已经停止。')
        if any(w.get('run_id') in run_ids and w.get('status') in {
                'running', 'waiting_execution', 'cancel_requested', 'cancelling', 'execution_unknown'} for w in tx.list('work_item')):
            add('product_attempt_active', '仍有活跃或状态未知的工作项，请先确认执行已经停止。')
        if any((a.get('run_id') in run_ids or a['id'] in attempt_ids)
                and a.get('state') not in {'completed', 'cancelled', 'failed'} for a in tx.list('supervised_attempt')):
            add('product_attempt_active', '仍有活跃或状态未知的受管进程，请先核对执行状态。')
        from agentflow.models.uncertainty import (
            acknowledged_invocation_ids,
            acknowledgment_state,
            invocation_blocks,
        )
        if acknowledged is None:
            acknowledged = acknowledged_invocation_ids(acknowledgment_state(tx))
        if any(i.get('run_id') in run_ids and invocation_blocks(i, acknowledged)
               for i in tx.list('model_invocation')):
            add('product_model_calls_unsettled', '仍有发送中、预留或结果不确定的模型调用，请先完成核对。')
        if any(j.get('run_id') in run_ids and j.get('state') not in {'completed', 'failed', 'cancelled'}
               for j in tx.list('node_job')):
            add('product_node_jobs_active', '仍有未结束的构建或测试节点作业，请先停止并核对。')
        iterations = {run.get('iteration_id') for run in runs}
        if product.get('restore_reconciliation_required') or any(
                ((a.get('owner_kind') == 'run' and a.get('owner_id') in run_ids)
                 or (a.get('owner_kind') == 'iteration' and a.get('owner_id') in iterations))
                and (a.get('restore_uncertain') or a.get('reserved_micros') or a.get('uncertain_micros'))
                for a in tx.list('budget_account')):
            add('product_reconciliation_required', '产品仍有尚未核对的恢复或预算状态。')
        launch = tx.get('product_launch', product['id'])
        if preview is not None:
            if preview.get('revision') != (launch or {}).get('revision'):
                add('preview_changed', '预览状态已变化，请刷新后再操作。')
            if preview.get('state') != 'stopped':
                add('preview_must_stop', '请先停止产品预览，并确认预览进程已结束。')
        elif launch and launch.get('state') != 'stopped':
            add('preview_must_stop', '请先停止产品预览，并确认预览进程已结束。')
        return blockers

    async def _preview(self, product_id):
        state = 'stopped'
        if self.service.launcher:
            state = (await self.service.launcher.status(product_id)).get('state', 'execution_unknown')
        row = await self.store.read('product_launch', product_id)
        if not self.service.launcher and row:
            state = row.get('state', 'execution_unknown')
        return {'state': state, 'revision': (row or {}).get('revision')}

    async def read_view(self):
        records = await asyncio.gather(*(self.store.list(kind) for kind in _RECORD_KINDS))
        return _ReadView(dict(zip(_RECORD_KINDS, records, strict=True)))

    async def describe(self, product, *, view=None):
        # A product list shares this read-only evidence. Mutations always use
        # _assert_available against the live writer transaction instead.
        view = view if view is not None else await self.read_view()
        launch = view.get('product_launch', product['id'])
        preview = {'state': product.get('launch', {}).get('state', (launch or {}).get('state', 'stopped')),
                   'revision': (launch or {}).get('revision')}
        blocked = self._blockers(view, product, preview, acknowledged=view.acknowledged_invocations())
        deleted = bool(product.get('deleted_at'))
        supported = bool(effective_targets(product)) and not (set(effective_targets(product)) - {'web', 'api'})
        return {'can_edit': not deleted and not blocked, 'can_delete': not deleted and not blocked,
                'can_restore': deleted and not blocked, 'can_restart': not deleted and not blocked and supported,
                'blocked_reasons': blocked}

    @staticmethod
    def _assert_available(tx, product, preview, *, deleted=False):
        if bool(product.get('deleted_at')) != deleted:
            raise DomainError('product_deleted' if product.get('deleted_at') else 'product_not_deleted',
                              '请先从回收站恢复该产品。' if product.get('deleted_at') else '该产品不在回收站。')
        blockers = ProductManagement._blockers(tx, product, preview)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)

    @staticmethod
    def _metadata(product):
        return {'initial_product_snapshot': product.get('initial_product_snapshot') or product_snapshot(product),
                'initial_request_snapshot': product.get('initial_request_snapshot') or dict(product),
                'config_revision': product.get('config_revision', 1),
                'run_config_revision': product.get('run_config_revision', 1 if product.get('run_id') else None),
                'needs_restart': product.get('needs_restart', False), 'deleted_at': product.get('deleted_at')}

    async def _replay(self, product_id, action, payload, key):
        identity = str(uuid5(NAMESPACE_URL, f'product-management:{action}:{product_id}:{key}'))
        fingerprint = canonical_digest(payload)
        record = await self.store.read('product_management_operation', identity)
        if record and record['fingerprint'] != fingerprint:
            raise DomainError('idempotency_conflict', '此提交标识已用于不同的产品管理操作。')
        return identity, fingerprint, record

    async def mutate(self, product_id, request, key, action='update'):
        payload = request.model_dump(mode='json', exclude_none=True)
        identity, fingerprint, replay = await self._replay(product_id, action, payload, key)
        if replay:
            return replay['result']
        async with self.service.lifecycle.lock(product_id):
            preview = await self._preview(product_id)
            def apply(tx):
                product = tx.get('product', product_id)
                if not product:
                    raise DomainError('not_found', 'Unknown product', 404)
                if product['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', '产品已变化，请刷新后再操作。')
                self._assert_available(tx, product, preview, deleted=action == 'restore')
                fields = self._metadata(product)
                changes = {k: v for k, v in payload.items() if k not in {'expected_revision', 'reason'}}
                if action == 'update':
                    actual = {k: v for k, v in changes.items() if v != (effective_targets(product) if k == 'targets' else product.get(k))}
                    fields.update(actual)
                    if 'targets' in actual:
                        fields['target'] = actual['targets'][0]
                        fields['execution_supported'] = not bool(set(actual['targets']) - {'web', 'api'})
                    if {'goal', 'targets'} & actual.keys():
                        fields.update(config_revision=fields['config_revision'] + 1, needs_restart=True)
                    fields['default_overrides'] = sorted(set(product.get('default_overrides') or [])
                        | (changes.keys() & {'language', 'review_mode', 'max_model_requests'}))
                elif action == 'delete':
                    fields.update(deleted_at=utc_now(), deleted_reason=request.reason)
                elif action == 'restore':
                    fields.update(deleted_at=None, restored_at=utc_now())
                else:
                    raise ValueError('Unsupported management action')
                result = tx.put('product', product_id, {**product, **fields}, product['revision'])
                tx.put('product_management_operation', identity, {'product_id': product_id, 'action': action,
                    'fingerprint': fingerprint, 'actor': 'owner', 'reason': request.reason, 'result': result,
                    'changed_fields': sorted(changes), 'created_at': utc_now()})
                tx.event('product.' + action, {'product_id': product_id, 'operation_id': identity, 'actor': 'owner',
                    'reason': request.reason, 'changed_fields': sorted(changes), 'config_revision': result['config_revision'],
                    'needs_restart': result['needs_restart']})
                return result
            return await self.store.command('product.management.' + action, key,
                {'product_id': product_id, **payload}, apply)

    async def restart(self, product_id, request, key):
        payload = request.model_dump(mode='json')
        identity = str(uuid5(NAMESPACE_URL, f'product-restart:{product_id}:{key}'))
        fingerprint = canonical_digest(payload)
        async with self.service.lifecycle.lock(product_id):
            previous = await self.store.read('product_change', identity)
            if previous:
                if previous['request_fingerprint'] != fingerprint:
                    raise DomainError('idempotency_conflict', '此标识已用于不同的从头运行请求。')
                return previous
            product = await self.store.read('product', product_id)
            if not product:
                raise DomainError('not_found', 'Unknown product', 404)
            if product['revision'] != request.expected_revision:
                raise DomainError('revision_conflict', '产品已变化，请刷新后再从头运行。')
            preview = await self._preview(product_id)
            records = await asyncio.gather(*(self.store.list(kind) for kind in _RECORD_KINDS))
            self._assert_available(_ReadView(dict(zip(_RECORD_KINDS, records, strict=True))), product, preview)
            if not effective_targets(product) or set(effective_targets(product)) - {'web', 'api'}:
                raise DomainError('product_execution_unsupported', '所选平台暂不支持自动研发，不能启动运行。')
            bindings = await self.service.lifecycle._models()
            project = await self.store.read('project', product['project_id']) if product.get('project_id') else None
            if not project and product.get('creation_mode') == 'import':
                path = self.service.lifecycle._check_path(product['project_path'], existing=True)
                project = await self.service.lifecycle._import_project(path, product['name'])
            baseline = None
            if project:
                path = self.service.lifecycle._check_path(project['local_path'], existing=True)
                diagnosis = await self.service.lifecycle.diagnose(path)
                if not diagnosis['execution_supported']:
                    raise DomainError('unsupported_toolchain', '现有项目不符合已适配的 Web/API 执行契约。',
                                      details=diagnosis['blocking_reasons'])
                if await self.service.workflow._git(path, 'status', '--porcelain', '--untracked-files=all'):
                    raise DomainError('dirty_worktree', '请先提交或保存现有源码改动，再从头运行。')
                baseline = await self.service.workflow._git(path, 'rev-parse', 'HEAD')
            run_ids = list(dict.fromkeys([*(product.get('run_ids') or []), *([product['run_id']] if product.get('run_id') else [])]))
            deliveries = [d for d in await self.store.list('delivery') if d.get('run_id') in run_ids and d.get('confirmed_at')]
            delivery = max(deliveries, key=lambda d: d['confirmed_at']) if deliveries else None
            snapshot = product_snapshot(product)
            record = {'kind': 'restart', 'product_id': product_id, 'project_id': project['id'] if project else None,
                'title': '从头运行', 'description': request.reason, 'acceptance_criteria': None,
                'product_snapshot': snapshot, 'config_revision': product.get('config_revision', 1),
                'request_fingerprint': fingerprint, 'state': 'preparing', 'phase': 'environment',
                'start_stage': 'goal', 'architecture_policy': 'design_for_current_goal',
                'base_run_id': delivery['run_id'] if delivery else product.get('run_id'),
                'base_commit': delivery['commit_oid'] if delivery else baseline,
                'source_commit': delivery['commit_oid'] if delivery else None,
                'source_ref': delivery['delivery_ref'] if delivery else None,
                'run_id': None, 'plan_id': None, 'language': snapshot['language'],
                'review_mode': product.get('review_mode', 'auto'), 'model_bindings': bindings,
                'max_model_requests': product.get('max_model_requests', 200),
                'max_active_seconds': product.get('max_active_seconds', 1800),
                'max_tool_calls': product.get('max_tool_calls', 100), 'preparation_generation': 1,
                'reused_input_versions': [], 'blocking_reasons': [], 'created_at': utc_now()}
            def create(tx):
                current = tx.get('product', product_id)
                if current['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', '产品已变化，请刷新后再从头运行。')
                self._assert_available(tx, current, preview)
                change = tx.put('product_change', identity, record)
                tx.put('product', product_id, {**current, **self._metadata(current),
                    'project_id': record['project_id'], 'current_change_id': identity, 'run_ids': run_ids,
                    'initial_run_id': current.get('initial_run_id') or (run_ids[0] if run_ids else None),
                    'state': 'preparing', 'phase': 'environment', 'blocking_reasons': [], 'finalization_error': False}, current['revision'])
                tx.event('product.restart_requested', {'product_id': product_id, 'change_id': identity, 'actor': 'owner',
                    'reason': request.reason, 'config_revision': record['config_revision'], 'base_run_id': record['base_run_id']})
                return change
            return await self.store.command('product.restart', key, {'product_id': product_id, **payload}, create)
