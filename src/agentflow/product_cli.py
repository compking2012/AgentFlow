"""Human-oriented CLI actions, all using the real owner API."""
from __future__ import annotations

import asyncio
import re
import sys
import webbrowser

from agentflow.common import DomainError
from agentflow.control.owner_client import OwnerClient
from agentflow.control.owner_ipc import launch_url, socket_path
from agentflow.control.product_models import product_identity
from agentflow.control.submissions import Submission

STATE_LABELS = {'preparing': '准备中', 'running': '研发中', 'waiting_approval': '等待审核',
                'registered': '已登记',
                'blocked': '需要处理', 'completed': '已交付', 'cancelled': '已取消', 'stopping': '停止中',
                'stopped': '已停止', 'execution_unknown': '等待核对'}
PHASE_LABELS = {'environment': '本机环境', 'planning': '规划', 'goal': '目标分析', 'research': '调研',
    'prd': '产品需求', 'requirements': '需求拆解', 'architecture': '架构设计',
    'development_plan': '任务规划', 'implementation': '代码开发', 'code_review': '代码审查',
    'unit_test_plan': '单测方案', 'unit_test_implementation': '单测实现', 'unit_test_execution': '单元测试',
    'integration_test_strategy': '集成方案', 'integration_test_implementation': '集成测试实现',
    'integration_test_execution': '集成测试', 'delivery': '交付', 'export': '导出',
    'delivered': '交付完成', 'repairing': '修复中', 'scheduling': '调度中', 'reconciling': '核对状态'}


def print_product(product):
    print(f"{product['id']}  {product.get('name', '')}  {STATE_LABELS.get(product['state'], product['state'])}")
    if product.get('phase'):
        print('阶段：' + PHASE_LABELS.get(product['phase'], product['phase']))
    for reason in product.get('blocking_reasons', []):
        print('待处理：' + reason)
    if product.get('delivery'):
        print('交付目录：' + product['delivery']['path'])
        print('启动产品：agentflow launch ' + product['id'])


async def run_product_command(args, configuration):
    if args.command == 'run':
        goal = args.goal.strip()
        if not 8 <= len(goal) <= 16000:
            raise DomainError('invalid_goal', '请用 8 至 16000 个字符描述产品目标。', 422)
        if args.name is not None and not 1 <= len(args.name.strip()) <= 100:
            raise DomainError('invalid_name', '产品名称需为 1 至 100 个字符。', 422)
    try:
        client = await OwnerClient.connect(configuration.settings, start=args.command in {'start', 'run'}, discover_active=True)
    except DomainError as exc:
        if exc.code == 'controller_unavailable' and args.command in {'status', 'stop'}:
            print('平台未运行。使用 agentflow start 打开工作台。')
            return
        raise
    try:
        if args.command == 'start':
            await asyncio.to_thread(webbrowser.open, await launch_url(client.data_dir))
            print('工作台已打开：' + client.origin)
            print('配置文件：' + str(configuration.config_path))
            setup = await client.request('GET', '/api/v1/product_setup')
            if setup.get('restart_required'):
                print('配置文件已修改，重启平台后才能创建新产品：agentflow stop，然后 agentflow start。')
            if not setup.get('models_ready'):
                print('请先在配置文件中填写模型接口；修改后执行 agentflow stop，再执行 agentflow start。')
        elif args.command == 'run':
            goal = args.goal.strip()
            name = args.name.strip() if args.name is not None else re.sub(r'\s+', ' ', goal)[:30]
            task_input = {'goal': goal, 'name': args.name,
                          'output': str(args.output.expanduser().absolute()) if args.output else None}
            defaults = configuration.product
            payload = {'name': name, 'goal': goal, 'output_directory': task_input['output'],
                'target': defaults.target, 'review_mode': defaults.review_mode,
                'language': defaults.language,
                'max_model_requests': defaults.max_model_requests}
            # The original payload and identity survive a lost response. Repeating
            # this command recovers it without extra flags or a second product.
            with Submission(client.data_dir, task_input, payload) as intent:
                try:
                    product = await client.request('POST', '/api/v1/products', intent.payload, key=intent.key)
                    if (not isinstance(product, dict) or product.get('id') != product_identity(intent.key)
                            or product.get('state') not in {'preparing', 'running', 'waiting_approval', 'blocked', 'completed', 'cancelled'}):
                        raise DomainError('owner_response_invalid', '提交回执无法核对；请再次运行同一命令恢复原请求。', 502)
                except DomainError as exc:
                    if not intent.recovered and 400 <= exc.status < 500 and exc.code != 'idempotency_conflict':
                        # A definite rejection is safe to correct and resubmit with
                        # current defaults. A replay's rejection cannot disprove
                        # an earlier, still-uncertain successful submission.
                        intent.acknowledged()
                    else:
                        print('提交记录已保留。再次运行同一命令可核对原请求，不会重复创建产品。', file=sys.stderr)
                    raise
                except Exception:
                    print('提交记录已保留。再次运行同一命令可核对原请求，不会重复创建产品。', file=sys.stderr)
                    raise
                intent.acknowledged()
            print('产品：' + product['id'], flush=True)
            previous = None
            try:
                while True:
                    product = await client.request('GET', '/api/v1/products/' + product['id'])
                    state = (product['state'], product.get('phase'), tuple(product.get('blocking_reasons', [])))
                    if state != previous:
                        print(f"{STATE_LABELS.get(state[0], state[0])} · {PHASE_LABELS.get(state[1], state[1] or '')}", flush=True)
                        if state[0] == 'waiting_approval':
                            print('在工作台完成审核后将继续；agentflow start 可打开工作台。', flush=True)
                        previous = state
                    if product['state'] in {'completed', 'blocked', 'cancelled'}:
                        break
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                print(f"已退出观察，研发保留在后台。agentflow status {product['id']} 可查看进展。", file=sys.stderr)
                raise
            print_product(product)
            if product['state'] == 'blocked':
                raise SystemExit(2)
        elif args.command == 'status':
            if args.product_id:
                print_product(await client.request('GET', '/api/v1/products/' + args.product_id))
            else:
                products = (await client.request('GET', '/api/v1/products'))['items']
                if not products:
                    print('还没有产品。使用 agentflow run "产品目标" 开始。')
                for product in products:
                    print_product(product)
        elif args.command == 'launch':
            result = await client.request('POST', '/api/v1/products/' + args.product_id + '/launch', {})
            print(result.get('url') or STATE_LABELS.get(result['state'], result['state']))
        elif args.product_id:
            result = await client.request('POST', '/api/v1/products/' + args.product_id + '/stop', {})
            print(STATE_LABELS.get(result['state'], result['state']))
        else:
            channel = socket_path(client.data_dir)
            old_identity = channel.stat().st_ino if channel.exists() else None
            await client.request('POST', '/api/v1/controller/shutdown', {})
            for _ in range(300):
                try:
                    if channel.stat().st_ino != old_identity:
                        break
                except FileNotFoundError:
                    break
                await asyncio.sleep(.1)
            else:
                raise DomainError('controller_stopping', '平台正在停止并保存任务状态，请稍后再启动。', 503)
            print('平台已停止。')
    finally:
        await client.close()
