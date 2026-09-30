"""Real recovery HTTP endpoints and Git state; no scheduler or model execution."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import uvicorn

from agentflow.control.api import create_app
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.settings import Settings
from agentflow.storage import Store


async def main(directory, mode):
    import socket
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    settings = Settings(data_dir=directory / 'controller', port=listener.getsockname()[1],
        dashboard_dir=Path(os.environ['AGENTFLOW_BROWSER_DASHBOARD_DIR']))
    store = Store(settings.data_dir)
    await store.start()
    app = create_app(settings, store=store)
    workflow = app.state.workflow
    model = str(uuid4())
    await store.command('fixture', 'profile', {}, lambda tx: tx.put('model_profile', model, {
        'model_profile_id': model, 'acceptance_status': 'accepted', 'credential_status': 'configured'}))
    project = await workflow.create_project({'name': '断点恢复验证', 'local_path': str(directory / 'project'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
    plan = await workflow.create_plan({'project_id': project['id'], 'goal': '保留已完成目标，并从中断的调研步骤继续',
        'purpose': 'artifact_only', 'selection': {'mode': 'selected', 'selected_steps': ['goal', 'research', 'prd']},
        'approval_steps': [], 'authorized_rework_steps': ['goal', 'research', 'prd'], 'input_versions': [],
        'runtime_bindings': {'role_model_profile_id': model}, 'app_targets': [],
        'budget_limit': {'currency': 'USD', 'limit_micros': 0, 'cost_mode': 'request_limited',
                         'max_model_requests': 20, 'max_tool_calls': 30, 'max_active_seconds': 300}}, 'plan')
    run = await workflow.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'start')
    goal = await workflow.claim_next(run['id'], 'fixture', 'goal')
    blob = await workflow.artifacts.put_bytes(b'{"title":"Completed goal","content":"Preserve this accepted result."}')
    await workflow.finish_attempt(goal['attempt']['id'], {'execution_status': 'completed', 'quality_result': 'unknown',
        'input_fingerprint': goal['attempt']['input_fingerprint'], 'fencing_token': goal['attempt']['fencing_token']},
        'finish-goal', verified_artifacts=[{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}])
    if mode != 'pending':
        research = await workflow.claim_next(run['id'], 'fixture', 'research')
        await workflow.finish_attempt(research['attempt']['id'], {'execution_status': 'failed', 'quality_result': 'unknown',
            'input_fingerprint': research['attempt']['input_fingerprint'], 'fencing_token': research['attempt']['fencing_token'],
            'runtime_failure_code': 'model_connection_failed'}, 'fail-research', verified_artifacts=[])
    current = await store.read('run', run['id'])
    await workflow.control_run(run['id'], {'action': 'pause', 'expected_revision': current['revision'], 'reason': 'Fixture paused'}, 'pause')
    await BudgetLedger(store).setup_accounts(run['id'], run['iteration_id'], 0, 0,
        run_max_requests=20, iteration_max_requests=20)
    if mode in {'cancelled', 'cancelled-resumable'}:
        current = await store.read('run', run['id'])
        await workflow.control_run(run['id'], {'action': 'cancel', 'expected_revision': current['revision'],
            'reason': 'Fixture cancelled after work stopped'}, 'cancel')
    if mode in {'exhausted', 'cancelled'}:
        def exhaust(tx):
            for kind, owner in [('run', run['id']), ('iteration', run['iteration_id'])]:
                account = tx.get('budget_account', account_id(kind, owner))
                tx.put('budget_account', account['id'], {**account, 'request_count': 20}, account['revision'])
            return {}
        await store.command('fixture', 'exhaust', {}, exhaust)

    @app.get('/api/v1/product_setup')
    async def setup():
        return {'models_ready': False, 'product_defaults': {'target': 'web', 'language': 'zh-CN', 'max_model_requests': 200},
                'ready': False, 'profiles': [], 'requirements': [],
                'role_model_profile_id': None, 'coding_model_profile_id': None,
                'local_execution': {'state': 'not_prepared', 'target_configs': []}}

    @app.get('/api/v1/products')
    async def products():
        return {'items': []}

    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, lifespan='off'))
    print(json.dumps({'origin': settings.origin, 'bootstrap': app.state.tokens.bootstrap_code, 'run_id': run['id']}), flush=True)
    try:
        await server.serve(sockets=[listener])
    finally:
        await store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--mode', choices=['failed', 'pending', 'exhausted', 'cancelled', 'cancelled-resumable'], default='failed')
    args = parser.parse_args()
    asyncio.run(main(args.directory, args.mode))
