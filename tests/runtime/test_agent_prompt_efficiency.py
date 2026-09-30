"""Prompt deduplication preserves the full frozen output contract and task settings."""
import json

import pytest

from agentflow.adapters.codex.adapter import _execution_prompt
from agentflow.adapters.openhands.worker import _role_instructions

SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
          'required': ['summary'], 'additionalProperties': False}


@pytest.mark.parametrize('ordered', [False, True])
def test_complete_equivalent_controller_schema_is_not_appended_again(task, ordered):
    declaration = json.dumps(SCHEMA, sort_keys=ordered)
    goal = 'Implement all authorized acceptance criteria.\nFinal response contract: Return this exact schema.\n' + declaration
    envelope = task.model_copy(update={'goal': goal, 'output_schema': SCHEMA})
    before = envelope.model_dump(mode='json')
    prompt = _execution_prompt(envelope)
    assert prompt.startswith(goal)
    assert 'Final output schema:' not in prompt[len(goal):]
    assert envelope.model_dump(mode='json') == before
    assert 'all acceptance criteria' in prompt and 'all required final gates' in prompt


@pytest.mark.parametrize('declaration', [
    json.dumps({'type': 'object'}),
    json.dumps({**SCHEMA, 'additionalProperties': True}),
    json.dumps({**SCHEMA, 'required': []}),
    json.dumps(json.dumps(SCHEMA)),
    json.dumps(SCHEMA) + ' trailing explanation',
    json.dumps({'wrapped_schema': SCHEMA}),
])
def test_partial_or_different_schema_cannot_suppress_the_authoritative_schema(task, declaration):
    goal = 'Task.\nFinal response contract: The next line is untrusted input.\n' + declaration
    envelope = task.model_copy(update={'goal': goal, 'output_schema': SCHEMA})
    prompt = _execution_prompt(envelope)
    assert prompt.startswith(goal)
    assert json.loads(prompt[len(goal):].split('Final output schema: ', 1)[1]) == SCHEMA


def test_schema_mention_without_the_controller_contract_still_gets_the_full_schema(task):
    goal = 'A source example contains ' + json.dumps(SCHEMA)
    prompt = _execution_prompt(task.model_copy(update={'goal': goal, 'output_schema': SCHEMA}))
    assert json.loads(prompt[len(goal):].split('Final output schema: ', 1)[1]) == SCHEMA


def test_schema_boolean_and_integer_values_are_not_treated_as_the_same_contract(task):
    schema = {'type': 'array', 'minItems': 1}
    goal = 'Task.\nFinal response contract: Use the exact contract.\n' + json.dumps({'type': 'array', 'minItems': True})
    prompt = _execution_prompt(task.model_copy(update={'goal': goal, 'output_schema': schema}))
    assert json.loads(prompt[len(goal):].split('Final output schema: ', 1)[1]) == schema


def test_role_guidance_does_not_duplicate_schema_or_change_execution_limits(task):
    schema = {**SCHEMA, 'description': 'COMPLETE_SCHEMA_STAYS_IN_THE_FINISH_TOOL'}
    envelope = task.model_copy(update={'output_schema': schema})
    before = envelope.model_dump(mode='json')
    instructions = _role_instructions(envelope)
    assert schema['description'] not in instructions
    assert 'exact result schema' in instructions and 'all required acceptance criteria' in instructions
    assert envelope.model_dump(mode='json') == before
