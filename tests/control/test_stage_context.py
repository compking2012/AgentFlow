import json
from types import SimpleNamespace

import pytest

from agentflow.common import DomainError
from agentflow.control.scheduler import DEVELOPMENT_PLAN_SCHEMA, Scheduler
from agentflow.control.stage_context import StageContext
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest.fixture
async def context_system(tmp_path):
    store = Store(tmp_path / 'controller')
    await store.start()
    artifacts = LocalArtifactStore(tmp_path / 'controller/artifacts')
    yield store, artifacts, Settings(data_dir=tmp_path / 'controller')
    await store.close()


async def document(store, artifacts, identity, content, **extra):
    raw = await artifacts.put_bytes(json.dumps({'title': identity, 'content': content, **extra}).encode())
    return await store.command('fixture', identity, {}, lambda tx: tx.put('artifact', identity, {
        'digest': raw['id'], 'name': 'openhands_final.json', 'media_type': 'application/json', 'generation': 1}))


def work(identity, step, dependencies=(), **extra):
    return {'id': identity, 'step': step, 'run_id': 'run', 'generation': 1,
            'dependencies': list(dependencies), 'artifact_ids': [identity], **extra}


async def test_accepted_stage_summary_replaces_children_and_scheduler_payload(context_system):
    store, artifacts, settings = context_system
    rows = [work('goal', 'goal'), work('child', 'research', ['goal'], parent_stage_id='research'),
            work('research', 'research', ['child'], kind='aggregation'), work('prd', 'prd', ['research'])]
    for row in rows[:-1]:
        await document(store, artifacts, row['id'], 'CONTENT_' + row['id'],
                       parallel_work=[{'goal': 'DO_NOT_REPEAT_SCHEDULER_INSTRUCTIONS' * 3000}])
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, rows[-1], {}, {row['id']: row for row in rows})
    assert [entry['title'] for entry in result['documents']] == ['goal', 'research']
    assert 'CONTENT_research' in result['text'] and 'CONTENT_child' not in result['text']
    assert 'DO_NOT_REPEAT' not in result['text']
    assert all('parallel_work' not in file.read_text() for file in result['directory'].glob('*.json'))


async def test_aggregate_receives_its_children_and_long_evidence_stays_complete(context_system):
    store, artifacts, settings = context_system
    body = '真实产品研究，完整保留。' * 12000
    await document(store, artifacts, 'facet', body)
    child = work('facet', 'research', parent_stage_id='research')
    aggregate = work('research', 'research', ['facet'], kind='aggregation')
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, aggregate, {}, {'facet': child, 'research': aggregate})
    assert len(result['text']) < 3000
    file = result['directory'] / result['documents'][0]['file']
    assert json.loads(file.read_text())['content'] == body
    assert file.stat().st_mode & 0o222 == 0
    file.chmod(0o600)
    file.write_text('changed')
    with pytest.raises(DomainError, match='modified'):
        await StageContext(store, artifacts, settings.data_dir).build(
            {'id': 'run'}, aggregate, {}, {'facet': child, 'research': aggregate})


async def test_product_prompt_omits_engineering_contract_and_uses_language(context_system):
    store, artifacts, settings = context_system
    item = work('goal', 'goal', status='pending', key='goal', write_paths=[])
    def setup(tx):
        tx.put('plan', 'plan', {'product_contract': {'stack': 'node_web_api', 'language': 'en'},
                              'target_configs': [{'secret_marker': 'EXECUTION_TARGET_DO_NOT_INCLUDE'}]})
        return tx.put('work_item', 'goal', {k: v for k, v in item.items() if k != 'id'})
    await store.command('fixture', 'setup', {}, setup)
    scheduler = Scheduler(SimpleNamespace(artifacts=artifacts, settings=settings), store, None, None, settings)
    prompt = await scheduler._prompt({'id': 'run', 'plan_id': 'plan', 'goal': 'A simple personal task list'}, item, 'a' * 40)
    assert 'EXECUTION_TARGET_DO_NOT_INCLUDE' not in prompt
    assert 'Supported product execution contract' not in prompt
    assert 'Frozen code commit' not in prompt
    assert 'English' in prompt and 'schema applies to the complete artifact assembled locally' in prompt
    assert json.loads(prompt.rsplit('\n', 1)[1]) == DEVELOPMENT_PLAN_SCHEMA
    assert 'concise' in prompt.lower() or 'short' in prompt.lower()


async def test_explicit_reused_inputs_remain_visible_and_deduplicated(context_system):
    store, artifacts, settings = context_system
    await document(store, artifacts, 'prior-research', 'EXPLICIT_ACCEPTED_BASELINE')
    item = work('implementation', 'implementation')
    result = await StageContext(store, artifacts, settings.data_dir).build({'id': 'run'}, item,
        {'reused_inputs': ['prior-research'], 'stage_reused_inputs': {'implementation': ['prior-research']}},
        {item['id']: item})
    assert result['text'].count('EXPLICIT_ACCEPTED_BASELINE') == 1
    assert len(result['documents']) == 1


