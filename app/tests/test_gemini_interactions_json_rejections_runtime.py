"""Exact Interactions Schema and error attribution through production paths."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from lib.client import ChatResult
from lib.config import load_config, validate_provider_config, get_model_api_form, get_model_transport, get_provider_interface
from lib.deepseek_params import build_request
from lib.gemini_interactions_validation import validate_interactions_rejection
from lib.profile_validation import validate_profile_response
from lib.report_retention import _batch_directory, _read_manifest, _snapshot
from scripts.param_test import run_one_profile

MODEL = 'gemini-3.7-flash'
CONTRACT = 'gemini_3_7_flash_interactions'
URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'
CONTEXT = {'requested_model': MODEL, 'request_url': URL}
MESSAGES = {
    'reject_thinking_minimal': "'minimal' is not a supported thinking level for this model. Allowed values are: high, low, medium.",
    'labels': "The parameter 'labels' is not available on the Gemini API but it is available on the Gemini Enterprise Agent Platform.",
}


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Offline test must not send HTTP')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)


@pytest.fixture(scope='module')
def config():
    # Exercise a detached candidate route, while the real MPDB gate stays shut.
    config = copy.deepcopy(load_config())
    provider = config['providers']['gemini']
    provider['models']['routes'][MODEL]['google_ai_studio']['api_forms']['gemini_interactions'] = {}
    provider['api_interfaces']['gemini_interactions'] = {
        'base_url': 'https://generativelanguage.googleapis.com', 'path': '/v1beta/interactions',
        'auth': 'google_api_key', 'headers': {}}
    return config


def body_for(config, suffix):
    return build_request(config, 'compatibility_profiles', CONTRACT + '_' + suffix,
        overrides={'model': MODEL}, model_family_override='gemini', api_form_override='gemini_interactions',
        route_profile_override='google_ai_studio', reference_source=CONTRACT,
        enforce_model_capabilities=False, parameter_test=True).body


def test_detached_candidate_route_accepts_transport_without_changing_default(config, monkeypatch):
    monkeypatch.delenv('LOADTEST_API_FORM', raising=False)
    default = get_model_api_form(config, MODEL, 'gemini', route_profile='google_ai_studio')
    validate_provider_config(config)
    assert get_model_api_form(config, MODEL, 'gemini', route_profile='google_ai_studio', api_form='gemini_interactions') == 'gemini_interactions'
    monkeypatch.setenv('LOADTEST_API_FORM', 'gemini_interactions')
    assert get_model_transport(config, MODEL, 'gemini', route_profile='google_ai_studio') == 'gemini_interactions'
    interface = get_provider_interface(config, 'gemini_interactions', 'gemini')
    assert interface['path'] == '/v1beta/interactions'
    monkeypatch.delenv('LOADTEST_API_FORM')
    assert get_model_api_form(config, MODEL, 'gemini', route_profile='google_ai_studio') == default


def json_validate(body, text, *, source=CONTRACT, context=CONTEXT, model=MODEL):
    payload = {'id': 'optional_resource', 'object': 'interaction', 'model': model, 'status': 'completed',
        'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': text}]}],
        'usage': {'total_input_tokens': 2, 'total_output_tokens': 2, 'total_thought_tokens': 0, 'total_tokens': 4}}
    return validate_profile_response(CONTRACT + '_response_format_json', payload,
        SimpleNamespace(success=True, usage=payload['usage']), request_body=body,
        transport='gemini_interactions', reference_source=source, request_context=context)


@pytest.mark.parametrize('value,passes', [
    ({'summary': 'ok', 'items': []}, True), ({'summary': 'ok', 'items': ['a', 'b']}, True),
    ({}, False), ({'summary': 'ok'}, False), ({'summary': 5, 'items': []}, False),
    ({'summary': 'ok', 'items': [5]}, False), ({'summary': 'ok', 'items': 'a'}, False),
    ({'summary': 'ok', 'items': [], 'extra': 1}, False),
])
def test_actual_default_schema_is_checked(config, value, passes):
    body = body_for(config, 'response_format_json'); before = copy.deepcopy(body)
    error = json_validate(body, json.dumps(value))
    assert (error is None) is passes
    if not passes: assert error == 'json_schema_mismatch'
    assert body == before


@pytest.mark.parametrize('expected,actual', [('ALPHA', 'ALPHA'), ('ALPHA', 'BETA'), ('BETA', 'BETA'), ('BETA', 'ALPHA')])
def test_schema_is_taken_from_actual_request_enum(config, expected, actual):
    body = body_for(config, 'response_format_json')
    body['response_format']['schema'] = {'type': 'object', 'properties': {'marker': {'type': 'string', 'enum': [expected]}},
        'required': ['marker'], 'additionalProperties': False}
    assert (json_validate(body, json.dumps({'marker': actual})) is None) is (expected == actual)


@pytest.mark.parametrize('text', ['not json', '{"summary":"a","summary":"b","items":[]}', '{"summary":"x","items":[NaN]}'])
def test_invalid_or_ambiguous_json_is_rejected(config, text):
    assert json_validate(body_for(config, 'response_format_json'), text) == 'json_parse'


@pytest.mark.parametrize('schema', [
    {'$ref': 'https://example.invalid/schema'}, {'$ref': '#/$defs/local'},
    {'type': 'object', 'properties': {'marker': {'$dynamicRef': '#local'}}},
    {'type': 'object', '$id': 'https://example.invalid/'},
    {'oneOf': [{'type': 'object'}, {'type': 'object'}]},
    {'type': 'object', 'properties': {'marker': {'type': 'string', 'pattern': '^A'}}},
    {'type': 'object', 'description': {}}, {'type': ['object', 'object']}, {'type': None},
])
def test_unknown_or_invalid_schema_is_unverified(config, schema):
    body = body_for(config, 'response_format_json'); body['response_format']['schema'] = schema
    assert json_validate(body, '{}') == 'interaction_schema_unverified'


@pytest.mark.parametrize('mutation', ['source', 'missing_context', 'url', 'model', 'store', 'stream'])
def test_json_schema_requires_exact_source_model_and_actual_url(config, mutation):
    body = body_for(config, 'response_format_json'); kwargs = {}
    if mutation == 'source': kwargs['source'] = 'other'
    if mutation == 'missing_context': kwargs['context'] = None
    if mutation == 'url': kwargs['context'] = {'requested_model': MODEL, 'request_url': 'https://example.invalid/v1beta/interactions'}
    if mutation == 'model': kwargs['model'] = 'gemini-other'
    if mutation in ('store', 'stream'): body[mutation] = True
    assert json_validate(body, '{"summary":"ok","items":[]}', **kwargs) == 'interaction_schema_scope_unverified'


def rejection(config, suffix, *, status=400, code='invalid_request', message=None, context=CONTEXT, body=None):
    return validate_interactions_rejection(CONTRACT + '_' + suffix,
        {'error': {'code': code, 'message': MESSAGES[suffix] if message is None else message}}, status,
        body=body_for(config, suffix) if body is None else body, transport='gemini_interactions',
        reference_source=CONTRACT, request_context=context)


@pytest.mark.parametrize('suffix', MESSAGES)
def test_two_exact_live_error_forms_are_attributed(config, suffix):
    assert rejection(config, suffix) is None


@pytest.mark.parametrize('suffix', MESSAGES)
@pytest.mark.parametrize('change', ['generic', 'wrong_field', 'malformed_tool', 'account_code', 'account_message', '401', '403', '422', 'missing_context', 'wrong_url', 'changed_request'])
def test_unrelated_errors_cannot_stand_in_for_parameter_rejection(config, suffix, change):
    kwargs = {}
    if change == 'generic': kwargs['message'] = 'Invalid request'
    if change == 'wrong_field': kwargs['message'] = MESSAGES['labels' if suffix != 'labels' else 'reject_thinking_minimal']
    if change == 'malformed_tool': kwargs['code'] = 'malformed_tool_call'
    if change == 'account_code': kwargs['code'] = 'permission_denied'
    if change == 'account_message': kwargs['message'] = 'API key has insufficient quota or permissions.'
    if change in ('401', '403', '422'): kwargs['status'] = int(change)
    if change == 'missing_context': kwargs['context'] = None
    if change == 'wrong_url': kwargs['context'] = {'requested_model': MODEL, 'request_url': 'https://other.invalid/v1beta/interactions'}
    if change == 'changed_request':
        body = body_for(config, suffix)
        if suffix == 'labels': body.pop('labels')
        else: body['generation_config']['thinking_level'] = 'low'
        kwargs['body'] = body
    assert rejection(config, suffix, **kwargs) is not None


@pytest.mark.parametrize('suffix', MESSAGES)
@pytest.mark.parametrize('kind,status,expected', [
    ('matched', 400, 'expected_rejection'), ('generic', 400, 'incompatible'),
    ('malformed', 400, 'incompatible'), ('matched', 422, 'incompatible'),
    ('matched', 401, 'fail'), ('matched', 403, 'fail'), ('matched', 200, 'unexpected_acceptance'),
    ('foreign_url', 400, 'incompatible'),
])
def test_real_runner_does_not_discard_attribution_result(config, suffix, kind, status, expected):
    class Client:
        calls = 0
        def _transport_url(self, transport):
            assert transport == 'gemini_interactions'
            return 'https://other.invalid/v1beta/interactions' if kind == 'foreign_url' else URL
        def gemini_interactions(self, body):
            self.calls += 1
            message = 'Invalid request' if kind == 'generic' else MESSAGES[suffix]
            code = 'malformed_tool_call' if kind == 'malformed' else 'invalid_request'
            payload = {'error': {'code': code, 'message': message}}
            if status == 200:
                payload = {'object': 'interaction', 'model': MODEL, 'status': 'completed',
                    'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': 'OK'}]}],
                    'usage': {'total_input_tokens': 1, 'total_output_tokens': 1, 'total_thought_tokens': 0, 'total_tokens': 2}}
            return ChatResult(success=status == 200, status_code=status, latency_ms=1, timestamp=0,
                response_json=payload, usage=payload.get('usage', {}), error_type=None if status == 200 else 'http_error')
    client = Client()
    result = run_one_profile(config, client, 'gemini', MODEL, 'gemini', CONTRACT, 'gemini',
        CONTRACT + '_' + suffix, 1, {'id': 'offline', 'prompt': 'Reply OK.'}, expectation='unsupported')
    assert client.calls == 1 and result['status'] == expected
    assert result['compatibility_pass'] is (expected == 'expected_rejection')


def test_retained_official_json_and_two_rejections_keep_original_facts():
    root = Path(__file__).resolve().parents[1]
    if root.name == 'app': root = root.parent
    fact_path = root / 'references/gemini_interactions_budgeted_facts_20260909.json'
    fact_raw = fact_path.read_bytes()
    assert hashlib.sha256(fact_raw).hexdigest() == 'df3527c813726a42b4a87434c64892cfefc26b4f843fc5a372b407ec576b7aa3'
    fact = json.loads(fact_raw)
    def retained(link):
        path = root / link['path']
        assert path.parent.parent == root / 'reports/approved_live_20260907'
        if not path.parent.exists(): pytest.skip('Optional P1M raw batch unavailable')
        with _batch_directory(path.parent) as fd:
            now = datetime.now(timezone.utc); meta = _read_manifest(fd, path.parent, now)
            if now >= datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00')):
                pytest.skip('Optional P1M raw batch expired')
            assert meta['expires_at'] == link['expires_at']
            owner = next(r for r in meta['owned_files'] if r['path'] == path.name)
            assert _snapshot(fd, path.name) == owner and owner['sha256'] == link['sha256']
            with os.fdopen(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb') as stream:
                blob = stream.read(owner['size'] + 1)
            assert len(blob) == owner['size'] and hashlib.sha256(blob).hexdigest() == owner['sha256']
            assert _snapshot(fd, path.name) == owner and _read_manifest(fd, path.parent, now) == meta
        return json.loads(blob)
    count = 0
    for row in fact['parameter_observations']:
        suffix = row['profile'].removeprefix(CONTRACT + '_')
        if suffix not in ('response_format_json', *MESSAGES): continue
        links = {Path(link['path']).name.rsplit('_', 1)[-1]: link for link in row['evidence_links']}
        attempt = retained(links['attempt.json']); payload = retained(links['response.txt'])
        case = attempt['case']; context = {'requested_model': case['body']['model'], 'request_url': case['url']}
        if suffix == 'response_format_json':
            assert row['http_status'] == 200
            assert validate_profile_response(row['profile'], payload, SimpleNamespace(success=True, usage=payload['usage']),
                request_body=case['body'], transport='gemini_interactions', reference_source=CONTRACT, request_context=context) is None
        else:
            assert row['original_evaluation']['field_attributed_rejection'] is False
            assert validate_interactions_rejection(row['profile'], payload, row['http_status'], body=case['body'],
                transport='gemini_interactions', reference_source=CONTRACT, request_context=context) is None
        count += 1
    assert count == 3 and fact_path.read_bytes() == fact_raw
