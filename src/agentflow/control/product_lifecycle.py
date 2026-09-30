"""Product registration and explicit requirement iterations over the real workflow."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.product_diagnostics import diagnose_project
from agentflow.control.product_models import DEFAULT_PRODUCT_LANGUAGE, TARGET_ORDER, product_identity
from agentflow.control.project_documents import ProjectDocumentService
from agentflow.domain.planning import STEPS
from agentflow.runtime.launcher import atomic_json

logger = logging.getLogger(__name__)


def effective_targets(product):
    if product.get('targets') is not None:
        return list(product['targets'])
    return [product.get('target', 'web')] if product.get('target', 'web') else []


def execution_targets(product):
    selected = set(effective_targets(product))
    return selected | {'api'} if 'web' in selected else selected


class ProductLifecycle:
    def __init__(self, service):
        self.service = service
        self._locks = {}
        self._imports = asyncio.Lock()
        self.tasks = {}

    def lock(self, product_id):
        return self._locks.setdefault(product_id, asyncio.Lock())

    async def preserve_legacy_plan_language(self, payload):
        """A saved pre-language plan keeps its original idempotent request shape."""
        contract = payload.get('product_contract') or {}
        if contract.get('language') != DEFAULT_PRODUCT_LANGUAGE:
            return payload
        plans = [plan for plan in await self.service.store.list('plan')
                 if plan.get('project_id') == payload.get('project_id')
                 and plan.get('product_contract', {}).get('product_id') == contract.get('product_id')
                 and plan.get('product_contract', {}).get('change_id') == contract.get('change_id')]
        if plans and all('language' not in plan.get('product_contract', {}) for plan in plans):
            # Legacy accepted plans already imply Chinese. Adding a new field to
            # their original command would turn a safe retry into a conflict.
            return {**payload, 'product_contract': {key: value for key, value in contract.items() if key != 'language'}}
        return payload

    def _check_path(self, value, *, existing=False):
        path = Path(value).expanduser()
        if not path.is_absolute() or path.is_symlink() or (existing and not path.is_dir()):
            raise DomainError('invalid_project_path', '请选择存在的绝对目录，不能使用符号链接', 422)
        path = path.resolve()
        protected = [self.service.settings.data_dir.resolve()]
        if self.service.configuration:
            protected.append(self.service.configuration.config_path.parent.resolve())
        if path == Path(path.anchor) or any(path == p or path in p.parents or p in path.parents for p in protected):
            raise DomainError('protected_project_path', '项目和输出目录必须与平台私有数据、配置目录分离', 422)
        return path

    async def diagnose(self, project_path):
        path = self._check_path(project_path, existing=True)
        return await asyncio.to_thread(diagnose_project, path)

    def _output(self, request, identity, source=None):
        root = self.service.configuration.product.output_root if self.service.configuration else Path.home() / 'AgentFlowProducts'
        slug = re.sub(r'[^A-Za-z0-9_-]+', '-', request.name).strip('-')[:48] or 'product'
        output = self._check_path(request.output_directory or root / f'{slug}-{identity[:8]}')
        if source and (output == source or output in source.parents or source in output.parents):
            raise DomainError('output_overlaps_project', '导入项目的交付目录必须放在原项目目录之外', 422)
        return output

    def _targets(self, request, diagnosis=None):
        if request.targets:
            values = request.targets
        elif 'target' in request.model_fields_set:
            values = [request.target]
        elif diagnosis is not None:
            values = diagnosis['detected_targets']
        else:
            values = [self.service.configuration.product.target if self.service.configuration else request.target]
        return [target for target in TARGET_ORDER if target in values]

    async def _models(self):
        setup = await self.service.setup_status()
        if setup.get('restart_required'):
            raise DomainError('configuration_restart_required', '配置已变化，请重启平台后再创建需求变更', 409)
        bindings = setup['model_bindings']
        for key, protocol in [('role_model_profile_id', 'chat_completions'), ('coding_model_profile_id', 'responses')]:
            if not bindings.get(key):
                raise DomainError('model_setup_required', '请先配置专业角色和编码模型', 409)
            profile = await self.service.models.registry.get(bindings[key])
            profile.assert_accepted(protocol)
            self.service.secrets.read(profile.credential_reference)
        if any(item['code'] == 'runtime_missing' for item in setup['requirements']):
            raise DomainError('runtime_setup_required', '请先修复本机 Agent 执行组件', 409)
        return dict(bindings)

    async def _import_project(self, path, name):
        workflow = self.service.workflow
        if await workflow._git(path, 'status', '--porcelain', '--untracked-files=all'):
            raise DomainError('dirty_worktree', '请先提交或另行保存原项目的本地改动，再登记可执行基线', 409)
        if Path(await workflow._git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
            raise DomainError('invalid_repository', '请选择 Git 仓库根目录', 422)
        for project in await self.service.store.list('project'):
            if Path(project['local_path']).resolve() == path:
                return project
        return await workflow.create_project({'name': name, 'local_path': str(path),
            'import_mode': 'snapshot_existing', 'dirty_worktree_policy': 'require_clean'},
            'product-import-project:' + canonical_digest(str(path)))

    async def submit_import(self, request, key):
        fingerprint = canonical_digest({'version': 2, 'request': request.model_dump(mode='json', exclude_unset=True)})
        identity = product_identity(key)
        async with self._imports:
            existing = await self.service.store.read('product', identity)
            if existing:
                if existing.get('request_fingerprint') != fingerprint:
                    raise DomainError('idempotency_conflict', '此提交标识已用于不同产品请求')
                return existing
            if self.service.configuration and self.service.configuration.restart_required():
                raise DomainError('configuration_restart_required', '配置已变化，请重启平台后再登记产品', 409)
            path = self._check_path(request.project_path, existing=True)
            diagnosis = await self.diagnose(path)
            targets = self._targets(request, diagnosis)
            output = self._output(request, identity, path)
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise DomainError('output_not_empty', '导入的交付目录必须是新目录或空目录')
            if any(p.get('project_path') == str(path) or p['output_directory'] == str(output)
                   for p in await self.service.store.list('product')):
                raise DomainError('product_exists', '该项目或交付目录已经属于另一个产品')
            errors = [item['message'] for item in diagnosis['blocking_reasons']]
            if set(targets) - {'web', 'api'}:
                errors.append('所选原生平台暂不支持执行，可先登记项目。')
            project = None
            try:
                if not diagnosis['git_detected']:
                    raise DomainError('git_repository_required', '请先为现有代码创建本地 Git 提交基线；登记导入不会自动初始化或改写原项目', 409)
                project = await self._import_project(path, request.name)
            except DomainError as error:
                errors.append(error.message)
            supported = bool(project and targets and diagnosis['execution_supported'] and not (set(targets) - {'web', 'api'}))
            language = (request.language if 'language' in request.model_fields_set or not self.service.configuration
                        else self.service.configuration.product.language)
            request_limit = (request.max_model_requests if 'max_model_requests' in request.model_fields_set
                             or not self.service.configuration else self.service.configuration.product.max_model_requests)
            payload = {**request.model_dump(mode='json'), 'target': targets[0] if targets else None, 'targets': targets,
                'language': language, 'initial_language': language,
                'max_model_requests': request_limit,
                'project_path': str(path), 'output_directory': str(output), 'request_fingerprint': fingerprint,
                'request_fingerprint_version': 2, 'diagnosis': diagnosis, 'execution_supported': supported,
                'project_id': project['id'] if project else None, 'run_id': None, 'plan_id': None,
                'run_ids': [], 'initial_run_id': None, 'current_change_id': None,
                'model_bindings': {}, 'delivery': None, 'state': 'registered' if supported else 'blocked',
                'phase': 'registered' if supported else 'diagnosis', 'blocking_reasons': list(dict.fromkeys(errors)),
                'created_at': utc_now()}

            def register(tx):
                for product in tx.list('product'):
                    if product.get('project_path') == str(path) or product['output_directory'] == str(output):
                        raise DomainError('product_exists', '该项目或交付目录已经属于另一个产品')
                if output.exists() and (not output.is_dir() or any(output.iterdir())):
                    raise DomainError('output_not_empty', '导入的交付目录必须是新目录或空目录')
                result = tx.put('product', identity, payload)
                tx.event('product.imported', {'product_id': identity, 'project_id': payload['project_id'],
                                             'execution_supported': supported})
                return result
            return await self.service.store.command('product.import', key, {'request_fingerprint': fingerprint}, register)

    async def register_unsupported(self, request, key, fingerprint):
        identity = product_identity(key)
        output = self._output(request, identity)
        targets = self._targets(request)
        def register(tx):
            if any(p['output_directory'] == str(output) for p in tx.list('product')):
                raise DomainError('output_in_use', '该交付目录已经属于另一个产品')
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise DomainError('output_not_empty', '新产品需要新目录或空目录')
            result = tx.put('product', identity, {**request.model_dump(mode='json'),
                'initial_language': request.language,
                'targets': targets, 'target': targets[0], 'output_directory': str(output),
                'request_fingerprint': fingerprint, 'request_fingerprint_version': 2,
                'state': 'blocked', 'phase': 'capability', 'execution_supported': False,
                'blocking_reasons': ['所选原生平台暂不支持自动研发执行，当前仅登记产品目标。'],
                'project_id': None, 'run_id': None, 'run_ids': [], 'current_change_id': None,
                'delivery': None, 'created_at': utc_now()})
            tx.event('product.registered', {'product_id': identity, 'execution_supported': False})
            return result
        return await self.service.store.command('product.create', key, {'request_fingerprint': fingerprint}, register)

    async def _baseline_inputs(self, product):
        product = await self.service.store.read('product', product['id'])
        existing = product.get('baseline_input_versions') or []
        if existing:
            return existing
        diagnosis = product.get('diagnosis') or await self.diagnose(
            (await self.service.store.read('project', product['project_id']))['local_path'])
        documents = {
            'goal': {'title': product['name'], 'summary': '用户提供的原始产品目标', 'content': product['goal'],
                'sources': [], 'unknowns': ['此文档为用户输入，不代表 Agent 已完成分析。']},
            'research': {'title': '现有工程静态基线', 'summary': '本地文件类型检查；未开展市场或竞品调研',
                'content': '## 本地工程诊断\n\n'
                    + '识别类型：' + ('、'.join(diagnosis['detected_targets']) or '未知，不自动猜测') + '\n\n'
                    + '框架标志：' + ('、'.join(diagnosis['frameworks']) or '未确认') + '\n\n'
                    + '### 静态标志\n\n' + '\n'.join(f"- {m['path']}：{m['reason']}" for m in diagnosis['markers'])
                    + '\n\n### 限制\n\n仅检查本地文件标志。未运行项目、未安装依赖，未完成市场/竞品调研或构建测试。',
                'sources': [item['path'] for item in diagnosis['markers']],
                'unknowns': ['未验证依赖安装、构建、测试、市场需求或竞品事实。']},
        }
        records = []
        for step, content in documents.items():
            blob = await self.service.workflow.artifacts.put_bytes(json.dumps(content, ensure_ascii=False).encode(),
                                                                 media_type='application/json')
            records.append({'step': step, 'digest': blob['id'], 'name': 'import-' + step + '.json',
                'media_type': 'application/json', 'project_id': product['project_id'], 'product_id': product['id'],
                'work_item_id': None, 'run_id': None, 'generation': 0, 'stale': False,
                'source_kind': 'owner_input' if step == 'goal' else 'static_diagnosis',
                'quality_result': 'not_applicable', 'created_at': utc_now()})
        def save(tx):
            current = tx.get('product', product['id'])
            if current.get('baseline_input_versions'):
                return {'input_versions': current['baseline_input_versions']}
            refs = []
            for record in records:
                row = tx.put('artifact', str(uuid5(NAMESPACE_URL, f"product-baseline:{product['id']}:{record['step']}")), record)
                refs.append({'artifact_version_id': row['id'], 'fingerprint': row['digest'], 'revision': row['revision']})
            tx.put('product', product['id'], {**current, 'baseline_input_versions': refs}, current['revision'])
            tx.event('product.baseline_recorded', {'product_id': product['id'], 'source_kinds': ['owner_input', 'static_diagnosis']})
            return {'input_versions': refs}
        saved = await self.service.store.command('product.baseline', product['id'],
            {'project_id': product['project_id'], 'digests': [r['digest'] for r in records]}, save)
        return saved['input_versions']

    async def _stage_inputs(self, product, base_run_id=None):
        ordered_history = list(dict.fromkeys([*(product.get('run_ids') or []),
            *([product['run_id']] if product.get('run_id') else [])]))
        if base_run_id in ordered_history:
            ordered_history = ordered_history[:ordered_history.index(base_run_id) + 1]
        if product.get('config_revision', 1) > 1:
            plans = {plan['id']: plan for plan in await self.service.store.list('plan')}
            valid_runs = {run['id'] for run in await self.service.store.list('run')
                if plans.get(run.get('plan_id'), {}).get('product_contract', {}).get('config_revision', 1)
                == product['config_revision']}
            ordered_history = [identity for identity in ordered_history if identity in valid_runs]
        history = set(ordered_history)
        candidates = [a for a in await self.service.store.list('artifact') if not a.get('stale')
            and not a.get('readable') and a.get('media_type') == 'application/json'
            and (a.get('run_id') in history and a.get('run_id') or a.get('product_id') == product['id'])]
        latest, ranks = {}, {}
        work = {item['id']: item for item in await self.service.store.list('work_item')}
        confirmed_runs = {delivery['run_id'] for delivery in await self.service.store.list('delivery')
                          if delivery.get('confirmed_at') and delivery.get('run_id') in history}
        for artifact in candidates:
            if 'proposal' in artifact.get('name', '').lower():
                continue
            if artifact.get('work_item_id'):
                item = work.get(artifact['work_item_id'])
                if (not item or item.get('parent_stage_id') or item.get('generation') != artifact.get('generation')
                        or item.get('status') != 'completed' or item.get('quality_result') == 'failed'):
                    continue
                if artifact['step'] in {'prd', 'architecture'} and artifact['run_id'] not in confirmed_runs:
                    continue
            rank = (ordered_history.index(artifact['run_id']) if artifact.get('run_id') in history else -1,
                    artifact.get('generation', 0), artifact.get('name') == 'openhands_final.json',
                    artifact.get('created_at', ''))
            if artifact['step'] not in ranks or rank > ranks[artifact['step']]:
                latest[artifact['step']], ranks[artifact['step']] = artifact, rank
        if 'goal' not in latest or 'research' not in latest:
            if product.get('config_revision', 1) > 1:
                raise DomainError('product_baseline_unavailable', '当前产品配置尚未形成目标和调研资料，请先完成从头运行。')
            for ref in await self._baseline_inputs(product):
                artifact = await self.service.store.read('artifact', ref['artifact_version_id'])
                latest.setdefault(artifact['step'], artifact)
        def refs(steps):
            return [{'artifact_version_id': latest[step]['id'], 'fingerprint': latest[step]['digest'],
                     'revision': latest[step]['revision']} for step in steps if step in latest]
        return {'prd': refs(['goal', 'research', 'prd']), 'architecture': refs(['architecture'])}

    async def submit_change(self, product_id, request, key):
        identity = str(uuid5(NAMESPACE_URL, f'product-change:{product_id}:{key}'))
        fingerprint = canonical_digest(request.model_dump(mode='json', exclude_unset=True))
        async with self.lock(product_id):
            existing = await self.service.store.read('product_change', identity)
            if existing:
                if existing['request_fingerprint'] != fingerprint:
                    raise DomainError('idempotency_conflict', '此标识已用于不同的需求变更')
                return existing
            product = await self.service.detail(product_id)
            if product.get('deleted_at') or product.get('needs_restart'):
                raise DomainError('product_deleted' if product.get('deleted_at') else 'product_restart_required',
                                  '请先恢复产品。' if product.get('deleted_at') else '关键配置已改变，请先从头运行。')
            blocked = product.get('management', {}).get('blocked_reasons', [])
            if blocked:
                raise DomainError(blocked[0]['code'], blocked[0]['message'], details=blocked)
            if product['revision'] != request.expected_revision:
                raise DomainError('revision_conflict', '产品状态已变化，请刷新后再提交需求变更')
            if not product.get('execution_supported', not (set(effective_targets(product)) - {'web', 'api'})):
                raise DomainError('product_execution_unsupported', '该产品目前仅可登记，暂不支持需求变更执行', 409)
            if not product.get('project_id') or product.get('restore_reconciliation_required'):
                raise DomainError('product_baseline_unavailable', '产品尚无可用的已登记工程基线', 409)
            if self.service.launcher:
                preview = await self.service.launcher.status(product_id)
                if preview['state'] in {'running', 'starting', 'execution_unknown'}:
                    raise DomainError('preview_must_stop', '请先停止该产品的预览，再提交需求变更', 409)
            previous = await self.service.store.read('run', product['run_id']) if product.get('run_id') else None
            if previous and previous['execution_state'] not in {'completed', 'cancelled'}:
                raise DomainError('product_run_active', '当前研发运行尚未结束，请先处理或取消原运行', 409)
            current = (await self.service.store.read('product_change', product['current_change_id'])
                       if product.get('current_change_id') else None)
            if current and current['state'] in {'preparing', 'running', 'waiting_approval'}:
                raise DomainError('product_change_active', '已有需求变更正在处理', 409)
            project = await self.service.store.read('project', product['project_id'])
            path = self._check_path(project['local_path'], existing=True)
            diagnosis = await self.diagnose(path)
            if not diagnosis['execution_supported']:
                raise DomainError('unsupported_toolchain', '当前项目不符合受支持执行契约', 409,
                                  details=diagnosis['blocking_reasons'])
            if await self.service.workflow._git(path, 'status', '--porcelain', '--untracked-files=all'):
                raise DomainError('dirty_worktree', '请先提交或保存本地改动，再创建需求变更', 409)
            bindings = await self._models()
            run_ids = list(dict.fromkeys([*(product.get('run_ids') or []), *([product['run_id']] if product.get('run_id') else [])]))
            deliveries = [d for d in await self.service.store.list('delivery') if d.get('run_id') in run_ids and d.get('confirmed_at')]
            delivery = max(deliveries, key=lambda d: d['confirmed_at']) if deliveries else None
            baseline = delivery['commit_oid'] if delivery else await self.service.workflow._git(path, 'rev-parse', 'HEAD')
            base_run_id = delivery['run_id'] if delivery else product.get('run_id')
            documents = await ProjectDocumentService(self.service.store, self.service.workflow.artifacts,
                self.service.settings).freeze(product_id, base_run_id)
            defaults = self.service.configuration.product if self.service.configuration else None
            record = {'product_id': product_id, 'project_id': project['id'], 'title': request.title or request.description[:60],
                'description': request.description, 'acceptance_criteria': request.acceptance_criteria,
                'language': request.language or product.get('language', DEFAULT_PRODUCT_LANGUAGE),
                'request_fingerprint': fingerprint, 'state': 'preparing', 'phase': 'environment',
                'start_stage': 'prd', 'architecture_policy': 'review_existing', 'base_run_id': base_run_id,
                'document_baseline': documents,
                'base_commit': baseline, 'source_commit': delivery['commit_oid'] if delivery else None,
                'source_ref': delivery['delivery_ref'] if delivery else None, 'run_id': None, 'plan_id': None,
                'review_mode': request.review_mode or (defaults.review_mode if defaults else product['review_mode']),
                'model_bindings': bindings, 'max_model_requests': defaults.max_model_requests if defaults else product['max_model_requests'],
                'max_active_seconds': defaults.max_active_seconds if defaults else product.get('max_active_seconds', 1800),
                'max_tool_calls': defaults.max_tool_calls if defaults else product.get('max_tool_calls', 100),
                'preparation_generation': 1, 'reused_input_versions': [], 'blocking_reasons': [], 'created_at': utc_now()}
            from agentflow.control.product_management import product_snapshot
            record.update(product_snapshot=product_snapshot(product), config_revision=product.get('config_revision', 1))
            for field in ('review_mode', 'max_model_requests'):
                if field in product.get('default_overrides', []) and not (field == 'review_mode' and request.review_mode):
                    record[field] = product[field]
            preview = await self.service.management._preview(product_id)
            def create(tx):
                current_product = tx.get('product', product_id)
                if current_product.get('deleted_at') or current_product.get('needs_restart'):
                    raise DomainError('product_restart_required', '产品配置已改变或已删除，请重新查看产品。')
                if current_product['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', '产品状态已变化，请刷新后再提交需求变更')
                self.service.management._assert_available(tx, current_product, preview)
                change = tx.put('product_change', identity, record)
                tx.put('product', product_id, {**current_product, 'current_change_id': identity, 'run_ids': run_ids,
                    'initial_run_id': current_product.get('initial_run_id') or (run_ids[0] if run_ids else None),
                    'state': 'preparing', 'phase': 'environment', 'blocking_reasons': [], 'finalization_error': False}, current_product['revision'])
                tx.event('product.change_created', {'product_id': product_id, 'change_id': identity, 'base_run_id': record['base_run_id']})
                return change
            return await self.service.store.command('product.change.create', key, {'product_id': product_id, 'fingerprint': fingerprint}, create)

    async def retry_change(self, product_id, key):
        async with self.lock(product_id):
            product = await self.service.store.read('product', product_id)
            change_id = product.get('current_change_id') if product else None
            if not change_id:
                raise DomainError('product_retry_not_allowed', '没有可恢复的需求变更准备')
            def retry(tx):
                current = tx.get('product', product_id)
                if current.get('deleted_at'):
                    raise DomainError('product_deleted', '请先从回收站恢复该产品。')
                change = tx.get('product_change', change_id)
                if current.get('needs_restart') and change.get('kind') != 'restart':
                    raise DomainError('product_restart_required', '关键配置已改变，请从头运行。')
                if change.get('config_revision', 1) != current.get('config_revision', 1):
                    raise DomainError('product_configuration_changed', '产品配置已改变，不能继续旧配置的准备任务。')
                active = self.tasks.get(change_id)
                if (current.get('current_change_id') != change_id or current.get('restore_reconciliation_required')
                        or not current.get('execution_supported', True) or not change or change.get('run_id')
                        or change['state'] != 'blocked' or (active and not active.done())):
                    raise DomainError('product_retry_not_allowed', '仅可恢复尚未启动运行的需求变更准备失败')
                tx.put('product_change', change_id, {**change, 'state': 'preparing', 'phase': 'environment',
                    'preparation_generation': change.get('preparation_generation', 1) + 1,
                    'blocking_reasons': []}, change['revision'])
                result = tx.put('product', product_id, {**current, 'state': 'preparing', 'phase': 'environment',
                    'blocking_reasons': []}, current['revision'])
                tx.event('product.change_preparation_retried', {'product_id': product_id, 'change_id': change_id})
                return result
            return await self.service.store.command('product.change.retry', key,
                {'product_id': product_id, 'change_id': change_id}, retry)

    async def prepare_change(self, change_id):
        change = await self.service.store.read('product_change', change_id)
        product = await self.service.store.read('product', change['product_id'])
        try:
            if product.get('deleted_at'):
                return
            if change.get('run_id') or product.get('current_change_id') != change_id:
                return
            restart = change.get('kind') == 'restart'
            if product.get('needs_restart') and not restart:
                return
            if change.get('config_revision', 1) != product.get('config_revision', 1):
                raise DomainError('product_configuration_changed', '产品配置已变化，不能继续旧版本的运行准备。')
            product = {**product, **change.get('product_snapshot', {})}
            if restart and change.get('project_id') and not product.get('project_id'):
                await self.service._change(product['id'], project_id=change['project_id'])
                product = {**product, 'project_id': change['project_id']}
            plans = [p for p in await self.service.store.list('plan')
                     if p.get('product_contract', {}).get('change_id') == change_id and p.get('started_run_id')]
            if len(plans) > 1:
                raise DomainError('change_run_ambiguous', '需求变更存在多个运行记录，需先核对')
            if plans:
                await self._attach_run(change, await self.service.store.read('run', plans[0]['started_run_id']))
                return
            prepared = self.service._local_view(await self.service.local.prepare(wait=True,
                required_targets=sorted(execution_targets(product))))
            if prepared.get('state') != 'ready':
                raise DomainError('local_execution_blocked', prepared.get('detail', '本机执行环境未准备完成'))
            output = self.service._owned_output(product)
            output.mkdir(parents=True, exist_ok=True)
            atomic_json(output / '.agentflow-product.json', {'product_id': product['id']})
            if restart and not change.get('project_id'):
                if product.get('creation_mode') == 'import':
                    raise DomainError('product_baseline_unavailable', '导入产品必须先建立现有仓库的提交基线。')
                project = await self.service.workflow.create_project({'name': product['name'],
                    'local_path': str(output / 'repository'), 'import_mode': 'initialize_managed',
                    'dirty_worktree_policy': 'require_clean'}, 'product-restart-project:' + change_id)
                await self.service._seed_starter(Path(project['local_path']), product['id'])
                baseline = await self.service.workflow._git(Path(project['local_path']), 'rev-parse', 'HEAD')
                change = await self._update_change(change_id, project_id=project['id'], base_commit=baseline)
                await self.service._change(product['id'], project_id=project['id'])
                product = {**product, 'project_id': project['id']}
            inputs = {} if restart else await self._stage_inputs(product, change['base_run_id'])
            targets = [c for c in prepared['target_configs'] if c['app_target'] in execution_targets(product)]
            if {c['app_target'] for c in targets} != execution_targets(product):
                raise DomainError('local_targets_missing', '缺少本次需求的具体目标执行配置')
            first_step = 'goal' if restart else 'prd'
            steps = list(STEPS[STEPS.index(first_step):STEPS.index('delivery') + 1])
            approvals = steps if change['review_mode'] == 'every_step' else ['prd', 'architecture', 'delivery'] if change['review_mode'] == 'milestones' else []
            payload = {'project_id': product['project_id'], 'goal':
                f"原产品目标（保留不变）：\n{product['goal']}\n\n本次需求变更：{change['title']}\n{change['description']}\n\n"
                + (f"验收标准：\n{change['acceptance_criteria']}\n\n" if change.get('acceptance_criteria') else '') +
                '在现有已提交代码和冻结文档基础上实施本次需求变更，保留未受影响的功能。可以新增、修改或删除本次明确要求调整的功能。从 PRD 开始更新完整文档，保留稳定需求编号并核对兼容性；检查、调整已有架构后继续开发和全部质量门禁。',
                'purpose': 'code_delivery', 'selection': {'mode': 'from_to', 'from_step': first_step, 'to_step': 'delivery'},
                'input_versions': [], 'stage_input_versions': inputs, 'approval_steps': approvals,
                'authorized_rework_steps': ['implementation', 'unit_test_implementation', 'integration_test_implementation'],
                'runtime_bindings': {**change['model_bindings'], 'coding_backend_id': str(uuid5(NAMESPACE_URL, 'agentflow:backend:codex_exec'))},
                'budget_limit': {'currency': 'USD', 'limit_micros': 0, 'cost_mode': 'request_limited',
                    'max_model_requests': change['max_model_requests'], 'max_active_seconds': change['max_active_seconds'],
                    'max_tool_calls': change['max_tool_calls']},
                'app_targets': sorted(execution_targets(product)), 'target_configs': targets,
                'product_contract': {'version': 2, 'stack': 'node_web_api', 'product_id': product['id'],
                    'language': change.get('language', DEFAULT_PRODUCT_LANGUAGE),
                    'display_name': product['name'] + ' · ' + change['title'],
                    'target': product['target'], 'targets': effective_targets(product), 'change_id': change_id,
                    'base_run_id': change['base_run_id'], 'start_stage': 'prd', 'architecture_policy': 'review_existing'}}
            if change.get('product_snapshot'):
                payload['product_contract'].update(product_snapshot=change['product_snapshot'],
                    config_revision=change['config_revision'])
            if restart:
                payload['goal'] = product['goal']
                payload['product_contract'].update(start_stage='goal', architecture_policy='design_for_current_goal',
                    display_name=product['name'], restart_id=change_id)
                # parent_run_id means a continuation sharing the old iteration's
                # budget. A full restart uses only the explicit historical link
                # in product_contract.base_run_id and creates a fresh iteration.
            elif 'document_baseline' in change:
                # Captured once when the change was accepted; preparation retries
                # never refreeze mutable current documents or alter legacy plans.
                payload['product_contract']['document_baseline'] = change['document_baseline']
            if change['source_commit']:
                payload.update(source_commit=change['source_commit'], source_ref=change['source_ref'])
            generation = change['preparation_generation']
            payload = await self.preserve_legacy_plan_language(payload)
            plan = await self.service.workflow.create_plan(payload, f'product-change-plan:{change_id}:{generation}')
            unique_refs = {ref['artifact_version_id']: ref for refs in inputs.values() for ref in refs}
            await self._update_change(change_id, plan_id=plan['id'], stage_input_versions=inputs,
                                      reused_input_versions=list(unique_refs.values()))
            if plan['base_commit'] != change['base_commit']:
                raise DomainError('project_baseline_changed', '工程提交在准备期间变化，请核对后创建新的需求变更')
            if plan['state'] != 'ready':
                raise DomainError('product_plan_blocked', '需求变更计划仍缺少必要输入', details=plan['missing_inputs'])
            run = await self.service.workflow.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']},
                                                        f'product-change-run:{change_id}:{generation}')
            await self._attach_run(change, run)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, DomainError):
                logger.exception('Product change preparation failed')
            message = error.message if isinstance(error, DomainError) else '需求变更准备失败，请查看平台日志'
            await self._update_change(change_id, state='blocked', phase='planning', blocking_reasons=[message])
            current = await self.service.store.read('product', product['id'])
            if current.get('current_change_id') == change_id:
                await self.service._change(product['id'], state='blocked', phase='planning', blocking_reasons=[message])

    async def _attach_run(self, change, run):
        def attach(tx):
            current = tx.get('product_change', change['id'])
            product = tx.get('product', change['product_id'])
            plan = tx.get('plan', run['plan_id'])
            if (product.get('current_change_id') != change['id'] or run['project_id'] != product['project_id']
                    or product.get('deleted_at') or current.get('config_revision', 1) != product.get('config_revision', 1)
                    or not plan or plan.get('product_contract', {}).get('change_id') != change['id']
                    or plan.get('product_contract', {}).get('product_id') != product['id']):
                raise DomainError('change_run_mismatch', '需求变更与运行身份不一致')
            first_step = 'goal' if current.get('kind') == 'restart' else 'prd'
            updated = tx.put('product_change', change['id'], {**current, 'run_id': run['id'], 'plan_id': run['plan_id'],
                'state': 'running', 'phase': first_step, 'blocking_reasons': []}, current['revision'])
            tx.put('product', product['id'], {**product, 'run_id': run['id'], 'plan_id': run['plan_id'],
                'run_ids': list(dict.fromkeys([*(product.get('run_ids') or []), run['id']])),
                'initial_run_id': product.get('initial_run_id') or run['id'], 'model_bindings': change['model_bindings'],
                'state': 'running', 'phase': first_step, 'blocking_reasons': [], 'needs_restart': False,
                'run_config_revision': current.get('config_revision', 1)}, product['revision'])
            tx.event('product.change_started', {'product_id': product['id'], 'change_id': change['id'], 'run_id': run['id']}, run_id=run['id'])
            return updated
        return await self.service.store.command('product.change.attach', run['id'], {'change_id': change['id'], 'run_id': run['id']}, attach)

    async def _update_change(self, identity, **changes):
        def update(tx):
            current = tx.get('product_change', identity)
            if all(current.get(k) == v for k, v in changes.items()):
                return current
            value = tx.put('product_change', identity, {**current, **changes}, current['revision'])
            tx.event('product.change_updated', {'change_id': identity, 'product_id': value['product_id'], 'state': value['state']}, run_id=value.get('run_id'))
            return value
        return await self.service.store.command('product.change.update', str(uuid4()), {'change_id': identity, **changes}, update)

    async def sync_change(self, product):
        if not product.get('current_change_id'):
            return
        change = await self.service.store.read('product_change', product['current_change_id'])
        if change and change.get('run_id') == product.get('run_id') and change.get('run_id'):
            changes = {key: product.get(key) for key in ('state', 'phase', 'blocking_reasons')}
            if product['state'] == 'completed':
                changes.update(delivery=product.get('delivery'), completed_at=utc_now())
            if any(change.get(k) != v for k, v in changes.items() if k != 'completed_at'):
                await self._update_change(change['id'], **changes)

    async def changes(self, product_id):
        await self.service.detail(product_id)
        return sorted([c for c in await self.service.store.list('product_change') if c['product_id'] == product_id],
                      key=lambda c: c['created_at'], reverse=True)
