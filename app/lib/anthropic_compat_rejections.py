"""Exact source-specific rejection matching for recorded compatibility cases."""
from __future__ import annotations
from typing import Any


def claude_compat_adaptive_rejection_matches(payload: Any, body: Any) -> bool:
    if not isinstance(body, dict) or body.get('thinking') != {'type': 'adaptive'} or 'extra_body' in body:
        return False
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get('param') not in (None, 'thinking', 'thinking.type'):
        return False
    message = error.get('message')
    return isinstance(message, str) and message.strip() == 'Adaptive thinking is not available via the OpenAI compatibility endpoint.'


def claude_compat_sampling_rejection_matches(profile: str, payload: Any, body: Any) -> bool:
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(body, dict) or not isinstance(error, dict):
        return False
    message = error.get('message'); parameter = error.get('param')
    if parameter not in (None, 'temperature', 'top_p') or not isinstance(message, str):
        return False
    if profile == 'claude_top_p':
        return ('top_p' in body and 'temperature' not in body and parameter in (None, 'top_p')
                and message.strip() == '`top_p` is deprecated for this model.')
    if profile == 'claude_sampling' and 'temperature' in body and 'top_p' in body:
        return (message.strip() == '`temperature` and `top_p` cannot both be specified for this model. Please use only one.'
                or parameter in (None, 'top_p') and message.strip() == '`top_p` is deprecated for this model.')
    return False
