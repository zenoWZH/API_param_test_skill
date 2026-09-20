"""Production validation of stateless Interactions; no HTTP or model execution."""
import copy
from types import SimpleNamespace

import pytest
import requests

from lib.profile_validation import validate_profile_response

MODEL = 'gemini-3.7-flash'
CONTRACT = 'gemini_3_7_flash_interactions'
URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Runtime validation must never issue HTTP')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)


def args(mode='auto', *, calls=True):
    body = {'model': MODEL, 'input': 'Call get_weather for Beijing.', 'store': False,
            'generation_config': {'tool_choice': mode, 'max_output_tokens': 2048},
            'tools': [{'type': 'function', 'name': 'get_weather', 'parameters': {
                'type': 'object', 'properties': {'city': {'type': 'string'}},
                'required': ['city'], 'additionalProperties': False}}]}
    output = {'type': 'function_call', 'id': 'call_1', 'name': 'get_weather', 'arguments': {'city': 'Beijing'}}
    if not calls:
        output = {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Beijing'}]}
    response = {'object': 'interaction', 'model': MODEL, 'status': 'requires_action' if calls else 'completed',
                'steps': [{'type': 'thought', 'signature': 'synthetic'}, output],
                'usage': {'total_tokens': 10, 'total_input_tokens': 5, 'total_output_tokens': 3, 'total_thought_tokens': 2}}
    return {'profile': CONTRACT + '_tools_' + mode, 'response_json': response,
            'result': SimpleNamespace(success=True, usage=response['usage']), 'request_body': body,
            'transport': 'gemini_interactions', 'reference_source': CONTRACT,
            'request_context': {'requested_model': MODEL, 'request_url': URL}}


@pytest.mark.parametrize('mode,calls', [('auto', True), ('auto', False), ('any', True), ('none', False), ('validated', True), ('validated', False)])
@pytest.mark.parametrize('resource_id', [None, '', 'stored_response'])
def test_modes_validate_actual_call_or_text_branch_without_mutation(mode, calls, resource_id):
    a = args(mode, calls=calls)
    if resource_id is not None:
        a['response_json']['id'] = resource_id
    before = copy.deepcopy(a)
    assert validate_profile_response(**a) is None
    assert a == before


@pytest.mark.parametrize('mode,calls,error', [('any', False, 'native_function_call_missing'), ('none', True, 'tool_calls_unexpected')])
def test_forced_and_forbidden_modes(mode, calls, error):
    assert validate_profile_response(**args(mode, calls=calls)) == error


@pytest.mark.parametrize('calls,status', [(False, 'requires_action'), (True, 'completed'), (True, 'incomplete'), (False, ['completed'])])
def test_status_must_match_actual_branch(calls, status):
    a = args(calls=calls); a['response_json']['status'] = status
    assert validate_profile_response(**a) == 'interaction_status_mismatch'


@pytest.mark.parametrize('field,value,error', [
    ('id', '', 'tool_call_id_missing'), ('id', None, 'tool_call_id_missing'),
    ('name', '', 'tool_call_name_missing'), ('name', 'other', 'tool_call_unknown_function'),
    ('arguments', '{}', 'tool_call_arguments_invalid'), ('arguments', [], 'tool_call_arguments_invalid'),
    ('arguments', {}, 'tool_call_arguments_schema_mismatch'),
    ('arguments', {'city': 1}, 'tool_call_arguments_schema_mismatch'),
    ('arguments', {'city': True}, 'tool_call_arguments_schema_mismatch'),
    ('arguments', {'city': 'Beijing', 'extra': 1}, 'tool_call_arguments_schema_mismatch'),
    ('arguments', {'city': 'Beijing', 'extra': float('nan')}, 'tool_call_arguments_invalid'),
])
def test_call_identity_name_and_exact_declared_arguments(field, value, error):
    a = args(); a['response_json']['steps'][-1][field] = value
    assert validate_profile_response(**a) == error


def test_undeclared_additional_properties_policy_is_not_invented():
    a = args(); del a['request_body']['tools'][0]['parameters']['additionalProperties']
    a['response_json']['steps'][-1]['arguments']['extra'] = 1
    assert validate_profile_response(**a) is None


def test_duplicate_call_ids_are_rejected():
    a = args(); a['response_json']['steps'].append(copy.deepcopy(a['response_json']['steps'][-1]))
    assert validate_profile_response(**a) == 'tool_call_id_duplicate'


@pytest.mark.parametrize('schema', [
    {'$ref': 'https://example.invalid/schema'}, {'$ref': '#/$defs/value'},
    {'type': 'object', '$id': 'https://example.invalid/base'},
    {'type': 'object', '$schema': 'https://example.invalid/dialect'},
    {'type': 'object', 'properties': {'city': {'$dynamicRef': 'https://example.invalid/schema'}}},
    {'type': 'object', 'properties': {'city': {'$recursiveRef': '#'}}},
    {'oneOf': [{'type': 'object'}, {'type': 'object'}]},
    {'type': 'object', 'properties': {'city': {'type': 'string', 'pattern': '^B'}}},
    {'type': ['object', 'object']},
    {'type': 'object', 'description': {'minLength': 999}},
    {'type': 'object', 'properties': {'city': {'type': 'string', 'title': []}}},
    {'type': 'object', 'properties': {'city': {'type': 'string', 'enum': ['Beijing', 'Beijing']}}},
    {'type': 'object', 'required': 'city'}, {}, None,
])
def test_references_dialects_and_unimplemented_constraints_are_unverified(schema):
    a = args(); a['request_body']['tools'][0]['parameters'] = schema
    assert validate_profile_response(**a) == 'interaction_tool_schema_unverified'


@pytest.mark.parametrize('shape', ['deep', 'large', 'cycle'])
def test_schema_complexity_is_bounded(shape):
    a = args(); schema = {'type': 'object'}
    if shape == 'deep':
        for _ in range(30): schema = {'type': 'object', 'properties': {'child': schema}}
    if shape == 'large': schema['properties'] = {str(i): {'type': 'string'} for i in range(5000)}
    if shape == 'cycle': schema['properties'] = {'child': schema}
    a['request_body']['tools'][0]['parameters'] = schema
    assert validate_profile_response(**a) == 'interaction_tool_schema_unverified'


@pytest.mark.parametrize('change', ['no_context', 'host', 'path', 'query', 'userinfo', 'model', 'source', 'store', 'stream', 'background', 'previous', 'agent', 'environment', 'input', 'media', 'profile'])
def test_no_resource_id_exception_stays_in_exact_approved_scope(change):
    a = args()
    if change == 'no_context': a['request_context'] = None
    if change == 'host': a['request_context']['request_url'] = 'https://example.invalid/v1beta/interactions'
    if change == 'path': a['request_context']['request_url'] = URL.replace('v1beta', 'v1')
    if change == 'query': a['request_context']['request_url'] = URL + '?x=1'
    if change == 'userinfo': a['request_context']['request_url'] = URL.replace('https://', 'https://user@')
    if change == 'model': a['request_body']['model'] = 'gemini-other'
    if change == 'source': a['reference_source'] = 'other'
    if change in ('store', 'stream', 'background'): a['request_body'][change] = True
    if change == 'previous': a['request_body']['previous_interaction_id'] = 'parent'
    if change in ('agent', 'environment'): a['request_body'][change] = 'resource'
    if change == 'input': a['request_body']['input'] = [{'type': 'text', 'text': 'hi'}]
    if change == 'media': a['request_body']['response_modalities'] = ['image']
    if change == 'profile': a['profile'] = CONTRACT + '_basic'
    assert validate_profile_response(**a) == 'interaction_id_missing'


@pytest.mark.parametrize('bad', ['missing_tools', 'duplicate', 'builtin', 'unknown_field', 'missing_schema'])
def test_tool_declarations_are_not_inferred_from_response(bad):
    a = args()
    if bad == 'missing_tools': del a['request_body']['tools']
    if bad == 'duplicate': a['request_body']['tools'] *= 2
    if bad == 'builtin': a['request_body']['tools'] = [{'type': 'google_search'}]
    if bad == 'unknown_field': a['request_body']['tools'][0]['execute'] = True
    if bad == 'missing_schema': del a['request_body']['tools'][0]['parameters']
    assert validate_profile_response(**a) in {'interaction_tool_declarations_invalid', 'interaction_tool_schema_unverified'}


@pytest.mark.parametrize('choice', [None, [], {'allowed_tools': {'mode': 'any', 'tools': ['other']}}, {'allowed_tools': {'mode': 'auto', 'tools': ['get_weather', 'get_weather']}}])
def test_malformed_or_undeclared_allowed_tools_choice(choice):
    a = args(); a['request_body']['generation_config']['tool_choice'] = choice
    assert validate_profile_response(**a) == 'interaction_tool_choice_invalid'


def test_allowed_tool_subset_and_actual_mode_are_applied():
    a = args('any'); a['request_body']['generation_config']['tool_choice'] = {'allowed_tools': {'mode': 'any', 'tools': ['get_weather']}}
    assert validate_profile_response(**a) is None


@pytest.mark.parametrize('summary', ['absent', [], '', [{'type': 'text', 'text': ''}]])
def test_auto_summary_may_be_absent_or_empty(summary):
    a = args(calls=False); a['profile'] = CONTRACT + '_thinking_summaries_auto'
    del a['request_body']['tools']; a['request_body']['generation_config'] = {'thinking_summaries': 'auto'}
    if summary != 'absent': a['response_json']['steps'][0]['summary'] = summary
    assert validate_profile_response(**a) is None


@pytest.mark.parametrize('mutation', ['image', 'nested_image', 'summary_number', 'signature_number', 'hidden_media', 'unknown_step', 'call_media', 'response_error'])
def test_malformed_or_media_steps_cannot_borrow_stateless_exception(mutation):
    a = args(); thought = a['response_json']['steps'][0]
    if mutation == 'image': thought['summary'] = [{'type': 'image', 'data': 'synthetic'}]
    if mutation == 'nested_image': thought['summary'] = [{'content': {'type': 'image', 'data': 'synthetic'}}]
    if mutation == 'summary_number': thought['summary'] = 42
    if mutation == 'signature_number': thought['signature'] = 42
    if mutation == 'hidden_media': thought['image'] = 'synthetic'
    if mutation == 'unknown_step': a['response_json']['steps'].append({'type': 'google_search_call'})
    if mutation == 'call_media': a['response_json']['steps'][-1]['image'] = 'synthetic'
    if mutation == 'response_error': a['response_json']['error'] = {'code': 'synthetic_failure'}
    assert validate_profile_response(**a) == 'interaction_stateless_response_invalid'
