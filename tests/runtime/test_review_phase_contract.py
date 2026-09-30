"""The review phase is supplied as system metadata, independently of the child goal."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agentflow.adapters.openhands.worker import _role_instructions
from agentflow.models.profiles import ModelProfile
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.service import RuntimeService


async def test_review_envelope_carries_controller_phase_to_system_instructions(task, tmp_path):
    contract = {'version': 1, 'producer_stages': [{'work_item_id': 'impl', 'step': 'implementation',
        'generation': 1, 'write_paths': ['src', 'public']}], 'required_test_phases': [],
        'deferred_test_phases': ['unit', 'integration'], 'existing_test_regressions': 'required',
        'frozen_boundaries': 'required', 'source_commit': 'a' * 40}
    profile = ModelProfile(model_profile_id='fixture', provider='openai_compatible', requested_model='fixture-model',
        accepted_api_model='fixture-model', acceptance_status='accepted', protocols=['chat_completions'],
        base_url='https://fixture.example.invalid/v1', credential_reference='fixture')
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.data_dir = tmp_path / 'runtime'
    runtime.models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock(return_value=profile)),
                                    secret_resolver=lambda _: 'fixture-only')
    runtime.codex, runtime.openhands = object(), object()
    payload = {'attempt_id': task.attempt_id, 'run_id': task.run_id, 'iteration_id': task.iteration_id,
        'role': 'review', 'step': 'code_review', 'goal': 'OUTDATED CHILD GOAL: all future tests must exist now',
        'input_fingerprint': task.input_fingerprint, 'workspace': str(task.workspace),
        'profile_id': 'fixture', 'task_token': 'fixture-token', 'cost_mode': 'request_limited',
        'review_phase_contract': contract}
    envelope, backend = await runtime._envelope(payload)
    encoded = envelope.model_dump(mode='json')
    assert encoded.get('review_phase_contract') == contract
    restored = TaskEnvelope.model_validate({**encoded, 'proxy_token': 'fixture-restored'})
    instructions = _role_instructions(restored)
    assert json.dumps(contract, sort_keys=True) in instructions
    assert payload['goal'] not in instructions
    assert 'Existing test deletion' in instructions and 'Do not discard such findings by path' in instructions
    assert backend is runtime.openhands
