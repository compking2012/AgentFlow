"""Translate controlled broker errors without dropping observations or accounting."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Literal, get_args

from agentflow.common import DomainError

IOOperation = Literal['read_code', 'list_code', 'read_context', 'write_document', 'propose_work', 'fetch_url',
                      'result_begin', 'result_append', 'result_status', 'result_revise_parallel_work']
_OPERATIONS = frozenset(get_args(IOOperation))
_EXPECTED_CODES = frozenset({
    'forbidden_path', 'forbidden_tool', 'code_write_forbidden', 'document_too_large', 'file_not_supported',
    'invalid_path', 'invalid_proposal', 'context_modified', 'invalid_context_range', 'invalid_source_range', 'network_not_authorized',
    'source_unavailable', 'result_already_finished', 'result_requires_staging', 'invalid_result_begin',
    'invalid_result_chunk', 'invalid_result_field', 'invalid_result_range', 'invalid_result_reference',
    'result_begin_conflict', 'result_budget_missing', 'result_builder_limit', 'result_checkpoint_conflict',
    'result_checkpoint_corrupt', 'result_chunk_conflict', 'result_chunk_limit', 'result_chunk_too_large',
    'result_identity_mismatch', 'result_incomplete', 'result_offset_conflict', 'result_read_too_large',
    'result_requires_partition', 'result_revision_conflict', 'result_stream_closed',
})
STOP_MESSAGES = {
    'role_tool_limit_exceeded': 'Role tool quota exhausted; no further model calls are allowed.',
    'role_cancelled': 'Role execution was cancelled; no further model calls are allowed.',
    'worker_internal_error': 'The controlled tool failed internally; execution stopped without accepting a result.',
}


@dataclass(frozen=True)
class IOResult:
    value: Any
    is_error: bool = False
    stop_code: str | None = None


def _error(code, operation, arguments, *, stop=None):
    message = ('Select an exact file from the frozen input index; directories are not readable inputs.'
        if operation == 'read_context' and code == 'forbidden_path' else
        'Correct the arguments using the advertised tool schema and authorized paths. The original permissions and limits still apply.')
    if operation.startswith('result_'):
        message = 'Use result_status to inspect acknowledged cursors, then correct the staged-result arguments. Limits and schema are unchanged.'
    if code == 'invalid_result_reference':
        message = ('Call result_status with no arguments or result_ref: null to list drafts. '
                   'For an existing draft, pass the exact result_ref object returned by result_begin/result_status; '
                   'strings such as "none" are invalid. Limits and schema are unchanged.')
    value = {'error': {'code': code, 'message': STOP_MESSAGES.get(stop, message)}, 'operation': operation}
    if isinstance(arguments.get('path'), str):
        value['requested_path'] = arguments['path'][:256]
    return IOResult(value, is_error=True, stop_code=stop)


def execute_io(broker, operation, arguments):
    try:
        if operation not in _OPERATIONS:
            broker.consume('forbidden_tool')
            return _error('forbidden_tool', 'forbidden_tool', {})
        # Python rejects missing/extra keywords before entering the broker method.
        # Charge those executed tool attempts once, through its existing meter.
        try:
            signature = inspect.signature(getattr(broker, operation), eval_str=True)
            bound = signature.bind(**arguments)
            if any(signature.parameters[name].annotation in {str, int, bool, dict}
                   and type(value) is not signature.parameters[name].annotation
                   for name, value in bound.arguments.items()):
                raise TypeError('Invalid primitive tool argument type')
        except TypeError:
            broker.consume(operation)
            return _error('invalid_arguments', operation, arguments)
        return IOResult(broker.execute(operation, **arguments))
    except DomainError as error:
        if error.code == 'planning_validation_failed':
            return IOResult({'error': {'code': error.code, 'message': error.message, 'details': error.details}}, is_error=True)
        stop = {'cancelled': 'role_cancelled', 'tool_limit_exceeded': 'role_tool_limit_exceeded'}.get(error.code)
        code = error.code if error.code in _EXPECTED_CODES or stop else 'tool_rejected'
        return _error(code, operation, arguments, stop=stop)
    except (TypeError, ValueError, UnicodeError):
        return _error('invalid_arguments', operation, arguments)
    except OSError:
        return _error('file_unavailable', operation, arguments)
    except Exception:
        # Neither Python exception strings nor internal paths go to the model.
        return _error('tool_internal_error', operation, {}, stop='worker_internal_error')
