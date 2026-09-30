"""Coverage repair stays bound to original authors, snapshots and formal gates."""
from copy import deepcopy
from uuid import uuid4

import pytest
from test_review_contract_repair import schedule, setup_batch
from test_workflow import flow as flow

from agentflow.common import DomainError, canonical_digest
from agentflow.control.coding_steps import CodingSteps


async def add_coverage_authority(store, result):
    """Keep the compact fixture's files while adding accepted independent plans."""
    def seed(tx):
        batch = tx.get('review_contract_repair', 'batch')
        context = deepcopy(batch['context'])
        context['test_paths'] = ['tests/api.spec.mjs', 'tests/web.spec.mjs']
        for index, (owner_id, step, plan_step, quote) in enumerate([
            ('api-tests', 'unit_test_implementation', 'unit_test_plan', 'Verify real HTTP and cascade deletion.'),
            ('web-tests', 'integration_test_implementation', 'integration_test_strategy', 'Assert detail mastery.')]):
            owner = tx.get('work_item', owner_id)
            tx.put('work_item', owner_id, {**owner, 'step': step, 'role': 'unit_test' if index == 0 else 'integration_test'}, owner['revision'])
            next(row for row in context['owners'] if row['work_item_id'] == owner_id)['step'] = step
            artifact = tx.put('artifact', plan_step, {'run_id': 'run', 'step': plan_step, 'digest': plan_step + '-digest', 'stale': False})
            context['accepted_documents'].append({'artifact_id': artifact['id'], 'step': plan_step,
                'digest': artifact['digest'], 'revision': artifact['revision'], 'text': quote})
            context['accepted_requirements'].append({'artifact_id': artifact['id'], 'step': plan_step,
                'requirement_id': 'C1', 'text': quote})
            result['actions'][index].update(classification='test_coverage_extension', migrations=[],
                requirement_refs=[{'artifact_id': artifact['id'], 'requirement_id': 'C1', 'quote': quote}])
        # The UI finding needs both production and Web test work.
        result['actions'].append({**deepcopy(result['actions'][1]),
            'finding_id': result['actions'][2]['finding_id'], 'evidence_paths': result['actions'][2]['evidence_paths']})
        return tx.put('review_contract_repair', 'batch', {**batch, 'context': context,
            'context_digest': canonical_digest(context)}, batch['revision'])
    await store.command('fixture.coverage-authority', 'batch', {}, seed)


async def test_mixed_coverage_repairs_use_normal_original_test_steps_and_remaining_budgets(flow):
    _, result = await setup_batch(flow, author_approval=True)
    await add_coverage_authority(flow[1], result)
    def spent(tx):
        return tx.put('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'), {
            'run_id': 'run', 'work_item_id': 'api-tests', 'base_commit': 'commit', 'max_steps': 5,
            'max_active_seconds': 100, 'max_tool_calls': 100, 'active_seconds': 17.0,
            'observed_tool_calls': 8, 'step_count': 2, 'uncertain': False})
    original = await flow[1].command('fixture.coverage-budget', 'spent', {}, spent)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    work = [await flow[1].read('work_item', key) for key in batch['repair_work_item_ids']]
    assert {(w['payload']['review_contract_owner'], w['step'], tuple(w['write_paths'])) for w in work} == {
        ('api-tests', 'unit_test_implementation', ('tests/api.spec.mjs',)),
        ('web-tests', 'integration_test_implementation', ('tests/web.spec.mjs',)),
        ('web', 'implementation', ('public/index.html',))}
    assert all(w['approval_required'] for w in work)
    assert len(batch['actions']) == 4 and len(work) == 3
    assert await flow[1].read('coding_work_budget', original['id']) == original
    unit = next(w for w in work if w['step'] == 'unit_test_implementation')
    budget = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', unit['id']))
    assert (budget['max_steps'], budget['max_active_seconds'], budget['max_tool_calls']) == (3, 83, 92)
    assert budget['review_contract_owner_budget'] == original['id']
    assert (await flow[1].read('work_item', 'api-tests'))['status'] == 'pending'
    assert not await flow[1].list('check') and not await flow[1].list('candidate')


async def test_coverage_prompt_delivers_only_assigned_plan_evidence_and_keeps_test_config_readonly(flow, monkeypatch):
    import json

    from agentflow.control.scheduler import Scheduler

    _, result = await setup_batch(flow)
    await add_coverage_authority(flow[1], result)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    work = [await flow[1].read('work_item', key) for key in batch['repair_work_item_ids']]
    item = next(w for w in work if w['step'] == 'integration_test_implementation')
    scheduler = Scheduler(flow[0], flow[1], None, None, flow[0].settings)
    async def context(*args, **kwargs):
        return {'directory': flow[0].settings.data_dir, 'review_phase_contract': None, 'text': ''}
    monkeypatch.setattr(scheduler.stage_context, 'build', context)
    run = await flow[1].read('run', 'run')
    prompt = await scheduler._prompt({**run, 'goal': 'Existing application'}, item, 'commit')
    assigned = json.loads(prompt.split('Actions:\n', 1)[1].split('\nFinal response contract:', 1)[0])
    assert assigned == item['payload']['review_contract_actions']
    assert 'Assert detail mastery.' in prompt and 'Verify real HTTP and cascade deletion.' not in prompt
    assert 'commit agentflow.project.json' not in prompt
    assert 'new_expected' not in prompt


