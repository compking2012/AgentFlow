"""Shared finite coding progress contract for collection and scheduling."""


def output_schema():
    return {'type': 'object', 'properties': {
        'summary': {'type': 'string', 'minLength': 1, 'maxLength': 600},
        'status': {'type': 'string', 'enum': ['continue', 'complete']},
        'next_action': {'type': 'string', 'maxLength': 600}},
        'required': ['summary', 'status', 'next_action'], 'additionalProperties': False}
