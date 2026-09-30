"""A user goal becomes one resumable engineering Run and one verified release."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.failure_messages import failure_display
from agentflow.control.product_exports import ProductExporter
from agentflow.control.product_lifecycle import ProductLifecycle, effective_targets, execution_targets
from agentflow.control.product_models import (
    DEFAULT_PRODUCT_LANGUAGE,
    ModelSetupRequest,
    ProductLanguageRequest,
    ProductRequest,
    product_identity,
)
from agentflow.control.product_repair import ProductTestRepair
from agentflow.control.remediation import ReviewRemediation
from agentflow.domain.planning import STEPS
from agentflow.models.budget import account_id
from agentflow.models.profiles import ModelProfile
from agentflow.runtime.failures import (
    known_failure_reason,
    read_frozen_codex_failure,
    read_role_failure,
    refine_codex_failure,
    runtime_failure_code,
)
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.prelaunch import prelaunch_failure_code

logger = logging.getLogger(__name__)
BINDING_ID = 'default'
_STEP_LABELS = {
    'goal': '目标分析', 'research': '调研', 'prd': '产品需求', 'requirements': '需求拆解',
    'architecture': '架构设计', 'development_plan': '任务规划', 'implementation': '代码开发',
    'code_review': '代码审查', 'unit_test_plan': '单测方案', 'unit_test_implementation': '单测实现',
    'unit_test_execution': '单元测试', 'integration_test_strategy': '集成方案',
    'integration_test_implementation': '集成测试实现', 'integration_test_execution': '集成测试',
    'delivery': '代码交付',
}


class ProductService:
    def __init__(self, store, workflow, models, runtime, local_execution, secrets, settings, *, starter_root=None,
                 configuration=None):
        self.store, self.workflow, self.models, self.runtime = store, workflow, models, runtime
        self.local, self.secrets, self.settings = local_execution, secrets, settings
        self.configuration = configuration
        self.starter_root = Path(starter_root) if starter_root else Path(__file__).resolve().parents[1] / 'resources/web_api_starter'
        self.exporter = ProductExporter(store, workflow.artifacts, local_execution.nodes, settings.data_dir)
        self._tasks = {}
        self._loop_task = None
        self._closed = False
        self._runtime_cache = None
        self._runtime_checked = 0.0
        self.launcher = None
        self.test_repair = ProductTestRepair(store, workflow, nodes=local_execution.nodes)
        self.review_repair = ReviewRemediation(store, workflow)
        self._failure_versions = {}
        self.lifecycle = ProductLifecycle(self)
        from agentflow.control.product_management import ProductManagement
        self.management = ProductManagement(self)

    async def setup_status(self):
        profiles = await self.models.list_profiles()
        binding = await self.store.read('product_model_binding', BINDING_ID) or {}
        by_id = {p['model_profile_id']: p for p in profiles}
        # Existing accepted profiles remain usable without duplicate configuration.
        selected = {}
        requirements = []
        for key, protocol in [('role_model_profile_id', 'chat_completions'), ('coding_model_profile_id', 'responses')]:
            identity = binding.get(key)
            if self.configuration:
                configured = getattr(self.configuration.models, 'roles' if protocol == 'chat_completions' else 'coding')
                if not configured.enabled:
                    identity = None
            elif not identity:
                options = [p for p in profiles if p['acceptance_status'] == 'accepted'
                           and p['credential_status'] == 'configured' and protocol in p['protocols']]
                identity = options[0]['model_profile_id'] if len(options) == 1 else None
            selected[key] = identity
            profile = by_id.get(identity)
            if not profile or profile['acceptance_status'] != 'accepted' or profile['credential_status'] != 'configured' or protocol not in profile['protocols']:
                requirements.append({'code': key + '_missing', 'message': '请配置专业角色模型（Chat Completions）' if protocol == 'chat_completions'
                                     else '请配置编码模型（Responses）'})
        models_ready = not requirements
        if self._runtime_cache is None or time.monotonic() - self._runtime_checked > 30:
            try:
                self._runtime_cache = await self.runtime.probe()
            except Exception:
                self._runtime_cache = [{'backend': 'runtime', 'available': False}]
            self._runtime_checked = time.monotonic()
        for backend in self._runtime_cache:
            if not backend.get('available'):
                requirements.append({'code': 'runtime_missing', 'message': f"请安装或修复 {backend.get('backend', 'Agent')} 执行组件"})
        local = self._local_view(await self.local.status())
        if local.get('state') == 'blocked':
            requirements.append({'code': 'local_execution_blocked', 'message': local.get('detail', '本机执行环境需要修复')})
        configuration_status = {}
        if self.configuration:
            try:
                restart = self.configuration.restart_required()
                if restart:
                    requirements.append({'code': 'configuration_restart_required',
                        'message': '配置文件已修改，请执行 agentflow stop 后再执行 agentflow start。'})
            except DomainError as exc:
                restart = True
                requirements.append({'code': exc.code, 'message': exc.message})
            configuration_status = {'configuration_path': str(self.configuration.config_path),
                'configuration_fingerprint': self.configuration.fingerprint, 'restart_required': restart,
                'product_defaults': self.configuration.product.model_dump(mode='json')}
        return {**configuration_status, 'ready': models_ready and not requirements and local.get('state') == 'ready',
            'models_ready': models_ready, 'requirements': requirements, 'model_bindings': selected,
            'profiles': profiles, 'local_execution': local, 'cost_mode': 'request_limited',
            'cost_notice': '调用次数可设上限，0 表示不限次数。每次输出、工具与执行时长限制独立生效；实际费用由供应商计费，未配置价格时保持未知。'}

    async def configure_model(self, request: ModelSetupRequest, key: str):
        if self.configuration:
            return await self.configuration.update_model(request, key, self.models.registry, self.secrets)
        raw_key = request.api_key.get_secret_value() if request.api_key else None
        public = request.model_dump(exclude={'api_key'})
        safe_payload = {**public, 'credential_digest': hashlib.sha256(raw_key.encode()).hexdigest() if raw_key else None}
        identity = str(uuid5(NAMESPACE_URL, 'agentflow:model-setup:' + key))
        protocols = ['chat_completions', 'responses'] if request.role == 'both' else [
            'responses' if request.role == 'coding' else 'chat_completions']
        profile = ModelProfile(model_profile_id=identity, provider=request.provider, requested_model=request.model,
            accepted_api_model=request.model, acceptance_status='accepted', base_url=request.base_url,
            protocols=protocols, credential_reference='local:' + identity if raw_key else 'env:' + request.credential_env,
            max_output_tokens=request.max_output_tokens)
        def prepare(tx):
            return tx.put('model_setup', identity, {'profile_id': identity, 'request_fingerprint': canonical_digest(safe_payload)})
        await self.store.command('product.model.prepare', key, safe_payload, prepare)
        if raw_key:
            self.secrets.put(identity, raw_key)
        else:
            self.secrets.read(profile.credential_reference)
        await self.models.registry.register(profile, 'product-model:' + identity)
        def bind(tx):
            previous = tx.get('product_model_binding', BINDING_ID)
            current = dict(previous or {})
            if request.role in {'roles', 'both'}:
                current['role_model_profile_id'] = identity
            if request.role in {'coding', 'both'}:
                current['coding_model_profile_id'] = identity
            result = tx.put('product_model_binding', BINDING_ID, current, previous['revision'] if previous else None)
            tx.event('product.models_configured', {'role': request.role, 'profile_id': identity})
            return result
        binding = await self.store.command('product.model.bind', key, {'profile_id': identity, 'role': request.role}, bind)
        return {'profile_id': identity, 'configured': True, 'bindings': binding}

    async def start(self):
        self._closed = False
        if self.launcher:
            await self.launcher.start()
        self._loop_task = asyncio.create_task(self._loop(), name='agentflow-products')

    async def submit(self, request: ProductRequest, key: str):
        if request.creation_mode == 'import':
            return await self.lifecycle.submit_import(request, key)
        request_payload = request.model_dump(mode='json')
        # Omitted fields inherit file defaults; explicit fields are task choices.
        # Their wire representations can otherwise collapse to the same digest.
        fingerprint = canonical_digest({'version': 2, 'request': request.model_dump(mode='json', exclude_unset=True)})
        identity = product_identity(key)
        existing = await self.store.read('product', identity)
        if existing:
            if existing.get('request_fingerprint'):
                legacy = {field: request_payload[field] for field in ('name', 'goal', 'output_directory', 'target',
                    'review_mode', 'max_model_requests', 'role_model_profile_id', 'coding_model_profile_id')}
                expected = fingerprint if existing.get('request_fingerprint_version') == 2 else canonical_digest(legacy)
                same_request = existing['request_fingerprint'] == expected
            else:
                # Older product records already froze their selected models. A
                # retry must not silently inherit later global model bindings.
                original = existing.get('initial_request_snapshot', existing)
                same_request = all(original.get(name) == request_payload[name] for name in
                    ('name', 'goal', 'target', 'review_mode', 'max_model_requests',
                     'role_model_profile_id', 'coding_model_profile_id'))
                if request.output_directory:
                    same_request &= str(Path(request.output_directory).expanduser().resolve()) == existing['output_directory']
            if not same_request:
                raise DomainError('idempotency_conflict', '此提交标识已经用于不同的产品请求')
            if (existing.get('request_fingerprint_version') != 2 and 'language' in request.model_fields_set
                    and request.language != existing.get('initial_language', DEFAULT_PRODUCT_LANGUAGE)):
                raise DomainError('idempotency_conflict', '此提交标识已经用于不同的产品语言')
            return existing
        if self.configuration:
            if self.configuration.restart_required():
                raise DomainError('configuration_restart_required',
                    '配置文件已修改，请执行 agentflow stop 后再执行 agentflow start，再创建产品。', 409)
            defaults = self.configuration.product
            request = request.model_copy(update={field: getattr(defaults, field)
                for field in ('target', 'review_mode', 'max_model_requests', 'language') if field not in request.model_fields_set})
        selected_targets = request.targets or [request.target]
        request = request.model_copy(update={'targets': selected_targets, 'target': selected_targets[0]})
        if set(selected_targets) - {'web', 'api'}:
            return await self.lifecycle.register_unsupported(request, key, fingerprint)
        setup = await self.setup_status()
        if setup.get('restart_required'):
            raise DomainError('configuration_restart_required',
                '配置文件已修改，请重启平台后再创建产品。', 409)
        selected = {**setup['model_bindings']}
        for field in selected:
            if getattr(request, field):
                selected[field] = getattr(request, field)
        for field, protocol in [('role_model_profile_id', 'chat_completions'), ('coding_model_profile_id', 'responses')]:
            profile_id = selected.get(field)
            if not profile_id:
                raise DomainError('model_setup_required', '请先在 ~/.config/agentflow/config.toml 或工作台模型设置中配置专业角色和编码模型。', 409)
            profile = await self.models.registry.get(profile_id)
            profile.assert_accepted(protocol)
            self.secrets.read(profile.credential_reference)
        if any(r['code'] == 'runtime_missing' for r in setup['requirements']):
            raise DomainError('runtime_setup_required', '请先安装 OpenHands SDK 和受支持的 Codex CLI', 409)
        slug = re.sub(r'[^A-Za-z0-9_-]+', '-', request.name).strip('-')[:48] or 'product'
        output_root = self.configuration.product.output_root if self.configuration else Path.home() / 'AgentFlowProducts'
        output = Path(request.output_directory).expanduser() if request.output_directory else output_root / f'{slug}-{identity[:8]}'
        if not output.is_absolute() or output.is_symlink():
            raise DomainError('invalid_output_directory', '请选择绝对路径的产品输出目录', 422)
        output = output.resolve()
        data = self.settings.data_dir.resolve()
        if output == Path(output.anchor) or output == data or output in data.parents or data in output.parents:
            raise DomainError('protected_output_directory', '产品输出目录必须与平台数据目录分离', 422)
        if self.configuration:
            private_root = self.configuration.config_path.parent.resolve()
            if output == private_root or output in private_root.parents or private_root in output.parents:
                raise DomainError('protected_output_directory', '产品输出目录必须与平台配置目录分离', 422)
        payload = {**request.model_dump(mode='json'), 'request_fingerprint': fingerprint, 'request_fingerprint_version': 2,
                   'initial_language': request.language,
                   'max_active_seconds': self.configuration.product.max_active_seconds if self.configuration else 1800,
                   'max_tool_calls': self.configuration.product.max_tool_calls if self.configuration else 100,
                   'output_directory': str(output), 'model_bindings': selected}
        def create(tx):
            if any(p['output_directory'] == str(output) for p in tx.list('product')):
                raise DomainError('output_in_use', '该目录已经属于一个产品；请查看原产品或选择新目录')
            if output.exists() and not output.is_dir():
                raise DomainError('invalid_output_directory', '产品输出路径必须是目录', 422)
            if output.exists() and any(output.iterdir()):
                raise DomainError('output_not_empty', '创建产品需要一个新目录或空目录；不会覆盖现有文件')
            product = tx.put('product', identity, {**payload, 'state': 'preparing', 'run_id': None, 'project_id': None,
                'execution_supported': True, 'run_ids': [], 'initial_run_id': None, 'current_change_id': None,
                'phase': 'environment', 'blocking_reasons': [], 'delivery': None, 'created_at': utc_now()})
            tx.event('product.created', {'product_id': identity, 'name': request.name})
            return product
        return await self.store.command('product.create', key, {'request_fingerprint': fingerprint}, create)

    async def detail(self, product_id, *, management_view=None):
        product = await self.store.read('product', product_id)
        if not product:
            raise DomainError('not_found', 'Unknown product', 404)
        if (product.get('state') == 'blocked' and product.get('run_id') and not product.get('finalization_error')
                and not product.get('restore_reconciliation_required')):
            failed = [item for item in await self.store.list('work_item')
                      if item.get('run_id') == product['run_id'] and self._failed_work(item)]
            notice = await self._test_runtime_notice(product)
            if notice:
                product = {**product, 'blocking_reasons': [notice['message']]}
            elif failed:
                # Existing failed products get useful diagnostics without changing
                # their durable state, restarting work, or reading arbitrary logs.
                product = {**product, 'blocking_reasons': await self._failure_reasons(failed)}
        if self.launcher:
            product = {**product, 'launch': await self.launcher.status(product_id)}
        run = await self.store.read('run', product['run_id']) if product.get('run_id') else None
        change = await self.store.read('product_change', product['current_change_id']) if product.get('current_change_id') else None
        supported = product.get('execution_supported', not (set(effective_targets(product)) - {'web', 'api'}))
        product = {**product, 'targets': effective_targets(product), 'creation_mode': product.get('creation_mode', 'new'),
            'language': product.get('language', DEFAULT_PRODUCT_LANGUAGE),
            'deleted_at': product.get('deleted_at'), 'needs_restart': product.get('needs_restart', False),
            'config_revision': product.get('config_revision', 1),
            'run_config_revision': product.get('run_config_revision', 1 if product.get('run_id') else None),
            'execution_supported': supported,
            'run_ids': product.get('run_ids') or ([product['run_id']] if product.get('run_id') else []),
            'initial_run_id': product.get('initial_run_id') or product.get('run_id'),
            'current_change_id': product.get('current_change_id'), 'current_change': change,
            'can_add_change': bool(not product.get('deleted_at') and not product.get('needs_restart')
                and supported and product.get('project_id')
                and product['state'] in {'registered', 'completed', 'cancelled', 'blocked'}
                and (not run or run['execution_state'] in {'completed', 'cancelled'})
                and (not change or change['state'] not in {'preparing', 'running', 'waiting_approval'})
                and (product.get('launch') or {}).get('state') not in {'running', 'starting', 'execution_unknown'})}
        management = await self.management.describe(product, view=management_view)
        return {**product, 'management': management, 'can_add_change': product['can_add_change'] and management['can_edit']}

    async def _test_runtime_notice(self, product):
        notice = product.get('test_runtime_repair_required')
        if not notice:
            return None
        candidate = await self.store.read('candidate', notice.get('candidate_id'))
        run = await self.store.read('run', product.get('run_id'))
        if (candidate and run and candidate.get('run_id') == run['id']
                and candidate.get('run_input_fingerprint') == run['input_fingerprint']):
            return notice
        return None

    async def update_language(self, product_id: str, request: ProductLanguageRequest, key: str):
        """Change future defaults only; accepted runs and changes keep their snapshots."""
        async with self.lifecycle.lock(product_id):
            preview = await self.management._preview(product_id)
            def update(tx):
                current = tx.get('product', product_id)
                if not current:
                    raise DomainError('not_found', 'Unknown product', 404)
                if current['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', '产品状态已变化，请重新读取后设置语言')
                self.management._assert_available(tx, current, preview)
                initial = current.get('initial_language', current.get('language', DEFAULT_PRODUCT_LANGUAGE))
                if current.get('language') == request.language and current.get('initial_language') == initial:
                    return current
                result = tx.put('product', product_id, {**current, **self.management._metadata(current), 'language': request.language,
                    'initial_language': initial}, current['revision'])
                tx.event('product.language_updated', {'product_id': product_id, 'language': request.language,
                    'applies_to': 'future_iterations'})
                return result
            return await self.store.command('product.language', key,
                {'product_id': product_id, **request.model_dump(mode='json')}, update)

    @staticmethod
    def _failed_work(item):
        return (not item.get('archived') and item.get('required', True)
                and (item.get('status') in {'failed', 'blocked', 'execution_unknown'}
                     or item.get('quality_result') == 'failed'))

    async def _failure_reasons(self, items):
        messages = []
        for item in items[:10]:
            code = known_failure_reason(item.get('blocking_reason'))
            exit_code = None
            attempt_id = item.get('attempt_id')
            attempt = await self.store.read('attempt', attempt_id) if attempt_id else None
            current = bool(attempt and attempt.get('work_item_id') == item['id']
                and attempt.get('run_id') == item.get('run_id')
                and all(attempt.get(field) == item.get(field) and item.get(field) is not None
                        for field in ('generation', 'fencing_token', 'input_fingerprint')))
            if current:
                code = runtime_failure_code(attempt.get('runtime_failure_code')) or code
                supervised = await self.store.read('supervised_attempt', attempt_id)
                if supervised is None and code in {None, 'isolation_unverified'}:
                    code = await prelaunch_failure_code(self.store, self.settings.data_dir, attempt_id) or code
                matching = bool(supervised and supervised.get('attempt_id') == attempt_id
                    and supervised.get('run_id') == item.get('run_id')
                    and all(supervised.get(field) == item.get(field) for field in ('fencing_token', 'input_fingerprint')))
                if matching:
                    exit_code = supervised.get('exit_code')
                    code = code or known_failure_reason(supervised.get('reason'))
                    if not code and supervised.get('backend') == 'openhands_role':
                        code = read_role_failure(self.settings.data_dir, attempt_id)
                    if not code and supervised.get('backend') == 'codex_exec':
                        code = await read_frozen_codex_failure(self.store, self.settings.data_dir, attempt_id,
                            run_id=item['run_id'], work_item_id=item['id'], fencing_token=item['fencing_token'],
                            input_fingerprint=item['input_fingerprint'])
                code = code or known_failure_reason(attempt.get('summary'))
                if code == 'model_output_limit':
                    code = await refine_codex_failure(self.store, self.settings.data_dir, attempt_id,
                        fencing_token=item['fencing_token'], input_fingerprint=item['input_fingerprint'], fallback=code)
                if code == 'model_rate_limited':
                    account = await self.store.read('budget_account', account_id('run', item['run_id']))
                    if (account and account.get('owner_id') == item['run_id']
                            and type(account.get('max_requests')) is int and account['max_requests'] > 0
                            and account.get('request_count', 0) >= account.get('max_requests', 1)):
                        code = 'model_request_limit_reached'
            if item.get('status') == 'execution_unknown':
                code = 'execution_unconfirmed'
            if not code:
                if item.get('status') == 'execution_unknown':
                    code = 'execution_unconfirmed'
                elif item.get('quality_result') == 'failed':
                    code = ('review_failed' if item.get('step') == 'code_review' else 'test_failed'
                            if item.get('step') in {'unit_test_execution', 'integration_test_execution'} else 'quality_failed')
                else:
                    code = 'work_blocked' if item.get('status') == 'blocked' else 'worker_exited'
            code, message = await failure_display(self.store, self.settings, item, attempt if current else None, code)
            if code == 'worker_exited' and type(exit_code) is int and -255 <= exit_code <= 65535:
                message = f'Agent 执行进程异常退出（退出码 {exit_code}）。'
            messages.append(_STEP_LABELS.get(item.get('step'), '研发任务') + '：' + message)
        if len(items) > 10:
            messages.append(f'另有 {len(items) - 10} 项任务未通过，可在工作台查看。')
        return messages

    async def prepare_local(self):
        return self._local_view(await self.local.prepare())

    @staticmethod
    def _local_view(value):
        result = dict(value)
        if result.get('state') == 'not_prepared':
            result['state'] = 'unprepared'
        if not result.get('detail') and result.get('message'):
            result['detail'] = result['message']
        return result

    @staticmethod
    def _owned_output(product):
        output = Path(product['output_directory'])
        if not output.is_absolute() or output.is_symlink() or output.resolve() != output:
            raise DomainError('output_ownership_changed', '输出目录或父目录被替换为符号链接')
        if output.exists():
            if not output.is_dir():
                raise DomainError('output_changed', '输出路径不再是目录')
            marker = output / '.agentflow-product.json'
            if marker.exists() or marker.is_symlink():
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                    raise DomainError('output_ownership_changed', '输出目录标识不可验证')
                try:
                    matches = json.loads(marker.read_text()).get('product_id') == product['id']
                except (OSError, ValueError, AttributeError):
                    matches = False
                if not matches:
                    raise DomainError('output_ownership_changed', '输出目录标识发生变化')
            elif any(output.iterdir()):
                raise DomainError('output_changed', '等待准备期间输出目录被占用；不会覆盖已有文件')
        return output

    @staticmethod
    def _committed_run(tx, product):
        plans = [plan for plan in tx.list('plan')
                 if plan.get('product_contract', {}).get('product_id') == product['id']
                 and not plan.get('product_contract', {}).get('change_id')
                 and plan.get('project_id') == product.get('project_id') and plan.get('started_run_id')]
        if not plans:
            return None
        if len(plans) != 1:
            raise DomainError('product_preparation_unknown', '存在多个已启动研发记录，需要先核对原运行')
        run = tx.get('run', plans[0]['started_run_id'])
        if not run or run.get('plan_id') != plans[0]['id'] or run.get('project_id') != product.get('project_id'):
            raise DomainError('product_preparation_unknown', '已启动运行的持久身份无法验证')
        return run

    async def _recover_prepared_run(self, identity):
        observed = await self.store.read('product', identity)
        if (observed.get('restore_reconciliation_required') or observed.get('deleted_at')
                or observed.get('needs_restart') or observed.get('current_change_id')):
            return None
        if observed.get('run_id'):
            return observed
        if not any(plan.get('product_contract', {}).get('product_id') == identity and plan.get('started_run_id')
                   for plan in await self.store.list('plan')):
            return None
        def recover(tx):
            product = tx.get('product', identity)
            if (product.get('restore_reconciliation_required') or product.get('deleted_at')
                    or product.get('needs_restart') or product.get('current_change_id')):
                return {}
            if product.get('run_id'):
                return product
            run = self._committed_run(tx, product)
            if run is None:
                return {}
            updated = tx.put('product', identity, {**product, 'run_id': run['id'], 'plan_id': run['plan_id'],
                'state': 'cancelled' if run['execution_state'] == 'cancelled' else 'running',
                'phase': 'reconciling', 'blocking_reasons': []}, product['revision'])
            tx.event('product.run_recovered', {'product_id': identity, 'run_id': run['id']}, run_id=run['id'])
            return updated
        return await self.store.command('product.recover_run', str(uuid4()), {'product_id': identity}, recover) or None

    async def retry_prepare(self, identity, key):
        observed = await self.store.read('product', identity)
        if observed and observed.get('deleted_at'):
            raise DomainError('product_deleted', '请先从回收站恢复该产品。')
        if observed and observed.get('current_change_id'):
            change = await self.store.read('product_change', observed['current_change_id'])
            if change and not change.get('run_id'):
                return await self.lifecycle.retry_change(identity, key)
        if observed and observed.get('needs_restart'):
            raise DomainError('product_restart_required', '关键配置已改变，请从头运行。')
        def retry(tx):
            product = tx.get('product', identity)
            if not product:
                raise DomainError('not_found', 'Unknown product', 404)
            active = self._tasks.get(identity)
            if product.get('finalization_error') and product.get('run_id') and not product.get('restore_reconciliation_required'):
                run = tx.get('run', product['run_id'])
                if run and run.get('execution_state') == 'completed' and run.get('delivery_ids') and product['state'] == 'blocked':
                    self._owned_output(product)
                    value = tx.put('product', identity, {**product, 'state': 'running', 'phase': 'export',
                        'finalization_error': False, 'blocking_reasons': []}, product['revision'])
                    tx.event('product.export_retried', {'product_id': identity}, run_id=run['id'])
                    return value
            if product.get('creation_mode') == 'import' or product.get('execution_supported') is False:
                raise DomainError('product_registration_only', '导入或未支持平台不能走新建准备流程；请使用诊断或需求变更入口')
            if (product.get('restore_reconciliation_required') or product.get('run_id')
                    or product['state'] != 'blocked' or (active and not active.done())):
                raise DomainError('product_retry_not_allowed', '仅可重试尚未启动研发运行的准备失败；已有运行请使用原运行控制')
            self._owned_output(product)
            run = self._committed_run(tx, product)
            changes = {'state': 'preparing', 'phase': 'environment', 'blocking_reasons': []}
            if run:
                changes.update(run_id=run['id'], plan_id=run['plan_id'],
                               state='cancelled' if run['execution_state'] == 'cancelled' else 'running', phase='reconciling')
            value = tx.put('product', identity, {**product, **changes}, product['revision'])
            tx.event('product.preparation_retried', {'product_id': identity, 'reused_run_id': value.get('run_id')})
            return value
        return await self.store.command('product.retry_prepare', key, {'product_id': identity}, retry)

    async def _loop(self):
        while not self._closed:
            try:
                for identity, task in list(self.lifecycle.tasks.items()):
                    if task.done():
                        self.lifecycle.tasks.pop(identity)
                        if not task.cancelled():
                            task.result()
                for identity, task in list(self._tasks.items()):
                    if task.done():
                        self._tasks.pop(identity)
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            logger.exception('Product operation needs attention')
                for product in await self.store.list('product'):
                    if product.get('deleted_at') or product.get('restore_reconciliation_required') or product.get('finalization_error'):
                        continue
                    if product.get('current_change_id'):
                        change = await self.store.read('product_change', product['current_change_id'])
                        if change and not change.get('run_id'):
                            if change['state'] == 'preparing' and change['id'] not in self.lifecycle.tasks:
                                self.lifecycle.tasks[change['id']] = asyncio.create_task(self.lifecycle.prepare_change(change['id']))
                            continue
                    if product.get('needs_restart'):
                        continue
                    if product['state'] == 'preparing' and (product.get('creation_mode') == 'import'
                            or product.get('execution_supported') is False):
                        await self._change(product['id'], state='blocked', phase='diagnosis',
                            blocking_reasons=['导入或未支持平台不能走新建准备流程，请使用诊断或需求变更入口。'])
                        continue
                    if product['state'] == 'preparing' and product['id'] not in self._tasks:
                        self._tasks[product['id']] = asyncio.create_task(self._prepare_product(product['id']))
                    elif product.get('run_id') and product['state'] not in {'completed', 'cancelled'}:
                        async with self.lifecycle.lock(product['id']):
                            await self._advance(product)
                    elif product['state'] == 'blocked' and not product.get('run_id'):
                        await self._recover_prepared_run(product['id'])
                    if product.get('current_change_id'):
                        await self.lifecycle.sync_change(await self.store.read('product', product['id']))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Product reconciliation failed; durable state retained')
            await asyncio.sleep(.5)

    async def _change(self, identity, **changes):
        def update(tx):
            current = tx.get('product', identity)
            if all(current.get(k) == v for k, v in changes.items()):
                return current
            value = tx.put('product', identity, {**current, **changes}, current['revision'])
            tx.event('product.updated', {'product_id': identity, 'state': value['state'], 'phase': value.get('phase')}, run_id=value.get('run_id'))
            return value
        current = await self.store.read('product', identity)
        if all(current.get(k) == v for k, v in changes.items()):
            return current
        return await self.store.command('product.update', str(uuid4()), {'product_id': identity, **changes}, update)

    async def _prepare_product(self, identity):
        try:
            observed = await self.store.read('product', identity)
            if observed.get('restore_reconciliation_required') or observed.get('deleted_at') or observed.get('needs_restart'):
                return
            if await self._recover_prepared_run(identity):
                return
            product = await self.detail(identity)
            if (product.get('creation_mode') == 'import' or product.get('execution_supported') is False
                    or execution_targets(product) - {'web', 'api'}):
                raise DomainError('product_execution_unsupported', '导入登记或未支持平台不能按新建模板执行')
            required_targets = sorted(execution_targets(product))
            prepared = self._local_view(await self.local.prepare(wait=True, required_targets=required_targets))
            if prepared.get('state') != 'ready':
                raise DomainError('local_execution_blocked', prepared.get('detail', '本机 Web/API 构建测试环境尚未准备完成'))
            output = self._owned_output(product)
            output.mkdir(parents=True, exist_ok=True)
            marker = output / '.agentflow-product.json'
            atomic_json(marker, {'product_id': identity})
            project = await self.workflow.create_project({'name': product['name'], 'local_path': str(output / 'repository'),
                'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'product-project:' + identity)
            await self._change(identity, project_id=project['id'], phase='planning')
            await self._seed_starter(Path(project['local_path']), identity)
            targets = [c for c in prepared['target_configs'] if c['app_target'] in execution_targets(product)]
            if not targets or {c['app_target'] for c in targets} != execution_targets(product):
                raise DomainError('local_targets_missing', '本机执行器没有所需的 Web/API 目标配置')
            approval_steps = (list(STEPS[:STEPS.index('delivery') + 1]) if product['review_mode'] == 'every_step'
                else ['prd', 'architecture', 'delivery'] if product['review_mode'] == 'milestones' else [])
            payload = {'project_id': project['id'], 'goal': product['goal'], 'purpose': 'code_delivery',
                'selection': {'mode': 'from_to', 'from_step': 'goal', 'to_step': 'delivery'},
                'input_versions': [], 'approval_steps': approval_steps,
                'authorized_rework_steps': ['implementation', 'unit_test_implementation', 'integration_test_implementation'],
                'runtime_bindings': {**product['model_bindings'], 'coding_backend_id': str(uuid5(NAMESPACE_URL, 'agentflow:backend:codex_exec'))},
                'budget_limit': {'currency': 'USD', 'limit_micros': 0, 'max_model_requests': product['max_model_requests'],
                    'max_tool_calls': product.get('max_tool_calls', 100),
                    'max_active_seconds': product.get('max_active_seconds', 1800), 'cost_mode': 'request_limited'},
                'app_targets': sorted({c['app_target'] for c in targets}), 'target_configs': targets,
                'product_contract': {'version': 1, 'stack': 'node_web_api', 'product_id': identity,
                    'display_name': product['name'], 'target': product['target'],
                    'language': product.get('initial_language', DEFAULT_PRODUCT_LANGUAGE)}}
            payload = await self.lifecycle.preserve_legacy_plan_language(payload)
            plan = await self.workflow.create_plan(payload, 'product-plan:' + identity)
            await self._change(identity, plan_id=plan['id'])
            if plan['state'] != 'ready':
                raise DomainError('product_plan_blocked', '研发计划仍有缺失前提', details=plan['missing_inputs'])
            run = await self.workflow.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'product-run:' + identity)
            await self._change(identity, run_id=run['id'], initial_run_id=run['id'], run_ids=[run['id']],
                               state='running', phase='goal', blocking_reasons=[])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, DomainError):
                logger.exception('Product preparation failed')
            await self._change(identity, state='blocked', blocking_reasons=[exc.message if isinstance(exc, DomainError) else '产品准备失败，请查看平台日志'])

    async def _seed_starter(self, repository, identity):
        marker = repository / '.git/agentflow-starter.json'
        if marker.exists():
            if json.loads(marker.read_text()).get('product_id') != identity:
                raise DomainError('starter_ownership_changed', '工程初始化标识发生变化')
            return
        if not self.starter_root.is_dir():
            raise DomainError('starter_missing', '安装包缺少受支持的 Web/API 起步工程')
        for source in self.starter_root.rglob('*'):
            if source.is_symlink():
                raise DomainError('starter_corrupt', '起步工程不允许符号链接')
            if source.is_file():
                target = repository / source.relative_to(self.starter_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != source.read_bytes():
                    raise DomainError('starter_changed', '初始化过程中工程文件发生变化')
                if not target.exists():
                    shutil.copyfile(source, target)
        await self.workflow._git(repository, 'add', '--all')
        if await self.workflow._git(repository, 'status', '--porcelain'):
            await self.workflow._git(repository, '-c', 'user.name=AgentFlow', '-c', 'user.email=agentflow@localhost',
                'commit', '-m', 'Initialize supported Web/API engineering scaffold')
        atomic_json(marker, {'product_id': identity, 'commit': await self.workflow._git(repository, 'rev-parse', 'HEAD')})

    async def _advance(self, product):
        product = await self.store.read('product', product['id'])
        if product.get('restore_reconciliation_required') or product.get('deleted_at') or product.get('needs_restart'):
            return
        try:
            run = await self.workflow.run_detail(product['run_id'])
            if run['execution_state'] == 'paused':
                # An owner pause is also the safe boundary for product editing;
                # background repair must not resume work across that boundary.
                return
            if run['delivery_ids'] and run['execution_state'] == 'completed':
                delivery = await self.store.read('delivery', run['delivery_ids'][-1])
                try:
                    release = await self.exporter.export(product, delivery)
                except Exception as exc:
                    if not isinstance(exc, DomainError):
                        logger.exception('Confirmed product export failed')
                    await self._change(product['id'], state='blocked', phase='export', finalization_error=True,
                        blocking_reasons=[exc.message if isinstance(exc, DomainError) else
                            '代码已通过测试并交付 Git，但导出产品包失败。修复磁盘或平台错误后可重试导出，不会重新调用模型。'])
                    return
                await self._change(product['id'], state='completed', phase='delivered', delivery=release, blocking_reasons=[])
                return
            work = run['work_items']
            if run['execution_state'] == 'cancelled':
                await self._change(product['id'], state='cancelled', phase='cancelled')
            elif any(w['status'] == 'waiting_approval' for w in work):
                await self._change(product['id'], state='waiting_approval', phase=next(w['step'] for w in work if w['status'] == 'waiting_approval'))
            else:
                failed = [w for w in work if self._failed_work(w)]
                if failed:
                    notice = await self._test_runtime_notice(product)
                    if notice:
                        await self._change(product['id'], state='blocked', phase='test_runtime_repair_required',
                            blocking_reasons=[notice['message']])
                        return
                    if self._failure_versions.get(product['id']) != run['revision']:
                        self._failure_versions[product['id']] = run['revision']
                        for work_item in failed:
                            if work_item['step'] == 'code_review' and await self.review_repair.repair(work_item['id']):
                                await self._change(product['id'], state='running', phase='repairing', blocking_reasons=[])
                                return
                        test_repair = await self.test_repair.attempt(product)
                        if test_repair['scheduled']:
                            await self._change(product['id'], state='running', phase='repairing', blocking_reasons=[])
                            return
                        if test_repair.get('reason_code') == 'test_runtime_repair_required':
                            await self._change(product['id'], state='blocked', phase='test_runtime_repair_required',
                                blocking_reasons=[test_repair['message']])
                            return
                    await self._change(product['id'], state='blocked', phase=failed[0]['step'],
                        blocking_reasons=await self._failure_reasons(failed))
                else:
                    current = next((w for w in work if w['status'] in {'running', 'waiting_execution'}), None)
                    await self._change(product['id'], state='running', phase=current['step'] if current else 'scheduling', blocking_reasons=[])
        except DomainError as exc:
            await self._change(product['id'], state='blocked', blocking_reasons=[exc.message])

    async def close(self):
        self._closed = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for task in self.lifecycle.tasks.values():
            task.cancel()
        if self.lifecycle.tasks:
            await asyncio.gather(*self.lifecycle.tasks.values(), return_exceptions=True)
        if self.launcher:
            await self.launcher.close()
