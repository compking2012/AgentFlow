"""The Codex collector's tool observation contract, shared with late metering."""


def observed_tool_usage(parsed, reason):
    tool_types = {'command_execution', 'file_change', 'mcp_tool_call', 'web_search', 'collab_tool_call'}
    # Diagnostic error items are not tools. Failed turns do not by themselves
    # make a complete, untruncated event log incomplete for tool accounting.
    known_types = tool_types | {'agent_message', 'reasoning', 'todo_list', 'error'}
    observed = set()
    complete = reason != 'log_limit' and not any(error.startswith((
        'invalid_event_line', 'unrecognized_event', 'events_missing', 'event_log_too_large'))
        for error in parsed.get('errors', []))
    for event in parsed.get('events', []):
        item = event.get('raw', {}).get('item')
        if not isinstance(item, dict):
            continue
        if item.get('type') not in known_types:
            complete = False
        if item.get('type') in tool_types:
            if not isinstance(item.get('id'), str) or not item['id'].strip():
                complete = False
            else:
                observed.add(item['id'])
    return {'observed_tool_calls': len(observed), 'tool_observation_complete': complete}
