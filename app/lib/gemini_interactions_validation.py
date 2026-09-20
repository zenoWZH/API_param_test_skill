"""Validate Interactions envelopes with a source-bound stateless ID exception.

The 2026-09-09 official OpenAPI Interaction.required list contains status only.
Observed store=false, synchronous text and function-call responses omit id.
Stored state chains retain their resource-ID requirement; a separate exact
stateless stream scope verifies output/usage without asserting resource identity.
Function call IDs are independently required. Tool schemas use a bounded local subset,
not a claim of full JSON Schema support or enforcement by the server.
https://ai.google.dev/static/api/interactions.openapi.json
See references/ai_studio_stateless_cache_facts_20260909.json for retained evidence.
"""
from __future__ import annotations
import math
import re
from urllib.parse import urlsplit
from .gemini_schema_validation import _normalize, _matches

MODEL = 'gemini-3.7-flash'
CONTRACT = 'gemini_3_7_flash_interactions'
REJECTION_PROFILES = frozenset({CONTRACT + '_reject_thinking_minimal', CONTRACT + '_labels'})


def _official_stateless_scope(body, transport, source, context):
    if (transport != 'gemini_interactions' or source != CONTRACT
            or not isinstance(body, dict) or body.get('model') != MODEL
            or body.get('store') is not False or not isinstance(body.get('input'), str) or not body['input'].strip()
            or body.get('stream', False) is not False or body.get('background', False) is not False
            or any(key in body for key in ('previous_interaction_id', 'agent', 'environment', 'environment_id'))
            or not isinstance(context, dict) or context.get('requested_model') != MODEL):
        return False
    try:
        url = urlsplit(context.get('request_url', ''))
        return (url.scheme == 'https' and url.hostname == 'generativelanguage.googleapis.com'
                and url.port in (None, 443) and url.path == '/v1beta/interactions'
                and url.username is None and url.password is None and not url.query and not url.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def validate_interactions_rejection(profile, response, status, *, body, transport, reference_source, request_context):
    """Recognize only the two exact official field rejections established live."""
    if (profile not in REJECTION_PROFILES
            or not _official_stateless_scope(body, transport, reference_source, request_context)):
        return 'interaction_rejection_scope_unverified'
    error = response.get('error') if isinstance(response, dict) else None
    if (type(status) is not int or status != 400 or not isinstance(error, dict)
            or error.get('code') != 'invalid_request' or not isinstance(error.get('message'), str)):
        return 'interaction_rejection_unattributed'
    message = error['message']
    if profile.endswith('_reject_thinking_minimal'):
        generation = body.get('generation_config')
        if not isinstance(generation, dict) or generation.get('thinking_level') != 'minimal':
            return 'interaction_rejection_request_mismatch'
        named = re.search(r"\bminimal\b.{0,80}\bnot\s+(?:a\s+)?supported\s+thinking\s+level\b", message, re.I)
        allowed = re.search(r'\ballowed\s+values\s+are\s*:', message, re.I)
        matches = named and allowed and all(re.search(r'\b' + level + r'\b', message, re.I) for level in ('high', 'low', 'medium'))
    else:
        labels = body.get('labels')
        if not isinstance(labels, dict) or not labels or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
            return 'interaction_rejection_request_mismatch'
        matches = re.search(r"\bparameter\s+['\"]?labels['\"]?\s+is\s+not\s+available\s+on\s+the\s+Gemini\s+API\b", message, re.I)
        matches = matches and re.search(r'\bavailable\s+on\s+the\s+Gemini\s+Enterprise\s+Agent\s+Platform\b', message, re.I)
    return None if matches else 'interaction_rejection_unattributed'


def validate_interactions_json(profile, response, *, body, transport, reference_source, request_context):
    """Check this matrix's declared JSON Schema subset, not arbitrary dialects."""
    if profile != CONTRACT + '_response_format_json':
        return False, None
    if (not _official_stateless_scope(body, transport, reference_source, request_context)
            or not isinstance(response, dict) or response.get('model') != MODEL):
        return True, 'interaction_schema_scope_unverified'
    format_ = body.get('response_format')
    if (not isinstance(format_, dict) or format_.get('type') != 'text'
            or format_.get('mime_type') != 'application/json' or 'schema' not in format_):
        return True, 'interaction_schema_request_invalid'
    try:
        schema = format_['schema']
        _bounded_json(schema, schema=True)
        normalized = _normalize(schema, native=False)
        _schema_metadata(schema)
    except (ValueError, TypeError, RecursionError):
        return True, 'interaction_schema_unverified'
    from .gemini_interactions_stream import strict_json
    try:
        value = strict_json(_text(response))
        _bounded_json(value)
    except (ValueError, TypeError, RecursionError):
        return True, 'json_parse'
    try:
        matches = _matches(value, normalized)
    except (ValueError, TypeError, RecursionError):
        return True, 'interaction_schema_unverified'
    return True, None if matches else 'json_schema_mismatch'


def _stateless_stream_id_optional(profile, response, body, transport, source, context):
    from .gemini_interactions_stream import stateless_text_stream_scope
    if (profile != CONTRACT + '_stream' or transport != 'gemini_interactions' or source != CONTRACT
            or not isinstance(context, dict) or context.get('requested_model') != MODEL
            or response.get('model') != MODEL or response.get('object') != 'interaction'
            or response.get('id') != '' or not stateless_text_stream_scope(body, context.get('request_url'))):
        return False
    observed = response.get('_stream_validation')
    return (isinstance(observed, dict) and observed.get('errors') == []
            and observed.get('stateless_source_scope_verified') is True
            and observed.get('all_lifecycle_ids_exactly_empty') is True
            and observed.get('stream_output_usage_verified') is True
            and observed.get('resource_identity_verified') is False
            and all(type(observed.get(k)) is int and observed[k] == 1 for k in ('created_count', 'terminal_count', 'done_count')))


def _stateless_id_optional(profile, response, body, transport, reference_source, context, tool_profiles):
    if (transport != 'gemini_interactions' or reference_source != CONTRACT
            or profile.endswith('_stream')
            or not isinstance(body, dict) or body.get('model') != MODEL
            or body.get('store') is not False or not isinstance(body.get('input'), str) or not body['input'].strip()
            or body.get('stream') is not None and body.get('stream') is not False
            or body.get('background') is not None and body.get('background') is not False
            or any(key in body for key in ('previous_interaction_id', 'agent'))
            or body.get('tools') and profile not in tool_profiles
            or response.get('model') != MODEL or response.get('object') != 'interaction'
            or not isinstance(context, dict) or context.get('requested_model') != MODEL):
        return False
    response_format = body.get('response_format')
    if response_format is not None and (
            not isinstance(response_format, dict) or response_format.get('type') != 'text'
            or response_format.get('mime_type') not in ('text/plain', 'application/json')):
        return False
    if body.get('response_modalities') or any(key in body for key in ('environment', 'environment_id')):
        return False
    try:
        url = urlsplit(context.get('request_url', ''))
        return (url.scheme == 'https' and url.hostname == 'generativelanguage.googleapis.com'
                and url.port in (None, 443) and url.username is None and url.password is None
                and not url.query and not url.fragment and url.path == '/v1beta/interactions')
    except (TypeError, ValueError, AttributeError):
        return False


def _text(response):
    return ''.join(part['text'] for step in response.get('steps', [])
                   if isinstance(step, dict) and step.get('type') == 'model_output'
                   for part in (step.get('content') if isinstance(step.get('content'), list) else [])
                   if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str))