async def test_global_reference_is_not_removed_by_an_additional_stage_binding(context_system):
    store, artifacts, settings = context_system
    await document(store, artifacts, 'baseline', 'GLOBAL_REFERENCE_REQUIRED')
    item = work('implementation', 'implementation')
    result = await StageContext(store, artifacts, settings.data_dir).build({'id': 'run'}, item,
        {'input_versions': [{'object_id': 'baseline'}], 'reused_inputs': ['baseline'],
         'stage_reused_inputs': {'architecture': ['baseline']}}, {item['id']: item})
    assert 'GLOBAL_REFERENCE_REQUIRED' in result['text']


async def test_document_size_boundary_is_readable_after_normalization(context_system):
    store, artifacts, settings = context_system
    prefix, suffix = b'{"content":"', b'"}'
    raw = prefix + b'x' * (StageContext.MAX_DOCUMENT_BYTES - len(prefix) - len(suffix)) + suffix
    blob = await artifacts.put_bytes(raw)
    await store.command('fixture', 'boundary', {}, lambda tx: tx.put('artifact', 'boundary', {
        'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json', 'generation': 1}))
    parent, child = work('boundary', 'architecture'), work('implementation', 'implementation', ['boundary'])
    result = await StageContext(store, artifacts, settings.data_dir).build({'id': 'run'}, child, {},
        {parent['id']: parent, child['id']: child})
    path = result['directory'] / result['documents'][0]['file']
    assert path.stat().st_size <= StageContext.MAX_DOCUMENT_BYTES
    assert json.loads(path.read_bytes()) == json.loads(raw)


async def test_missing_current_parent_evidence_is_never_silently_dropped(context_system):
    store, artifacts, settings = context_system
    parent, child = work('missing', 'architecture'), work('implementation', 'implementation', ['missing'])
    with pytest.raises(DomainError, match='missing'):
        await StageContext(store, artifacts, settings.data_dir).build({'id': 'run'}, child, {},
            {parent['id']: parent, child['id']: child})


async def test_normalized_codex_final_is_the_machine_input_and_readable_source(context_system):
    from agentflow.control.readable import render_document
    from agentflow.runtime.codex_output import parse_codex_final

    store, artifacts, settings = context_system
    schema = {'type': 'object', 'properties': {'summary': {'type': 'string'},
        'status': {'enum': ['complete']}, 'next_action': {'type': 'string'}},
        'required': ['summary', 'status', 'next_action'], 'additionalProperties': False}
    raw = b'Wrapper text must stay only in original evidence.\n{"summary":"Verified all 20 API cases.","status":"complete","next_action":""}'
    result, normalized = parse_codex_final(raw, schema)
    assert normalized
    for identity, name, content in [('original', 'codex_final.json', raw),
            ('normalized', 'codex_final.normalized.json', json.dumps(result).encode())]:
        blob = await artifacts.put_bytes(content)
        await store.command('fixture.normalized', identity, {}, lambda tx, identity=identity, name=name, blob=blob:
            tx.put('artifact', identity, {'digest': blob['id'], 'name': name, 'media_type': 'application/json', 'generation': 1}))
    parent = work('implementation', 'implementation', artifact_ids=['original', 'normalized'])
    child = work('review', 'code_review', ['implementation'])
    context = await StageContext(store, artifacts, settings.data_dir).build({'id': 'run'}, child, {},
        {parent['id']: parent, child['id']: child})
    assert len(context['documents']) == 1
    document = json.loads((context['directory'] / context['documents'][0]['file']).read_text())
    assert document['summary'] == result['summary'] and 'Wrapper text' not in context['text']
    rendered = render_document(parent, [({'name': 'codex_final.normalized.json'}, result)])
    assert result['summary'] in rendered and 'Wrapper text' not in rendered


async def test_review_phase_comes_from_producers_not_a_legacy_child_goal(context_system):
    store, artifacts, settings = context_system
    rows = [work('impl', 'implementation', artifact_ids=[], write_paths=['src', 'public'], status='completed'),
        work('cr-tests', 'code_review', ['impl'], artifact_ids=[], parent_stage_id='review',
             payload={'goal': 'Require all unit/integration tests to match the accepted plans',
                      'review_phase_contract': {'required_test_phases': ['unit', 'integration']}}),
        work('review', 'code_review', ['cr-tests'], kind='aggregation', artifact_ids=[]),
        work('unit-plan', 'unit_test_plan', ['review'], status='pending', artifact_ids=[]),
        work('unit-code', 'unit_test_implementation', ['unit-plan'], status='pending', artifact_ids=[]),
        work('integration-code', 'integration_test_implementation', ['unit-code'], status='pending', artifact_ids=[])]
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, rows[1], {}, {row['id']: row for row in rows})
    contract = result.get('review_phase_contract', {})
    assert contract.get('producer_stages') == [{'work_item_id': 'impl', 'step': 'implementation',
        'generation': 1, 'write_paths': ['src', 'public']}]
    assert contract.get('required_test_phases') == []
    assert contract.get('deferred_test_phases') == ['unit', 'integration']
    assert contract.get('existing_test_regressions') == 'required'
    assert contract.get('frozen_boundaries') == 'required'
    assert 'review_phase_contract' not in rows[1]  # Derivation must not mutate stored work.


