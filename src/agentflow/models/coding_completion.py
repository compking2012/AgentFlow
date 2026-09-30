"""Steer stalled Codex steps to a progress receipt; never decide their quality."""
from __future__ import annotations

import json
import re
import shlex

_EXIT = re.compile(r'(?:^|\n)Process exited with code 0\n')
_COMPLETION_MARKER = (('completion_marker',),)
_RECEIPT_HINT = (
    'AgentFlow execution feedback: consecutive successful tool calls repeated unchanged read-only checks '
    'and results, printed completion markers, or did nothing. '
    'Return the required short coding progress JSON in the final answer now; tools are unavailable for this '
    'response. Use status=complete only if the assigned implementation is ready. If anything remains, use '
    'status=continue and a specific next_action so the controller can preserve the checkpoint and start '
    'the next coding step. Do not fabricate completed work, test results or evidence. All original scope, '
    'assertions, independent review, formal testing, permissions and budgets still apply.'
)


def _check(arguments):
    try:
        value = json.loads(arguments)
        if not isinstance(value, dict) or not isinstance(value.get('cmd'), str):
            return None
        lexer = shlex.shlex(value['cmd'], posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        parts = list(lexer)
    except (ValueError, TypeError, KeyError):
        return None
    # Recognize a deliberately small shell grammar. Other tools/commands break
    # the observation window, rather than being assumed harmless or completed.
    if parts == ['true'] or (len(parts) == 2 and parts[0] == 'echo'
                            and parts[1].casefold() in {'done', 'ok', 'ready', 'finished', 'complete', 'completed'}):
        return _COMPLETION_MARKER, None
    groups, current = [], []
    for part in parts:
        if part == '&&':
            groups.append(current)
            current = []
        else:
            current.append(part)
    groups.append(current)
    normalized, marker = [], None
    for index, group in enumerate(groups):
        if (index == 0 and len(groups) > 1 and len(group) == 2 and group[0] == 'cd'
                and group[1].startswith('/')
                and not any(character in group[1] for character in ';|&`$<>\n')):
            normalized.append(tuple(group))
            continue
        if group[:3] == ['git', '-C', '.']:
            group = ['git', *group[3:]]
        if (group in (['git', 'status', '--short'], ['git', 'status', '--porcelain'],
                      ['git', 'diff', '--stat'], ['git', 'diff', '--check'])
                or len(group) == 3 and group[:2] == ['node', '--check']
                and not any(character in group[2] for character in ';|&`$<>\n')):
            normalized.append(tuple(group))
        elif (index == len(groups) - 1 and normalized and len(group) == 2 and group[0] == 'echo'
                and re.fullmatch(r'[A-Z_]+', group[1])):
            marker = group[1]
        else:
            return None
    return (tuple(normalized), marker) if any(group[0] != 'cd' for group in normalized) else None


def add_completion_hint(body, *, max_request_bytes):
    """Repeated unchanged checks or no-ops request one receipt-only response.

    Three identical observation cycles or consecutive completion markers are
    progress signals, not retry limits. No invocation is denied, no process is
    stopped and no work is accepted here. Receipt/schema, checkpoint and quality
    gates remain authoritative. The request change is visible in model Trace.
    """
    text = body.get('text')
    form = text.get('format') if isinstance(text, dict) else None
    schema = form.get('schema') if isinstance(form, dict) else None
    properties = schema.get('properties') if isinstance(schema, dict) else None
    if (not isinstance(properties, dict) or form.get('type') != 'json_schema'
            or form.get('name') != 'codex_output_schema' or set(properties) != {'summary', 'status', 'next_action'}
            or not isinstance(properties.get('status'), dict)
            or properties['status'].get('enum') != ['continue', 'complete']
            or not isinstance(body.get('input'), list)):
        return
    history = body['input']
    if history and history[-1] == {'role': 'developer', 'content': _RECEIPT_HINT}:
        return
    observations, pending = [], {}
    marker_streak = 0
    for item in history:
        if not isinstance(item, dict):
            observations, pending = [], {}
            marker_streak = 0
            continue
        kind = item.get('type')
        if kind == 'function_call':
            command = _check(item.get('arguments')) if item.get('name') == 'exec_command' else None
            call_id = item.get('call_id')
            if not command or not isinstance(call_id, str) or call_id in pending:
                observations, pending = [], {}
                marker_streak = 0
            else:
                pending[call_id] = command
        elif kind == 'function_call_output':
            call_id = item.get('call_id')
            command = pending.pop(call_id, None) if isinstance(call_id, str) else None
            output = item.get('output')
            if not command or not isinstance(output, str) or not _EXIT.search(output) or '\nOutput:\n' not in output:
                observations, pending = [], {}
                marker_streak = 0
            else:
                result = output.split('\nOutput:\n', 1)[1]
                signature, marker = command
                if signature == _COMPLETION_MARKER:
                    marker_streak += 1
                    continue
                marker_streak = 0
                if marker and result.endswith(marker + '\n'):
                    result = result[:-len(marker) - 1]
                observations.append((signature, result))
                observations = observations[-12:]
        elif kind in {'custom_tool_call', 'custom_tool_call_output', 'local_shell_call'} or item.get('role') == 'user':
            observations, pending = [], {}
            marker_streak = 0
    if pending:
        return
    # Compare values, including results; command repetition alone is insufficient.
    repeated = any(len(observations) >= width * 3
        and observations[-width:] == observations[-2 * width:-width] == observations[-3 * width:-2 * width]
        for width in range(1, 5))
    receipt_only = repeated or marker_streak >= 3
    if not receipt_only:
        return
    history.append({'role': 'developer', 'content': _RECEIPT_HINT})
    had_choice, previous_choice = 'tool_choice' in body, body.get('tool_choice')
    body['tool_choice'] = 'none'
    if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > max_request_bytes:
        history.pop()
        if had_choice:
            body['tool_choice'] = previous_choice
        else:
            body.pop('tool_choice', None)