def _text_summary(value, depth=0):
    if depth > 16:
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_text_summary(part, depth + 1) for part in value)
    if not isinstance(value, dict) or set(value) - {'type', 'text', 'content'}:
        return False
    if 'content' in value:
        return _text_summary(value['content'], depth + 1) and value.get('type') in (None, 'text')
    return value.get('type') == 'text' and isinstance(value.get('text'), str)


def _stateless_text_shape(response, *, allow_calls=False):
    if (response.get('error') or response.get('errors', []) != []
            or response.get('previous_interaction_id') or response.get('agent')):
        return False
    steps = response.get('steps')
    if not isinstance(steps, list) or not steps:
        return False
    for step in steps:
        kinds = {'model_output', 'thought', 'function_call'} if allow_calls else {'model_output', 'thought'}
        if not isinstance(step, dict) or not isinstance(step.get('type'), str) or step['type'] not in kinds:
            return False
        if step['type'] == 'model_output':
            if set(step) - {'type', 'content'}:
                return False
            content = step.get('content')
            if not isinstance(content, list) or not content or not all(
                    isinstance(part, dict) and set(part) <= {'type', 'text'}
                    and part.get('type') == 'text' and isinstance(part.get('text'), str)
                    for part in content):
                return False
        elif step['type'] == 'thought':
            if (set(step) - {'type', 'signature', 'summary', 'content'}
                    or 'signature' in step and not isinstance(step['signature'], str)
                    or any(key in step and not _text_summary(step[key]) for key in ('summary', 'content'))):
                return False
        elif set(step) - {'type', 'id', 'name', 'arguments'}:
            return False
    return True