@pytest.mark.parametrize('step,required,deferred', [
    ('unit_test_implementation', ['unit'], ['integration']),
    ('integration_test_implementation', ['unit', 'integration'], []),
])
async def test_generated_tests_keep_their_full_review_obligations(context_system, step, required, deferred):
    store, artifacts, settings = context_system
    rows = [work('impl', 'implementation', artifact_ids=[]),
        work('unit-code', 'unit_test_implementation', ['impl'], artifact_ids=[]),
        work('unit-review', 'code_review', ['unit-code'], artifact_ids=[]),
        work('integration-code', 'integration_test_implementation', ['unit-review'], artifact_ids=[]),
        work('integration-review', 'code_review', ['integration-code'], artifact_ids=[])]
    item = rows[2] if step == 'unit_test_implementation' else rows[4]
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, item, {}, {row['id']: row for row in rows})
    contract = result.get('review_phase_contract', {})
    assert contract.get('required_test_phases') == required
    assert contract.get('deferred_test_phases') == deferred
    assert contract.get('existing_test_regressions') == 'required'


async def test_scheduler_exposes_producer_scopes_for_planning_and_freezes_review_contract(context_system):
    store, artifacts, settings = context_system
    rows = [work('plan', 'development_plan', key='development_plan', status='running', artifact_ids=[]),
        work('impl', 'implementation', ['plan'], key='implementation', status='pending', artifact_ids=[]),
        work('review', 'code_review', ['impl'], key='code_review', status='pending', artifact_ids=[],
            payload={'goal': 'OUTDATED: block until future unit and integration tests exist'}),
        work('unit-code', 'unit_test_implementation', ['review'], key='unit_test_implementation',
            status='pending', artifact_ids=[]),
        work('unit-review', 'code_review', ['unit-code'], key='unit_test_implementation:review',
            status='pending', artifact_ids=[])]
    def setup(tx):
        from agentflow.domain.planning import ROLES
        tx.put('plan', 'plan', {'actual_steps': list(dict.fromkeys(row['step'] for row in rows)),
            'work_specs': [{'key': row['key'], 'step': row['step'], 'role': ROLES[row['step']]} for row in rows]})
        for row in rows:
            tx.put('work_item', row['id'], {**{key: value for key, value in row.items() if key != 'id'},
                                          'role': ROLES[row['step']]})
        return {}
    await store.command('fixture', 'phase-planning', {}, setup)
    scheduler = Scheduler(SimpleNamespace(artifacts=artifacts, settings=settings), store, None, None, settings)
    run = {'id': 'run', 'plan_id': 'plan', 'goal': 'Build a product'}
    planning = await scheduler._prompt(run, rows[0], 'a' * 40)
    metadata = planning.split('Pending stage contracts available for expansion:\n', 1)
    assert len(metadata) == 2
    stages = {row['stage_key']: row for row in json.loads(metadata[1].split('\n', 1)[0])}
    assert stages['code_review']['allowed_review_focuses'] == ['current_code', 'existing_test_regressions']
    assert stages['unit_test_implementation:review']['allowed_review_focuses'] == [
        'current_code', 'existing_test_regressions', 'unit_test_coverage']
    await scheduler._prompt(run, rows[2], 'a' * 40)
    frozen = scheduler._review_phase_contracts[('review', 1)]
    assert frozen['required_test_phases'] == []
    assert frozen['deferred_test_phases'] == ['unit']
    assert frozen['source_commit'] == 'a' * 40


async def test_reused_test_baseline_does_not_advance_the_current_generation(context_system):
    store, artifacts, settings = context_system
    await document(store, artifacts, 'old-tests', 'PRIOR_GENERATION_TEST_BASELINE')
    def mark_old_tests(tx):
        value = tx.get('artifact', 'old-tests')
        return tx.put('artifact', 'old-tests', {**value, 'step': 'unit_test_implementation'}, value['revision'])
    await store.command('fixture', 'old-tests-producer', {}, mark_old_tests)
    rows = [work('impl', 'implementation', artifact_ids=[]),
        work('review', 'code_review', ['impl'], artifact_ids=[]),
        work('new-tests', 'unit_test_implementation', ['review'], status='pending', artifact_ids=[])]
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, rows[1], {'reused_inputs': ['old-tests']}, {row['id']: row for row in rows})
    assert result['review_phase_contract']['required_test_phases'] == []
    assert result['review_phase_contract']['deferred_test_phases'] == ['unit']
    assert result['review_phase_contract']['existing_test_regressions'] == 'required'
    assert result['documents'][0]['artifact_id'] == 'old-tests'
