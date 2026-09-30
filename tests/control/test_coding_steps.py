"""Finite coding output with real Git checkpoints, gated final completion and shared budgets."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.coding_steps import output_schema
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.budget import BudgetLedger
from agentflow.repository import RepositoryAdapter
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def coding_env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'data', max_coding_steps=3)
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
    workflow = WorkflowService(store, artifacts, settings)
    project = await workflow.create_project({'name': 'bounded coding', 'local_path': str(tmp_path / 'project'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
    fingerprint = canonical_digest('bounded fixture')
    def seed(tx):
        tx.put('plan', 'plan', {'authorized_rework_steps': ['implementation'], 'reused_inputs': []})
        tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
            'execution_state': 'running', 'quality_result': 'unknown', 'input_fingerprint': fingerprint,
            'base_commit': project['base_commit'], 'base_ref': project['base_ref'], 'delivery_ids': [],
            'blocking_reasons': [], 'budget_limit': {'max_active_seconds': 100, 'max_tool_calls': 12}})
        base = {'run_id': 'run', 'project_id': project['id'], 'generation': 1, 'fencing_token': 0,
            'input_fingerprint': fingerprint, 'policy_fingerprint': fingerprint, 'required': True,
            'status': 'pending', 'quality_result': 'unknown', 'approval_required': True,
            'artifact_ids': [], 'payload': {}, 'attempt_id': None}
        tx.put('work_item', 'code', {**base, 'key': 'implementation', 'step': 'implementation',
            'role': 'development', 'dependencies': [], 'write_paths': ['feature.py']})
        tx.put('work_item', 'review', {**base, 'key': 'code_review', 'step': 'code_review',
            'role': 'review', 'dependencies': ['code'], 'write_paths': []})
        return {}
    await store.command('fixture', 'seed', {}, seed)
    await BudgetLedger(store).setup_accounts('run', 'iteration', 10000, 10000)
    env = SimpleNamespace(store=store, settings=settings, workflow=workflow, repository=RepositoryAdapter(),
        artifacts=artifacts, project=project, root=tmp_path)
    env.scheduler = Scheduler(workflow, store, None, None, settings)
    yield env
    await store.close()


async def task_for(env):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'code'
    work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
    source, commit = await env.scheduler._source(run, work)
    control = await env.scheduler.coding_steps.prepare(run, work, attempt, commit, 512)
    workspace = env.root / attempt['id']
    await env.repository.clone_snapshot(source, workspace, commit)
    task = {'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run', 'step': 'implementation',
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': work['write_paths'],
        'output_schema': output_schema(), 'coding_step': control}
    return task


async def execute(env, task, result, change=None, *, seconds=8, tools=2, known=True):
    workspace = Path(task['workspace'])
    if change is not None:
        (workspace / 'feature.py').write_text(change)
    folder = env.settings.data_dir / 'attempt_artifacts' / task['attempt_id']
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / 'codex_final.json'
    final.write_text(json.dumps(result))
    class FixtureRuntime:
        async def execute_task(self, _task):
            return {'execution_status': 'completed', 'result': result, 'summary': 'fixture completed',
                'artifacts': [{'path': str(final)}], 'active_seconds': seconds, 'observed_tool_calls': tools,
                'tool_observation_complete': known}
    env.scheduler.runtime = FixtureRuntime()
    await env.scheduler._execute_existing(task)
    return await env.store.read('work_item', 'code')


async def test_small_steps_keep_progress_but_do_not_complete_work_or_bypass_approval(coding_env):
    env = coding_env
    first = await task_for(env)
    original_accounts = await env.store.list('budget_account')
    result = await execute(env, first, {'summary': 'first function', 'status': 'continue',
        'next_action': 'add the second function'}, 'def first():\n    return 1\n')
    assert result['status'] == 'pending' and result['generation'] == 2
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'
    assert not await env.store.list('approval')
    old_attempt = await env.store.read('attempt', first['attempt_id'])
    assert old_attempt['coding_step_complete'] and old_attempt['work_complete'] is False
    second = await task_for(env)
    assert second['coding_step']['max_output_tokens'] == 512
    assert second['coding_step']['max_active_seconds'] == 92
    assert second['coding_step']['max_tool_calls'] == 10
    assert (Path(second['workspace']) / 'feature.py').read_text().startswith('def first')
    result = await execute(env, second, {'summary': 'all functions ready', 'status': 'complete', 'next_action': ''},
        'def first():\n    return 1\n\ndef second():\n    return 2\n')
    assert result['status'] == 'waiting_approval'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    approval = next(a for a in await env.store.list('approval') if not a['stale'])
    await env.workflow.decide(approval['id'], {'decision': 'approve', 'expected_revision': approval['revision'],
        'expected_fingerprint': approval['fingerprint']}, 'approve')
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'review'
    assert await env.store.list('budget_account') == original_accounts


async def test_final_confirmation_can_reuse_prior_changed_checkpoint_without_rewriting_files(coding_env):
    env = coding_env
    first = await task_for(env)
    await execute(env, first, {'summary': 'code saved', 'status': 'continue', 'next_action': 'verify completion'}, 'READY = True\n')
    second = await task_for(env)
    result = await execute(env, second, {'summary': 'verified original task complete', 'status': 'complete', 'next_action': ''})
    assert result['status'] == 'waiting_approval'
    snapshot = await env.store.read('code_snapshot', second['attempt_id'])
    assert first['source_commit'] in snapshot['parent_commit_oids']


async def test_complete_with_a_handoff_note_preserves_it_without_starting_another_coding_step(coding_env):
    env = coding_env
    task = await task_for(env)
    note = '仓储由另一个子任务完成；合并后再运行集成测试。'
    result = await execute(env, task, {'summary': '当前模块已完成', 'status': 'complete', 'next_action': note},
                           'VALUE = 1\n')
    assert result['status'] == 'waiting_approval'
    assert result['generation'] == 1 and result['attempt_id'] == task['attempt_id']
    assert not await env.store.list('coding_step_checkpoint')
    assert len(await env.store.list('attempt')) == 1
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'
    artifacts = [a for a in await env.store.list('artifact') if a['name'] == 'codex_final.json']
    assert json.loads(await env.artifacts.read(artifacts[0]['digest']))['next_action'] == note


@pytest.mark.parametrize('kind', ['no_change', 'malformed', 'scope', 'unknown_usage'])
async def test_invalid_progress_never_opens_downstream(coding_env, kind):
    env = coding_env
    task = await task_for(env)
    content = {'summary': 'partial', 'status': 'continue', 'next_action': 'continue small step'}
    if kind == 'malformed':
        content['next_action'] = ''
    if kind == 'scope':
        (Path(task['workspace']) / 'outside.py').write_text('UNAUTHORIZED = True\n')
    result = await execute(env, task, content, None if kind == 'no_change' else 'READY = True\n', known=kind != 'unknown_usage')
    assert result['status'] == 'blocked'
    assert not await env.store.list('coding_step_checkpoint')
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'


@pytest.mark.parametrize('limit', ['steps', 'time', 'tools'])
async def test_progress_does_not_reset_work_execution_budget(coding_env, limit):
    env = coding_env
    task = await task_for(env)
    budget_id = task['coding_step']['budget_id']
    if limit == 'steps':
        def cap(tx):
            row = tx.get('coding_work_budget', budget_id)
            return tx.put('coding_work_budget', budget_id, {**row, 'max_steps': 1}, row['revision'])
        await env.store.command('fixture', 'cap', {}, cap)
    await execute(env, task, {'summary': 'saved', 'status': 'continue', 'next_action': 'another step'},
        'STEP = 1\n', seconds=100 if limit == 'time' else 8, tools=12 if limit == 'tools' else 2)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    source, commit = await env.scheduler._source(claim['run'], claim['work_item'])
    with pytest.raises(DomainError) as error:
        await env.scheduler.coding_steps.prepare(claim['run'], claim['work_item'], claim['attempt'], commit, 512)
    assert error.value.code == 'coding_budget_exhausted'
    assert (source / 'feature.py').read_text() == 'STEP = 1\n'


async def test_stale_or_corrupt_checkpoint_cannot_be_used_as_new_source(coding_env):
    env = coding_env
    task = await task_for(env)
    await execute(env, task, {'summary': 'saved', 'status': 'continue', 'next_action': 'next'}, 'STEP = 1\n')
    work = await env.store.read('work_item', 'code')
    snapshot = await env.store.read('code_snapshot', task['attempt_id'])
    def corrupt(tx):
        row = tx.get('code_snapshot', snapshot['id'])
        return tx.put('code_snapshot', row['id'], {**row, 'tree_oid': 'f' * 40}, row['revision'])
    await env.store.command('fixture', 'corrupt', {}, corrupt)
    with pytest.raises(DomainError) as error:
        await env.scheduler._source(await env.store.read('run', 'run'), work)
    assert error.value.code == 'invalid_coding_checkpoint'


async def test_process_restart_reopens_exact_next_step_and_accounts_once(coding_env):
    env = coding_env
    task = await task_for(env)
    await execute(env, task, {'summary': 'durable', 'status': 'continue', 'next_action': 'next'}, 'STEP = 1\n')
    budgets = await env.store.list('coding_work_budget')
    assert len(await env.store.list('coding_step_usage')) == 1
    # A new controller instance can read the same immutable checkpoint.
    env.scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    following = await task_for(env)
    assert following['coding_step']['step_number'] == 2
    assert following['source_commit'] != task['source_commit']
    assert await env.store.list('coding_work_budget') == budgets


async def test_cancel_requested_never_turns_partial_progress_into_another_step(coding_env):
    env = coding_env
    task = await task_for(env)
    def cancel(tx):
        work = tx.get('work_item', 'code')
        return tx.put('work_item', 'code', {**work, 'status': 'cancel_requested'}, work['revision'])
    await env.store.command('fixture', 'cancel', {}, cancel)
    result = await execute(env, task, {'summary': 'partial', 'status': 'continue', 'next_action': 'next'}, 'PARTIAL = 1\n')
    assert result['status'] == 'cancelled'
    assert not await env.store.list('coding_step_checkpoint')
    assert len(await env.store.list('coding_step_usage')) == 1


async def test_actual_codex_writes_two_small_steps_under_fixed_finite_output_limit(coding_env):
    import platform
    import threading
    from datetime import UTC, datetime, timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from pydantic import SecretStr

    from agentflow.adapters.codex import CodexExecAdapter
    from agentflow.models.profiles import AttemptContext, ModelProfile
    from agentflow.models.provider import ModelProvider
    from agentflow.runtime.contracts import TaskEnvelope
    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    from agentflow.runtime.supervisor import Supervisor

    executable = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
    if platform.system() != 'Darwin' or not executable.is_file():
        pytest.skip('Requires installed Codex and the verified macOS sandbox')
    env = coding_env
    calls = []
    accepted_limits = []
    model = 'gpt-5.4'
    profile = ModelProfile(model_profile_id='local-fixture', provider='local_test', requested_model=model,
        accepted_api_model=model, acceptance_status='accepted', base_url='http://127.0.0.1:1/v1',
        protocols=['responses'], credential_reference='test-only', allow_loopback_upstream=True, max_output_tokens=512)
    context = AttemptContext(attempt_id='fixture', run_id='run', iteration_id='iteration',
        model_profile_id='local-fixture', fencing_token=1, input_fingerprint=canonical_digest('fixture'),
        expires_at=(datetime.now(UTC) + timedelta(minutes=3)).isoformat(), max_model_requests=8, max_output_tokens=512)
    def response_item(item, number):
        response = {'id': f'resp_{number}', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': model, 'output': [item], 'usage': {'input_tokens': 20, 'output_tokens': 64, 'total_tokens': 84}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_item.added', 'output_index': 0,
                   'item': {**item, **({'input': ''} if item['type'] == 'custom_tool_call' else {'content': []})}}]
        if item['type'] == 'custom_tool_call':
            events.append({'type': 'response.custom_tool_call_input.delta', 'output_index': 0,
                'item_id': item['id'], 'delta': item['input']})
        else:
            part = {'type': 'output_text', 'text': '', 'annotations': []}
            events.extend([{'type': 'response.content_part.added', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'part': part},
                {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'delta': item['content'][0]['text']},
                {'type': 'response.output_text.done', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'text': item['content'][0]['text']}])
        events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                   {'type': 'response.completed', 'response': response}]
        return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            normalized, limit = ModelProvider().normalize_request(profile, context, 'responses', body)
            assert normalized['max_output_tokens'] == limit == 512
            accepted_limits.append(limit)
            calls.append(body)
            number = len(calls)
            if number in (1, 3):
                patch = ('*** Begin Patch\n*** Add File: feature.py\n+VALUE = 1\n*** End Patch' if number == 1 else
                         '*** Begin Patch\n*** Update File: feature.py\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch')
                item = {'id': f'patch_{number}', 'type': 'custom_tool_call', 'call_id': f'call_{number}',
                    'name': 'apply_patch', 'input': patch}
            else:
                result = {'summary': 'small change saved', 'status': 'continue' if number == 2 else 'complete',
                          'next_action': 'set VALUE to 2' if number == 2 else ''}
                item = {'id': f'msg_{number}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': json.dumps(result), 'annotations': []}]}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(response_item(item, number))
            self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    supervisor = Supervisor(env.store, env.settings.data_dir)
    adapter = CodexExecAdapter(supervisor, MacSeatbeltSandbox(env.settings.data_dir / 'sandbox_profiles'), executable)
    class RealRuntime:
        async def execute_task(self, task):
            envelope = TaskEnvelope(attempt_id=task['attempt_id'], operation_id=task['attempt_id'],
                work_item_id='code', run_id='run', iteration_id='iteration', role='development',
                goal='Current invocation: make only the specified small change, then return the required short progress JSON.',
                input_fingerprint=task['input_fingerprint'], fencing_token=task['fencing_token'],
                workspace=Path(task['workspace']),
                artifact_dir=env.settings.data_dir / 'attempt_artifacts' / canonical_digest(task['attempt_id']).split(':')[1],
                allowed_write_roots=[Path(task['workspace']) / 'feature.py'], allow_code_write=True,
                model_profile_id='local-fixture', model=model, proxy_base_url=f'http://127.0.0.1:{server.server_port}/v1',
                proxy_token=SecretStr('scoped-fixture-token'), max_active_seconds=30,
                max_output_tokens=512, max_tool_calls=10, output_schema=output_schema())
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            return await adapter.collect_artifacts(envelope.attempt_id, envelope)
    env.scheduler.runtime = RealRuntime()
    try:
        first = await task_for(env)
        await env.scheduler._execute_existing(first)
        work = await env.store.read('work_item', 'code')
        assert work['status'] == 'pending', work
        second = await task_for(env)
        await env.scheduler._execute_existing(second)
        work = await env.store.read('work_item', 'code')
        assert work['status'] == 'waiting_approval', work
        assert (Path(second['workspace']) / 'feature.py').read_text() == 'VALUE = 2\n'
        assert accepted_limits == [512] * 4 and len(calls) == 4
        budget = (await env.store.list('coding_work_budget'))[0]
        assert budget['step_count'] == 2 and budget['observed_tool_calls'] >= 2 and not budget['uncertain']
    finally:
        await supervisor.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


async def test_cancellation_during_snapshot_collection_never_schedules_next_step(coding_env, monkeypatch):
    env = coding_env
    task = await task_for(env)
    original = env.scheduler.repository.freeze_workspace
    async def cancel_after_freeze(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        def cancel(tx):
            work = tx.get('work_item', 'code')
            return tx.put('work_item', 'code', {**work, 'status': 'cancel_requested'}, work['revision'])
        await env.store.command('fixture', 'cancel-after-freeze', {}, cancel)
        return snapshot
    monkeypatch.setattr(env.scheduler.repository, 'freeze_workspace', cancel_after_freeze)
    work = await execute(env, task, {'summary': 'partial saved', 'status': 'continue', 'next_action': 'next'}, 'VALUE = 1\n')
    assert work['status'] == 'cancelled'
    assert not await env.store.list('coding_step_checkpoint')


async def test_recovery_after_partial_step_keeps_its_lineage_base_without_resetting_budget(coding_env):
    env = coding_env
    first = await task_for(env)
    await execute(env, first, {'summary': 'all code saved', 'status': 'continue', 'next_action': 'confirm completion'}, 'READY = True\n')
    second = await task_for(env)
    await env.store.command('fixture', 'context-second', {}, lambda tx: tx.put('dispatch_context', second['attempt_id'], {'task': second}))
    await env.scheduler.coding_steps.account(second, {'active_seconds': 4, 'observed_tool_calls': 0, 'tool_observation_complete': True})
    await env.workflow.block_attempt(second['attempt_id'], 'final response interrupted', 'blocked-second')
    prior = await env.store.read('code_snapshot', first['attempt_id'])
    run = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['code'], 'reason': 'test restore'}, 'restore-lineage')
    def recovery(tx):
        tx.put('code_snapshot', 'recovered-response', {'run_id': 'run', 'work_item_id': 'code',
            'base_oid': second['source_commit'], 'commit_oid': prior['commit_oid'], 'tree_oid': prior['tree_oid'],
            'repository_path': prior['repository_path'], 'source_attempt_id': second['attempt_id'],
            'source_generation': second['coding_step']['generation'], 'source_fencing_token': second['fencing_token'],
            'source_input_fingerprint': second['input_fingerprint']})
        work = tx.get('work_item', 'code')
        return tx.put('work_item', 'code', {**work, 'payload': {**work['payload'],
            'recovery_checkpoint_id': 'recovered-response', 'repair_base_snapshot_id': 'recovered-response'}}, work['revision'])
    await env.store.command('fixture', 'recovered-response', {}, recovery)
    claim = await env.workflow.claim_next('run', 'fixture', 'claim-restored')
    control = await env.scheduler.coding_steps.prepare(claim['run'], claim['work_item'], claim['attempt'], prior['commit_oid'], 512)
    assert control['base_commit'] == first['source_commit']
    assert control['max_active_seconds'] == 88
    assert control['step_number'] == 3


async def test_multi_step_branch_can_assemble_with_unchanged_parallel_sibling(coding_env):
    env = coding_env
    base = env.project['base_commit']
    sibling_repo = env.root / 'sibling'
    await env.repository.clone_snapshot(Path(env.project['local_path']), sibling_repo, base)
    (sibling_repo / 'other.py').write_text('OTHER = 3\n')
    sibling = await env.repository.freeze_workspace(sibling_repo, base, 'parallel sibling')
    def graph(tx):
        code = tx.get('work_item', 'code')
        tx.put('work_item', 'code', {**code, 'approval_required': False, 'kind': 'stage_child', 'parent_stage_id': 'aggregate'}, code['revision'])
        common = {key: value for key, value in code.items() if key not in {'id', 'revision'}}
        tx.put('work_item', 'sibling', {**common, 'key': 'sibling', 'status': 'completed', 'approval_required': False,
            'kind': 'stage_child', 'parent_stage_id': 'aggregate', 'write_paths': ['other.py'], 'attempt_id': 'side-attempt'})
        tx.put('code_snapshot', 'side-attempt', {'run_id': 'run', 'work_item_id': 'sibling', 'generation': 1,
            'commit_oid': sibling['commit_oid'], 'tree_oid': sibling['tree_oid'], 'base_oid': base,
            'repository_path': str(sibling_repo), 'parent_commit_oids': [], 'stale': False})
        tx.put('work_item', 'aggregate', {**common, 'key': 'aggregate', 'kind': 'aggregation',
            'dependencies': ['code', 'sibling'], 'expanded_child_ids': ['code', 'sibling'],
            'original_dependencies': [], 'original_write_paths': ['.'], 'approval_required': False, 'write_paths': []})
        review = tx.get('work_item', 'review')
        tx.put('work_item', 'review', {**review, 'dependencies': ['aggregate']}, review['revision'])
        return {}
    await env.store.command('fixture', 'parallel-graph', {}, graph)
    first = await task_for(env)
    await execute(env, first, {'summary': 'first', 'status': 'continue', 'next_action': 'second'}, 'VALUE = 1\n')
    second = await task_for(env)
    result = await execute(env, second, {'summary': 'complete', 'status': 'complete', 'next_action': ''}, 'VALUE = 2\n')
    assert result['status'] == 'completed'
    snapshot = await env.store.read('code_snapshot', second['attempt_id'])
    assert snapshot['base_oid'] == base
    aggregate = await env.store.read('work_item', 'aggregate')
    path, _ = await env.scheduler._source(await env.store.read('run', 'run'), aggregate)
    assert (path / 'feature.py').read_text() == 'VALUE = 2\n'
    assert (path / 'other.py').read_text() == 'OTHER = 3\n'
    assert (await env.store.read('work_item', 'sibling'))['attempt_id'] == 'side-attempt'


async def test_verified_continuation_clears_resolved_run_blockers(coding_env):
    env = coding_env
    task = await task_for(env)
    def old_blocker(tx):
        run = tx.get('run', 'run')
        return tx.put('run', 'run', {**run, 'blocking_reasons': ['implementation: blocked']}, run['revision'])
    await env.store.command('fixture', 'old-blocker', {}, old_blocker)
    result = await execute(env, task, {'summary': 'verified progress', 'status': 'continue', 'next_action': 'next case'}, 'VALUE = 1\n')
    assert result['status'] == 'pending'
    assert (await env.store.read('run', 'run'))['blocking_reasons'] == []


async def _stopped_coding_process_proof(env, first):
    import psutil

    from agentflow.runtime.launcher import atomic_json

    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': first['attempt_id']}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    process = {'attempt_id': first['attempt_id'], 'operation_id': first['attempt_id'], 'nonce': 'fixture',
        'pid': 1073741824, 'process_started_at': 1.0,
        'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()}), 'fencing_token': first['fencing_token']}
    atomic_json(directory / 'result.json', {**process, 'execution_status': 'completed', 'exit_code': 0})
    def seed(tx):
        run = tx.get('run', 'run')
        limit = {**run['budget_limit'], 'max_model_requests': 1000, 'currency': 'USD',
                 'limit_micros': 10000, 'cost_mode': 'request_limited'}
        if not tx.get('iteration', 'iteration'):
            tx.put('run', 'run', {**run, 'budget_limit': limit}, run['revision'])
            tx.put('iteration', 'iteration', {'project_id': env.project['id'],
                'budget_limit': {**limit, 'max_model_requests': 10000}})
        tx.put('dispatch_context', first['attempt_id'], {'task': first})
        tx.put('supervised_attempt', first['attempt_id'], {**process, 'state': 'completed', 'run_id': 'run',
            'directory': str(directory), 'input_fingerprint': first['input_fingerprint']})
        return {}
    await env.store.command('fixture', 'recovery-launch-proof:' + first['attempt_id'], {}, seed)


async def _exhausted_before_dispatch(env):
    first = await task_for(env)
    await _stopped_coding_process_proof(env, first)
    await execute(env, first, {'summary': 'first tests saved', 'status': 'continue', 'next_action': 'implement case two'},
                  'FIRST_TEST = True\n', tools=12)
    second = await env.workflow.claim_next('run', 'fixture', 'exhausted-claim')
    _, commit = await env.scheduler._source(second['run'], second['work_item'])
    with pytest.raises(DomainError, match='额度'):
        await env.scheduler.coding_steps.prepare(second['run'], second['work_item'], second['attempt'], commit, 512)
    await env.workflow.block_attempt(second['attempt']['id'], 'tool allowance exhausted', 'exhausted-block',
                                     failure_code='coding_budget_exhausted')
    return first, second


@pytest.mark.parametrize('tampered', [False, True])
async def test_recovery_without_a_new_edit_keeps_prior_small_step_contributions(coding_env, tampered):
    from agentflow.control.recovery import RunRecoveryService
    from agentflow.runtime.workspace import WorkspaceManager

    env = coding_env
    first = await task_for(env)
    await _stopped_coding_process_proof(env, first)
    await execute(env, first, {'summary': 'all assigned tests saved', 'status': 'continue',
        'next_action': 'confirm this work is complete'}, 'FIRST_TEST = True\n')
    second = await task_for(env)
    # The actual recovery path uses a registered workspace and stopped process
    # evidence, not the unrestricted Git directories used by most unit fixtures.
    path = await WorkspaceManager(env.settings.data_dir).create_clone(
        Path(first['workspace']), second['source_commit'], second['attempt_id'])
    second['workspace'] = str(path)
    await _stopped_coding_process_proof(env, second)
    await execute(env, second, {'summary': 'wrongly continuing after completion',
        'status': 'continue', 'next_action': 'work on another module'}, tools=1)
    work = await env.store.read('work_item', 'code')
    assert work['status'] == 'blocked' and work['runtime_failure_code'] == 'coding_no_progress'
    assert not await env.store.read('code_snapshot', second['attempt_id'])
    assert not (await env.repository.collect_diff(path, second['source_commit']))['has_changes']
    if tampered:
        def change(tx):
            context = tx.get('dispatch_context', second['attempt_id'])
            task = {**context['task'], 'coding_step': {**context['task']['coding_step'], 'base_commit': 'f' * 40}}
            return tx.put('dispatch_context', context['id'], {'task': task}, context['revision'])
        await env.store.command('fixture', 'tampered-baseline', {}, change)
    before = await env.store.list('coding_work_budget')
    run = await env.store.read('run', 'run')
    recovery = RunRecoveryService(env.store, env.workflow)
    request = {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'code'}
    if tampered:
        with pytest.raises(DomainError) as error:
            await recovery.recover('run', request, 'no-edit-recovery')
        assert error.value.code == 'recovery_checkpoint_invalid'
        assert not await env.store.list('run_recovery')
        return
    receipt = await recovery.recover('run', request, 'no-edit-recovery')
    assert receipt['checkpoint']['kind'] == 'stopped_workspace'
    assert env.repository._integrity(path, receipt['checkpoint']['commit_oid']) == env.repository._integrity(
        path, second['source_commit'])
    restored = await task_for(env)
    assert (Path(restored['workspace']) / 'feature.py').read_text() == 'FIRST_TEST = True\n'
    assert restored['coding_step']['base_commit'] == first['source_commit']
    assert await env.store.list('coding_work_budget') == before
    completed = await execute(env, restored, {'summary': 'assigned work confirmed', 'status': 'complete',
        'next_action': ''}, tools=1)
    assert completed['status'] == 'waiting_approval'


async def test_budget_recovery_before_dispatch_keeps_verified_code_and_next_action(coding_env):
    from agentflow.control.recovery import RunRecoveryService

    env = coding_env
    first, second = await _exhausted_before_dispatch(env)
    recovery = RunRecoveryService(env.store, env.workflow)
    blocked = (await recovery.options('run'))['retry_options'][0]
    assert not blocked['eligible']
    assert blocked['checkpoint']['kind'] == 'prior_code_snapshot'
    assert blocked['checkpoint']['commit_oid'] != first['source_commit']
    def additional_tools(tx):
        budget = tx.get('coding_work_budget', first['coding_step']['budget_id'])
        return tx.put('coding_work_budget', budget['id'], {**budget, 'max_tool_calls': 24}, budget['revision'])
    await env.store.command('fixture', 'owner-grant-tools', {}, additional_tools)
    before = await env.store.list('coding_work_budget')
    run = await env.store.read('run', 'run')
    request = {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'code'}
    result = await recovery.recover('run', request, 'resume-saved-step')
    snapshot = await env.store.read('code_snapshot', result['checkpoint']['snapshot_id'])
    assert snapshot['source_attempt_id'] == first['attempt_id'] != second['attempt']['id']
    assert snapshot['source_coding_checkpoint_id'] == 'coding-step-' + first['attempt_id']
    assert await recovery.recover('run', request, 'resume-saved-step') == result
    # A second cancellation before dispatch must retain the same original source.
    run = await env.store.read('run', 'run')
    stopped = await env.workflow.control_run('run', {'expected_revision': run['revision'],
        'action': 'cancel', 'reason': 'fixture cancellation before dispatch'}, 'cancel-restored')
    result = await recovery.recover('run', {'expected_revision': stopped['revision'],
        'mode': 'retry', 'work_item_id': 'code'}, 'resume-again')
    new_task = await task_for(env)
    assert new_task['source_commit'] == result['checkpoint']['commit_oid']
    assert (Path(new_task['workspace']) / 'feature.py').read_text() == 'FIRST_TEST = True\n'
    assert new_task['coding_step']['previous_summary'] == 'first tests saved'
    assert new_task['coding_step']['next_action'] == 'implement case two'
    assert new_task['coding_step']['step_number'] == 2
    assert new_task['coding_step']['max_tool_calls'] == 12
    assert new_task['coding_step']['max_active_seconds'] == 92
    assert new_task['coding_step']['base_commit'] == first['source_commit']
    assert await env.store.list('coding_work_budget') == before
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'


@pytest.mark.parametrize('field,value', [('attempt_id', 'wrong-attempt'), ('generation', 10),
                                       ('budget_id', 'other-budget'), ('tree_oid', 'f' * 40)])
async def test_pre_dispatch_recovery_rejects_mismatched_coding_checkpoint(coding_env, field, value):
    from agentflow.control.recovery import RunRecoveryService

    env = coding_env
    first, _ = await _exhausted_before_dispatch(env)
    def corrupt(tx):
        point = tx.get('coding_step_checkpoint', 'coding-step-' + first['attempt_id'])
        return tx.put('coding_step_checkpoint', point['id'], {**point, field: value}, point['revision'])
    await env.store.command('fixture', 'corrupt-continuation', {}, corrupt)
    options = await RunRecoveryService(env.store, env.workflow).options('run')
    blocked = options['retry_options'][0]
    assert not blocked['eligible']
    assert any(reason['code'] == 'recovery_checkpoint_invalid' for reason in blocked['blockers'])
    assert not await env.store.list('run_recovery')


async def test_timeout_reports_this_attempt_and_cumulative_allowance(coding_env):
    env = coding_env
    task = await task_for(env)
    async def timed_out(_):
        return {'execution_status': 'failed', 'runtime_failure_code': 'worker_timeout',
                'summary': 'Agent execution timed out', 'artifacts': [],
                'active_seconds': 100.05, 'observed_tool_calls': 2, 'tool_observation_complete': True}
    env.scheduler.runtime = SimpleNamespace(execute_task=timed_out)
    await env.scheduler._execute_existing(task)
    attempt = await env.store.read('attempt', task['attempt_id'])
    assert attempt['runtime_failure_code'] == 'worker_timeout'
    assert '本工作累计执行' in attempt['summary'] and '/ 100.0 秒' in attempt['summary']
    assert '本次执行可用时长为 100.0 秒' in attempt['summary']
    assert '不会清零' in attempt['summary']