def _bounded_json(value, *, schema=False, depth=0, budget=None):
    """Fail closed on non-JSON data, excessive nesting and unsupported schemas.

    The reused GenerateContent matcher treats oneOf as anyOf. Interactions does
    not inherit that interpretation: oneOf is unverified here, as are all
    reference/dialect keywords. No resolver or network client is involved.
    """
    if budget is None:
        budget = [4096]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        raise ValueError('JSON bounds')
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('JSON object keys')
        if schema and any(key.startswith('$') or key == 'oneOf' for key in value):
            raise ValueError('schema keyword unverified')
        for item in value.values():
            _bounded_json(item, schema=schema, depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for item in value:
            _bounded_json(item, schema=schema, depth=depth + 1, budget=budget)
    elif not (value is None or type(value) in (str, bool, int)
              or type(value) is float and math.isfinite(value)):
        raise ValueError('non-JSON value')


def _schema_metadata(schema):
    """Complete shape checks absent from the narrower GenerateContent helper.

    Visit only schema nodes, so ordinary property names remain ordinary names.
    The caller has already bounded the tree and normalized structural fields.
    """
    for key in ('title', 'description'):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError('schema annotation type')
    types = schema.get('type')
    if 'type' in schema and not isinstance(types, (str, list)):
        raise ValueError('schema type')
    if isinstance(types, list) and len(types) != len(set(types)):
        raise ValueError('duplicate schema type')
    values = schema.get('enum', [])
    if any(value in values[:index] for index, value in enumerate(values)):
        raise ValueError('duplicate enum value')
    for child in schema.get('properties', {}).values():
        _schema_metadata(child)
    for key in ('items', 'additionalProperties'):
        if isinstance(schema.get(key), dict):
            _schema_metadata(schema[key])
    for child in schema.get('anyOf', []):
        _schema_metadata(child)


def validate_interactions_tools(response, body):
    """Validate the actual mode, declared functions and supported schema subset.

    An unimplemented schema returns unverified even if this response happens to
    look correct. A successful check establishes instance adherence only.
    """
    tools = body.get('tools') if isinstance(body, dict) else None
    if not isinstance(tools, list) or not tools:
        return 'interaction_tool_declarations_invalid'
    declared = {}
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) - {'type', 'name', 'description', 'parameters'}
                or tool.get('type') != 'function' or not isinstance(tool.get('name'), str)
                or not tool['name'].strip() or tool['name'] in declared
                or 'description' in tool and not isinstance(tool['description'], str)):
            return 'interaction_tool_declarations_invalid'
        try:
            schema = tool.get('parameters')
            _bounded_json(schema, schema=True)
            declared[tool['name']] = _normalize(schema, native=False)
            _schema_metadata(schema)
        except (ValueError, TypeError, RecursionError):
            return 'interaction_tool_schema_unverified'
    generation = body.get('generation_config', {})
    if not isinstance(generation, dict):
        return 'interaction_tool_choice_invalid'
    mode = generation.get('tool_choice', 'auto')
    permitted = set(declared)
    if isinstance(mode, dict):
        allowed = mode.get('allowed_tools')
        if (set(mode) != {'allowed_tools'} or not isinstance(allowed, dict)
                or set(allowed) != {'mode', 'tools'} or not isinstance(allowed['tools'], list)
                or not allowed['tools'] or any(not isinstance(name, str) for name in allowed['tools'])):
            return 'interaction_tool_choice_invalid'
        permitted = set(allowed['tools'])
        if len(permitted) != len(allowed['tools']) or not permitted <= set(declared):
            return 'interaction_tool_choice_invalid'
        mode = allowed['mode']
    if not isinstance(mode, str) or mode not in {'auto', 'any', 'none', 'validated'}:
        return 'interaction_tool_choice_invalid'
    steps = response.get('steps')
    if not isinstance(steps, list):
        return 'interaction_steps_missing'
    calls = [step for step in steps if isinstance(step, dict) and step.get('type') == 'function_call']
    if mode == 'none' and calls:
        return 'tool_calls_unexpected'
    if mode == 'any' and not calls:
        return 'native_function_call_missing'
    call_ids = set()
    for call in calls:
        ident, name, arguments = call.get('id'), call.get('name'), call.get('arguments')
        if not isinstance(ident, str) or not ident.strip():
            return 'tool_call_id_missing'
        if ident in call_ids:
            return 'tool_call_id_duplicate'
        call_ids.add(ident)
        if not isinstance(name, str) or not name.strip():
            return 'tool_call_name_missing'
        if name not in permitted:
            return 'tool_call_unknown_function'
        if not isinstance(arguments, dict):
            return 'tool_call_arguments_invalid'
        try:
            _bounded_json(arguments)
            if not _matches(arguments, declared[name]):
                return 'tool_call_arguments_schema_mismatch'
        except (ValueError, TypeError, RecursionError):
            return 'tool_call_arguments_invalid'
    if response.get('status') != ('requires_action' if calls else 'completed'):
        return 'interaction_status_mismatch'
    if not calls and not _text(response).strip():
        return 'interaction_text_missing'
    return None


