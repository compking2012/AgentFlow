"""The model must see the exact text against which citations are validated."""
import json

from test_review_contract_repair import setup_batch
from test_workflow import flow as flow

from agentflow.common import canonical_digest
from agentflow.control.scheduler import Scheduler


async def test_triage_catalog_exposes_exact_validator_text_and_document_quote_identity(flow, monkeypatch):
    await setup_batch(flow)
    def evidence(tx):
        batch = tx.get('review_contract_repair', 'batch')
        context = dict(batch['context'])
        context['accepted_documents'] = [{'artifact_id': 'unit-plan', 'step': 'unit_test_plan',
            'text': 'Use a real HTTP server; verify status 413.'}]
        context['accepted_requirements'] = [
            {'artifact_id': 'unit-plan', 'requirement_id': 'PR-15', 'step': 'unit_test_plan',
             'text': '{"requirement_id":"PR-15","case_id":"UT-21"}'},
            {'artifact_id': 'unit-plan', 'requirement_id': 'PR-15', 'step': 'unit_test_plan',
             'text': '{"requirement_id":"PR-15","case_id":"UT-22"}'},
            {'artifact_id': 'unit-plan', 'requirement_id': 'document:unit_test_plan', 'step': 'unit_test_plan',
             'text': 'Use a real HTTP server; verify status 413.'},
        ]
        return tx.put('review_contract_repair', batch['id'], {**batch, 'context': context,
            'context_digest': canonical_digest(context)}, batch['revision'])
    batch = await flow[1].command('fixture.triage-citations', 'context', {}, evidence)
    scheduler = Scheduler(flow[0], flow[1], None, None, flow[0].settings)
    async def context(*args, **kwargs):
        return {'directory': flow[0].settings.data_dir, 'review_phase_contract': None, 'text': ''}
    monkeypatch.setattr(scheduler.stage_context, 'build', context)
    run = await flow[1].read('run', 'run')
    work = await flow[1].read('work_item', 'triage')
    prompt = await scheduler._prompt({**run, 'goal': 'Word book'}, work, 'commit')
    frozen, _ = json.JSONDecoder().raw_decode(prompt.split('Frozen disposition context:\n', 1)[1])
    expected = {(r['artifact_id'], r['requirement_id']): r for r in batch['context']['accepted_requirements']}
    assert len(frozen['requirement_catalog']) == len(expected)
    for row in frozen['requirement_catalog']:
        original = expected[(row['artifact_id'], row['requirement_id'])]
        assert row['text'] == original['text']
        assert row['step'] == original['step']
    assert 'document:<step>' in prompt