async def test_coverage_guard_receipts_and_merged_snapshot_preserve_independent_formal_gates(flow):
    from test_review_contract_lifecycle import (
        finish_diagnostic,
        finish_repairs_and_assembly,
        lifecycle_source,
    )

    env = await lifecycle_source(flow, coverage=True)
    validation, merged = await finish_repairs_and_assembly(env)
    checks = await env.store.list('test_coverage_check')
    assert len(checks) == 2 and all(row['report']['ok'] for row in checks)
    assert not await env.store.list('test_migration_check')
    diagnostic = await finish_diagnostic(env, validation, restart=True)
    assert diagnostic['state'] == 'passed' and diagnostic['source']['commit_oid'] == merged['commit_oid']
    assert (await env.store.read('review_contract_repair', 'batch'))['state'] == 'reviewing'
    assert not await env.store.list('check') and not await env.store.list('candidate')
    assert (await env.store.read('work_item', 'formal-unit'))['status'] == 'pending'


@pytest.mark.parametrize('tamper', ['missing_receipt', 'wrong_generation', 'wrong_context', 'merged_test_changed'])
async def test_coverage_validation_rejects_missing_or_stale_receipts_and_changed_assembly(flow, monkeypatch, tamper):
    from test_review_contract_lifecycle import finish_repairs_and_assembly, lifecycle_source

    env = await lifecycle_source(flow, coverage=True)
    validation, merged = await finish_repairs_and_assembly(env)
    check = (await env.store.list('test_coverage_check'))[0]
    if tamper == 'merged_test_changed':
        work = await env.store.read('work_item', check['work_item_id'])
        path = env.source.parent / 'changed-assembly'
        await env.repository.clone_snapshot(merged['repository_path'], path, merged['commit_oid'])
        test_file = path / work['write_paths'][0]
        test_file.write_text(test_file.read_text() + '\n// unguarded merge change\n')
        changed = await env.repository.freeze_workspace(path, merged['commit_oid'], 'Changed assembly')
    def damage(tx):
        if tamper == 'merged_test_changed':
            batch = tx.get('review_contract_repair', 'batch')
            assembly = tx.get('work_item', batch['assembly_work_item_id'])
            source = tx.get('code_snapshot', assembly['attempt_id'])
            tx.put('code_snapshot', source['id'], {**source, **changed, 'repository_path': str(path)}, source['revision'])
        elif tamper == 'missing_receipt':
            pass
        else:
            field, value = ('generation', check['generation'] + 1) if tamper == 'wrong_generation' else ('context_digest', 'foreign')
            tx.put('test_coverage_check', check['id'], {**check, field: value}, check['revision'])
        return {}
    await env.store.command('fixture.coverage-damage', tamper, {}, damage)
    if tamper == 'missing_receipt':
        read = env.store.read
        async def missing(kind, identity):
            return None if kind == 'test_coverage_check' else await read(kind, identity)
        monkeypatch.setattr(env.store, 'read', missing)
    with pytest.raises(DomainError) as caught:
        await env.core.validate_or_poll(validation)
    assert caught.value.code == 'test_coverage_invalid'
    assert not await env.store.list('review_diagnostic')


async def test_coverage_guard_validates_frozen_commit_and_receipt_cannot_be_replaced(flow):
    from test_review_contract_lifecycle import lifecycle_source

    env = await lifecycle_source(flow, coverage=True)
    batch = await env.store.read('review_contract_repair', 'batch')
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in batch['repair_work_item_ids']]
    claim = next(c for c in claims if c['work_item']['payload']['review_contract_kind'] == 'test_coverage_extension')
    work, attempt = claim['work_item'], claim['attempt']
    folder = env.source.parent / 'coverage-action'
    await env.repository.clone_snapshot(env.source, folder, env.snapshot['commit_oid'])
    path = folder / work['write_paths'][0]
    original = path.read_text()
    path.write_text(original.replace('actual()', 'fake()'))
    rejected = await env.repository.freeze_workspace(folder, env.snapshot['commit_oid'], 'Changed old assertion')
    source = {**rejected, 'repository_path': str(folder)}
    path.write_text(original.replace('});\n', 'expect(1).toBe(1);});\n'))
    task = {**attempt, 'attempt_id': attempt['id']}
    with pytest.raises(DomainError) as caught:
        await env.core.validate_action(task, source)
    assert caught.value.code == 'test_coverage_invalid'
    assert not await env.store.list('test_coverage_check')
    accepted = await env.repository.freeze_workspace(folder, rejected['commit_oid'], 'Add coverage')
    source.update(accepted)
    await env.core.validate_action(task, source)
    receipt = await env.store.read('test_coverage_check', attempt['id'])
    await env.core.validate_action(task, source)
    assert await env.store.read('test_coverage_check', attempt['id']) == receipt
    path.write_text(path.read_text().replace('});\n', 'expect(2).toBe(2);});\n'))
    different = await env.repository.freeze_workspace(folder, accepted['commit_oid'], 'Different coverage')
    with pytest.raises(DomainError):
        await env.core.validate_action(task, {**different, 'repository_path': str(folder)})
    assert await env.store.read('test_coverage_check', attempt['id']) == receipt