def validate_interactions_envelope(profile, response_json, result, *, request_body=None,
                                   transport='gemini_interactions', reference_source=None,
                                   request_context=None, tool_profiles=(), tool_none_profiles=()):
    all_tools = set(tool_profiles) | set(tool_none_profiles)
    optional_id = _stateless_id_optional(profile, response_json, request_body, transport,
                                        reference_source, request_context, all_tools)
    optional_id = optional_id or _stateless_stream_id_optional(profile, response_json, request_body,
                                        transport, reference_source, request_context)
    interaction_id = response_json.get('id')
    if optional_id:
        if 'id' in response_json and not isinstance(interaction_id, str):
            return 'interaction_id_invalid'
        if not _stateless_text_shape(response_json, allow_calls=profile in all_tools):
            return 'interaction_stateless_response_invalid'
    elif not isinstance(interaction_id, str) or not interaction_id.strip():
        return 'interaction_id_missing'
    model = response_json.get('model')
    if not isinstance(model, str) or not model.strip():
        return 'interaction_model_missing'
    if profile in all_tools:
        error = validate_interactions_tools(response_json, request_body)
        if error:
            return error
    elif response_json.get('status') != 'completed':
        return 'interaction_status_mismatch'
    steps = response_json.get('steps')
    if not isinstance(steps, list) or not steps:
        return 'interaction_steps_missing'
    if not all(isinstance(step, dict) and step.get('type') for step in steps):
        return 'interaction_step_malformed'
    usage = response_json.get('usage')
    if not isinstance(usage, dict) or not usage:
        return 'interaction_usage_missing'
    total = usage.get('total_tokens')
    if type(total) is not int or total < 0:
        return 'interaction_usage_invalid'
    if not isinstance(result.usage, dict) or result.usage != usage:
        return 'interaction_usage_mismatch'
    if profile not in all_tools and not _text(response_json).strip():
        return 'interaction_text_missing'
    return None
