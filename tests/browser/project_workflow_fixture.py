"""Isolated UI records; no scheduler or paid model is constructed."""
import json
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from agentflow.common import canonical_digest, utc_now
from agentflow.domain.planning import ROLES


def install_project_workflow_fixture(app, store, artifacts, directory, fixture_key):
    @app.post('/__fixture/project_versions')
    async def project_versions(request: Request):
        if request.headers.get('x-fixture-key') != fixture_key:
            return JSONResponse({}, 403)
        values = await request.json()
        if values.get('product_id'):
            def update(tx):
                product = tx.get('product', values['product_id'])
                if values.get('deleted'):
                    tx.put('product', product['id'], {**product, 'deleted_at': utc_now()}, product['revision'])
                else:
                    change_id = str(uuid4())
                    tx.put('product_change', change_id, {'product_id': product['id'], 'title': '后来提交的变更',
                        'description': '新版本不应打断旧版本浏览', 'state': 'preparing', 'start_stage': 'prd',
                        'kind': 'change', 'base_run_id': product['run_id'], 'created_at': utc_now()})
                    tx.put('product', product['id'], {**product, 'current_change_id': change_id}, product['revision'])
                return {'ok': True}
            return await store.command('fixture.project-version-update', str(uuid4()), values, update)
        product_id, imported_id = str(uuid4()), str(uuid4())
        project = await app.state.workflow.create_project({'name': '阅读清单源码', 'local_path': str(directory / product_id),
            'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, str(uuid4()))
        output = directory / 'product-docs'
        output.mkdir(exist_ok=True)
        (output / '.agentflow-product.json').write_text(json.dumps({'product_id': product_id}))
        run_ids = [str(uuid4()) for _ in range(3)]
        changes = [str(uuid4()), str(uuid4())]
        frozen = []
        for step in ('goal', 'research'):
            blob = await artifacts.put_bytes(json.dumps({'title': step, 'content': '真实版本记录的测试文档'}).encode())
            frozen.append((step, str(uuid4()), str(uuid4()), blob['id']))
        def seed(tx):
            refs = {}
            for step, work_id, artifact_id, digest in frozen:
                artifact = tx.put('artifact', artifact_id, {'run_id': run_ids[0], 'work_item_id': work_id,
                    'project_id': project['id'], 'step': step, 'generation': 1, 'digest': digest,
                    'media_type': 'application/json', 'name': 'openhands_final.json', 'stale': False})
                refs[artifact_id] = {'digest': digest, 'revision': artifact['revision']}
                tx.put('work_item', work_id, {'run_id': run_ids[0], 'project_id': project['id'], 'key': step,
                    'step': step, 'role': ROLES[step], 'status': 'completed', 'quality_result': 'unknown',
                    'generation': 1, 'fencing_token': 1, 'input_fingerprint': canonical_digest(step),
                    'dependencies': [], 'artifact_ids': [artifact_id], 'approval_required': False, 'required': True})
            for number, run_id in enumerate(run_ids):
                plan_id = str(uuid4())
                tx.put('plan', plan_id, {'plan_id': plan_id, 'project_id': project['id'], 'goal': '阅读清单',
                    'state': 'started', 'actual_steps': ['goal', 'research'] if number == 0 else ['prd'],
                    'input_fingerprint': canonical_digest(run_id), 'work_specs': [], 'missing_inputs': [],
                    'base_commit': project['base_commit'], 'app_targets': [], 'target_configs': [],
                    'product_contract': {'product_id': product_id, 'change_id': changes[number - 1] if number else None,
                        'base_run_id': run_ids[number - 1] if number else None},
                    'stage_reused_inputs': {'prd': list(refs)} if number else {},
                    'reused_input_versions': refs if number else {}})
                tx.put('run', run_id, {'run_id': run_id, 'plan_id': plan_id, 'project_id': project['id'],
                    'goal': ['阅读清单初始目标', '增加筛选功能', '增加导出功能'][number],
                    'display_name': ['阅读清单', '筛选收藏', '导出列表'][number],
                    'execution_state': 'completed' if number == 0 else 'running', 'quality_result': 'unknown',
                    'input_fingerprint': canonical_digest(run_id), 'purpose': 'artifact_only', 'delivery_ids': [],
                    'blocking_reasons': [], 'created_at': f'2026-09-0{number + 1}T00:00:00Z'})
                if number:
                    work_id = str(uuid4())
                    tx.put('work_item', work_id, {'run_id': run_id, 'project_id': project['id'], 'key': 'prd',
                        'step': 'prd', 'role': 'product', 'status': 'pending' if number == 2 else 'waiting_approval',
                        'quality_result': 'unknown', 'generation': 1, 'fencing_token': 1,
                        'input_fingerprint': canonical_digest(work_id), 'dependencies': [], 'artifact_ids': [],
                        'approval_required': number == 1, 'required': True})
                    tx.put('product_change', changes[number - 1], {'product_id': product_id,
                        'title': '筛选收藏' if number == 1 else '导出列表', 'description': '测试版本',
                        'run_id': run_id, 'plan_id': plan_id, 'base_run_id': run_ids[number - 1],
                        'state': 'running', 'kind': 'change', 'start_stage': 'prd', 'created_at': f'2026-09-0{number + 1}T00:00:00Z'})
            tx.put('product', product_id, {'name': '阅读清单', 'goal': '帮助用户管理阅读', 'project_id': project['id'],
                'initial_run_id': run_ids[0], 'run_id': run_ids[-1], 'run_ids': run_ids,
                'current_change_id': changes[-1], 'output_directory': str(output), 'state': 'running',
                'target': 'web', 'targets': ['web'], 'blocking_reasons': []})
            tx.put('product', imported_id, {'name': '已登记工具', 'goal': '已有本地工具', 'project_id': None,
                'creation_mode': 'import', 'diagnosis': {'diagnosis_kind': 'static'}, 'run_ids': [],
                'output_directory': str(directory / 'import-output'), 'state': 'registered', 'target': 'web',
                'targets': ['web'], 'blocking_reasons': []})
            return {'product_id': product_id, 'imported_id': imported_id, 'run_ids': run_ids, 'changes': changes}
        return await store.command('fixture.project-versions', str(uuid4()), {}, seed)
