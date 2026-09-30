"""Actual broker failures retain accounting, authority and safe error text."""
import json

import pytest

from agentflow.adapters.openhands.tools import ToolBroker


@pytest.mark.parametrize('operation,arguments', [
    ('read_code', {}), ('read_code', {'path': 'original.py', 'extra': True}),
    ('read_code', {'path': None}), ('read_code', {'path': '../secret'}),
    ('read_code', {'path': 'missing.txt'}), ('read_context', {'path': '.'}),
    ('write_document', {'path': 'bad.py', 'content': 'PRIVATE_CODE'}),
    ('write_document', {'path': 'bad.md', 'content': None}),
    ('write_document', {'path': 'bad.md', 'content': {'private': 'PRIVATE_CODE'}}),
])
def test_expected_io_error_is_observed_and_charged_once(task, operation, arguments):
    from agentflow.adapters.openhands.io_execution import execute_io
    broker = ToolBroker(task)
    result = execute_io(broker, operation, arguments)
    assert result.is_error and result.stop_code is None
    assert broker.calls == 1 and len(broker.events) == 1
    assert not broker.finished_result
    assert not (task.artifact_dir / 'bad.py').exists()
    assert 'PRIVATE_CODE' not in json.dumps(result.value)
    success = execute_io(broker, 'read_code', {'path': 'original.py'})
    assert not success.is_error and success.value['text'] == 'ORIGINAL = True\n'
    assert broker.calls == 2


@pytest.mark.parametrize('cancelled', [False, True])
def test_io_limit_and_cancellation_are_terminal_without_changing_quota(task, cancelled):
    from agentflow.adapters.openhands.io_execution import execute_io
    broker = ToolBroker(task.model_copy(update={'max_tool_calls': 1}))
    if cancelled:
        broker.stopped.set()
    else:
        broker.consume('read_code')
    result = execute_io(broker, 'read_code', {'path': 'original.py'})
    assert result.is_error and result.stop_code == ('role_cancelled' if cancelled else 'role_tool_limit_exceeded')
    assert broker.calls == (0 if cancelled else 1)


def test_unexpected_exception_is_terminal_without_private_exception_text(task, monkeypatch):
    from agentflow.adapters.openhands.io_execution import execute_io
    broker = ToolBroker(task)
    def fail(path):
        broker.consume('read_code')
        raise RuntimeError('PRIVATE_CREDENTIAL_OR_PATH')
    monkeypatch.setattr(broker, 'read_code', fail)
    result = execute_io(broker, 'read_code', {'path': 'original.py'})
    assert result.is_error and result.stop_code == 'worker_internal_error'
    assert 'PRIVATE_CREDENTIAL_OR_PATH' not in json.dumps(result.value)
    assert broker.calls == 1


@pytest.mark.parametrize('reference', ['none', 'null', [], False, 17, {},
    {'id': 'a' * 64, 'revision': -1, 'digest': 'sha256:' + 'b' * 64}])
@pytest.mark.parametrize('operation', ['result_status', 'result_append'])
def test_invalid_result_reference_is_correctable_without_aborting_the_role(task, reference, operation):
    from agentflow.adapters.openhands.io_execution import execute_io
    broker = ToolBroker(task)
    arguments = {'result_ref': reference}
    if operation == 'result_append':
        arguments.update(field='review', chunk_id='bad-ref', expected_offset=0, value='text')
    result = execute_io(broker, operation, arguments)
    assert result.is_error and result.stop_code is None
    assert result.value['error']['code'] == 'invalid_result_reference'
    assert 'null' in result.value['error']['message']
    assert broker.calls == 1 and not broker.finished_result
    corrected = execute_io(broker, 'result_status', {})
    assert not corrected.is_error and corrected.value['drafts'] == []
    assert broker.calls == 2
    broker.finish({'review': 'Recovered after invalid result reference'})
    assert broker.calls == 3 and broker.finished_result
