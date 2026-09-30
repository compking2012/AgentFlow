"""Every original action is rebound before choosing its mandatory guard receipt."""
import pytest
from test_review_contract_lifecycle import finish_repairs_and_assembly, lifecycle_source
from test_workflow import flow as flow

from agentflow.common import DomainError


@pytest.mark.parametrize('tamper', ['kind', 'paths', 'owner', 'actions'])
async def test_validation_rejects_changed_action_authority_before_guard_selection(flow, monkeypatch, tamper):
    env = await lifecycle_source(flow, coverage=True)
    claim, _ = await finish_repairs_and_assembly(env)
    check = (await env.store.list('test_coverage_check'))[0]

    def mutate(tx):
        action = tx.get('work_item', check['work_item_id'])
        payload = dict(action['payload'])
        paths = action['write_paths']
        if tamper == 'kind':
            payload['review_contract_kind'] = 'production_fix'
        elif tamper == 'paths':
            paths = []
        elif tamper == 'owner':
            payload['review_contract_owner'] = 'web'
        else:
            payload['review_contract_actions'] = []
        return tx.put('work_item', action['id'], {**action, 'payload': payload,
                                                'write_paths': paths}, action['revision'])

    await env.store.command('fixture.coverage-action-authority', tamper, {}, mutate)
    if tamper == 'kind':
        read = env.store.read

        async def no_receipt(kind, identity):
            if kind == 'test_coverage_check' and identity == check['id']:
                return None
            return await read(kind, identity)

        monkeypatch.setattr(env.store, 'read', no_receipt)
    with pytest.raises(DomainError) as caught:
        await env.core.validate_or_poll(claim)
    assert caught.value.code == 'review_contract_binding_invalid'
    assert not await env.store.list('review_diagnostic')
