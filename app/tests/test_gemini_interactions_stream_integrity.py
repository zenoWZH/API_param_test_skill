import copy
import json
from unittest.mock import Mock
import pytest

from lib.client import OpenAICompatibleClient
from lib.gemini_interactions_stream import InteractionStream

MODEL = 'gemini-3.7-flash'
URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'


def events():
    return [
        {'event_type': 'interaction.created', 'interaction': {'id': 's1', 'object': 'interaction', 'model': MODEL, 'status': 'in_progress'}},
        {'event_type': 'interaction.status_update', 'interaction_id': 's1', 'status': 'in_progress'},
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': 'OK'}},
        {'event_type': 'step.stop', 'index': 0},
        {'event_type': 'interaction.completed', 'interaction': {'id': 's1', 'model': MODEL, 'status': 'completed',
            'usage': {'total_input_tokens': 1, 'total_output_tokens': 2, 'total_thought_tokens': 0, 'total_tokens': 3}}},
    ]


def wire(rows, done=True):
    return ''.join('data: '+json.dumps(row)+'\n\n' for row in rows).encode() + (b'data: [DONE]\n\n' if done else b'')


class Response:
    status_code = 200
    headers = {'content-type': 'text/event-stream'}
    encoding = 'utf-8'
    def __init__(self, raw): self.raw = raw
    def iter_lines(self, decode_unicode=False):
        assert decode_unicode is False
        return iter(self.raw.splitlines())
    def __enter__(self): return self
    def __exit__(self, *_): return False


def parse(raw, body=None, url=URL):
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.timeout_sec = 1
    client._transport_url = lambda transport: url
    client._auth_headers = lambda *args: {}
    client.session = Mock()
    client.session.post.return_value = Response(raw)
    return client.gemini_interactions(body or {'model': MODEL, 'input': 'Reply OK', 'stream': True, 'store': False})


def test_complete_native_stream_needs_no_optional_event_ids():
    result = parse(wire(events()))
    assert result.success and result.text == 'OK'
    d = result.response_json['_stream_validation']
    assert d['lifecycle_identity_verified'] and d['event_id_observed_count'] == 0
    assert not d['event_id_required'] and not d['errors']


@pytest.mark.parametrize('mutation,code', [
    ('empty_id', 'mixed_resource_ids'), ('wrong_id', 'identity_conflict'),
    ('wrong_model', 'model_mismatch'), ('missing_created', 'missing_created'),
    ('duplicate_created', 'created_order'), ('missing_terminal', 'missing_terminal'),
    ('missing_done', 'missing_done'), ('unknown_delta_index', 'step_order'),
    ('duplicate_start', 'step_start_invalid'), ('delta_after_stop', 'step_order'),
    ('unclosed_step', 'unclosed_step'), ('bool_index', 'step_index_invalid'),
])
def test_lifecycle_and_step_failures_cannot_be_hidden_by_terminal(mutation, code):
    rows = events()
    if mutation == 'empty_id': rows[1]['interaction_id'] = ''
    if mutation == 'wrong_id': rows[-1]['interaction']['id'] = 's2'
    if mutation == 'wrong_model': rows[-1]['interaction']['model'] = 'other'
    if mutation == 'missing_created': rows.pop(0)
    if mutation == 'duplicate_created': rows.insert(1, copy.deepcopy(rows[0]))
    if mutation == 'missing_terminal': rows.pop()
    if mutation == 'unknown_delta_index': rows[3]['index'] = 99
    if mutation == 'duplicate_start': rows.insert(3, copy.deepcopy(rows[2]))
    if mutation == 'delta_after_stop': rows.insert(5, copy.deepcopy(rows[3]))
    if mutation == 'unclosed_step': rows.pop(4)
    if mutation == 'bool_index': rows[3]['index'] = True
    result = parse(wire(rows, done=mutation != 'missing_done'))
    assert not result.success
    assert any(code in e for e in result.response_json['_stream_validation']['errors'])


@pytest.mark.parametrize('suffix', [b'data: [DONE]\n\n', b'data: {"event_type":"error"}\n\n'])
def test_data_after_done_is_read_and_rejected(suffix):
    result = parse(wire(events())+suffix)
    assert not result.success and 'data_after_done' in result.error_type
    assert result.raw_text.endswith(suffix.decode().rstrip('\n')+'\n')


def test_multiline_data_comments_and_sse_metadata():
    rows = events()
    first = json.dumps(rows[0], indent=2)
    raw = b': keepalive\nretry: 1000\nid: recovery-token\n'
    raw += ''.join('data: '+line+'\n' for line in first.splitlines()).encode()+b'\n'
    result = parse(raw+wire(rows[1:]))
    assert result.success and result.text == 'OK'


@pytest.mark.parametrize('bad', [
    b'data: {"event_type":"step.delta","event_type":"error"}\n\n',
    b'data: {"event_type":"step.delta","value":NaN}\n\n',
    b'data: []\n\n', b'data: {bad-json}\n\n', b'data: \xff\n\n',
])
def test_bad_data_is_not_repaired_by_later_valid_events(bad):
    result = parse(wire(events()[:1], done=False)+bad+wire(events()[1:]))
    assert not result.success and result.response_json['_stream_validation']['errors']


def test_unterminated_last_frame_is_not_dispatched():
    result = parse(wire(events(), done=False)+b'data: [DONE]')
    assert not result.success
    assert 'interaction_stream_incomplete_frame' in result.response_json['_stream_validation']['errors']


def test_terminal_can_omit_redundant_model_but_not_change_it():
    rows = events()
    rows[-1]['interaction'].pop('model')
    result = parse(wire(rows))
    assert result.success and result.response_json['model'] == MODEL


def test_terminal_only_preserves_data_but_does_not_certify_full_stream():
    row = events()[-1]
    row['interaction']['steps'] = [{'type': 'model_output', 'content': [{'type': 'text', 'text': 'terminal'}]}]
    result = parse(wire([row]))
    assert not result.success and result.text == 'terminal'
    assert 'missing_created' in result.error_type


def test_function_argument_json_is_strict_at_step_stop():
    rows = events()
    rows[2]['step'] = {'type': 'function_call', 'id': 'call1', 'name': 'get_weather', 'arguments': {}}
    rows[3]['delta'] = {'type': 'arguments_delta', 'arguments': '{"city":"A","city":"B"}'}
    rows[-1]['interaction']['status'] = 'requires_action'
    result = parse(wire(rows))
    assert not result.success and result.error_type == 'stream_tool_arguments_parse'


def test_protocol_rejects_mismatched_sse_event_name():
    protocol = InteractionStream(MODEL)
    raw = b'event: error\n'+wire(events())
    list(protocol.payloads(raw.splitlines()))
    assert 'interaction_stream_event_name_mismatch' in protocol.errors


@pytest.mark.parametrize('delta', [{'type': 'image', 'data': 'unhandled'}, {'type': 'arguments_delta', 'arguments': '{}'}, {'type': 'text', 'text': 7}])
def test_unhandled_or_mistyped_delta_is_not_silently_discarded(delta):
    rows = events();rows[3]['delta'] = delta
    result = parse(wire(rows))
    assert not result.success and result.response_json['_stream_validation']['errors']


def empty_ids(rows):
    for row in rows:
        if 'interaction' in row: row['interaction']['id'] = ''
        if 'interaction_id' in row: row['interaction_id'] = ''
    return rows


def test_exact_stateless_empty_ids_allow_output_usage_but_not_resource_identity():
    result = parse(wire(empty_ids(events())))
    assert result.success and result.text == 'OK'
    d = result.response_json['_stream_validation']
    assert d['stream_output_usage_verified'] and d['all_lifecycle_ids_exactly_empty']
    assert not d['resource_identity_verified'] and d['resume_verification'] == 'not_tested'


@pytest.mark.parametrize('mutation', ['missing', 'null', 'numeric', 'whitespace', 'mixed'])
def test_empty_id_exception_does_not_expand_to_other_missing_identity(mutation):
    rows = empty_ids(events())
    if mutation == 'missing': rows[1].pop('interaction_id')
    else: rows[1]['interaction_id'] = {'null': None, 'numeric': 0, 'whitespace': ' ', 'mixed': 's1'}[mutation]
    assert not parse(wire(rows)).success


@pytest.mark.parametrize('change', [
    {'store': True}, {'tools': []}, {'agent': 'agent'}, {'previous_interaction_id': 'old'},
    {'background': True}, {'input': [{'type': 'text', 'text': 'hi'}]}, {'last_event_id': 'resume'},
    {'response_modalities': ['image']}, {'response_format': {'mime_type': 'application/json'}},
    {'generation_config': {'tool_choice': 'none'}}, {'model': 'other-model'},
])
def test_empty_id_scope_excludes_other_operation_contexts(change):
    body = {'model': MODEL, 'input': 'hi', 'store': False, 'stream': True, **change}
    assert not parse(wire(empty_ids(events())), body=body).success


@pytest.mark.parametrize('url', ['https://other.example/v1beta/interactions', 'http://generativelanguage.googleapis.com/v1beta/interactions',
    URL+'?last_event_id=resume', URL+'/old', 'https://@generativelanguage.googleapis.com/v1beta/interactions'])
def test_empty_id_scope_uses_actual_client_url(url):
    assert not parse(wire(empty_ids(events())), url=url).success


@pytest.mark.parametrize('usage', [{}, {'total_input_tokens': 1, 'total_output_tokens': 2, 'total_thought_tokens': 0, 'total_tokens': 99},
    {'total_input_tokens': True, 'total_output_tokens': 2, 'total_thought_tokens': 0, 'total_tokens': 3}])
def test_stateless_output_does_not_hide_missing_or_inconsistent_usage(usage):
    rows = empty_ids(events());rows[-1]['interaction']['usage'] = usage
    assert not parse(wire(rows)).success


def test_empty_ids_require_actual_text_deltas_and_exclude_function_calls():
    rows = empty_ids(events());rows[3]['delta'] = {'type': 'text', 'text': ''}
    assert not parse(wire(rows)).success
    rows = empty_ids(events());rows[2]['step'] = {'type': 'function_call', 'id': 'c1', 'name': 'get_weather', 'arguments': {}}
    rows[3]['delta'] = {'type': 'arguments_delta', 'arguments': '{"city":"Beijing"}'}
    rows[-1]['interaction']['status'] = 'requires_action'
    assert not parse(wire(rows)).success


def test_retained_official_empty_ids_verify_stateless_output_without_new_http():
    from datetime import datetime, timezone
    from pathlib import Path
    import hashlib
    import os
    base = Path(__file__).resolve().parents[1]
    if base.name == 'app':
        base = base.parent
        from lib.approved_report_retention import retention as kernel
    else:
        from lib import report_retention as kernel
    batch = base / 'reports/approved_live_20260907/gemini_interactions_budgeted_20260909T213320Z_8a4ac7a8'
    if not batch.exists():
        pytest.skip('Optional retained official batch unavailable')
    now = datetime.now(timezone.utc)
    name = 'case_04_response.txt'
    with kernel._batch_directory(batch) as fd:
        meta = kernel._read_manifest(fd, batch, now)
        if now >= datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00')):
            pytest.skip('Optional retained official batch expired')
        files = {}
        for filename in (name, 'request_package.json'):
            entry = next(r for r in meta['owned_files'] if r['path'] == filename)
            assert kernel._snapshot(fd, filename) == entry
            with os.fdopen(os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), 'rb') as source:
                blob = source.read(2 * 1024 * 1024 + 1)
            assert len(blob) <= 2 * 1024 * 1024 and kernel._snapshot(fd, filename) == entry
            assert hashlib.sha256(blob).hexdigest() == entry['sha256']
            files[filename] = blob
        assert kernel._read_manifest(fd, batch, now) == meta
    raw = files[name]
    assert hashlib.sha256(raw).hexdigest() == '32578bc529b242969338225bc611541c9d39f74f8544e2a7baa5ca22c2a0b04f'
    package = json.loads(files['request_package.json'])
    encoded = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert hashlib.sha256(encoded).hexdigest() == 'd8482c9e9977b45688a62664ce9a1d243f4c09f6fda484b3fdb5ef1ad7b7c50e'
    body = package['cases'][3]['body']
    result = parse(raw, body=body)
    assert result.success and result.error_type is None
    assert result.text and result.usage and result.response_json['id'] == ''
    d = result.response_json['_stream_validation']
    assert d['created_count'] == d['terminal_count'] == d['done_count'] == 1
    assert d['event_count'] == 11 and d['lifecycle_identity_unobservable_count'] == 3
    assert not d['lifecycle_identity_verified'] and d['event_id_observed_count'] == 0
    assert d['stream_output_usage_verified'] and not d['resource_identity_verified']
    from lib.profile_validation import validate_profile_response
    assert validate_profile_response('gemini_3_7_flash_interactions_stream', result.response_json, result,
        request_body=body, transport='gemini_interactions', reference_source='gemini_3_7_flash_interactions',
        request_context={'requested_model': MODEL, 'request_url': URL}) is None


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('terminal', [
    [{'type': 'model_output', 'content': [{'type': 'text', 'text': 'different'}]}],
    [{'type': 'model_output', 'content': [{'type': 'text', 'text': 'OK'}]},
     {'type': 'model_output', 'content': [{'type': 'text', 'text': 'extra'}]}],
    [], None, [{'type': 'model_output', 'content': [{'type': 'image', 'data': 'unverified'}]}],
])
def test_terminal_output_cannot_replace_or_hide_streamed_text(empty, terminal):
    rows = empty_ids(events()) if empty else events()
    rows[-1]['interaction']['steps'] = terminal
    result = parse(wire(rows))
    assert not result.success
    diagnostics = result.response_json['_stream_validation']
    assert 'interaction_stream_terminal_content_mismatch' in diagnostics['errors']
    assert not diagnostics['stream_output_usage_verified']


def test_terminal_text_matches_per_output_slot_and_ignores_thought_internals():
    rows = events()
    rows[2:2] = [
        {'event_type': 'step.start', 'index': 1, 'step': {'type': 'thought'}},
        {'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'thought_summary', 'text': 'public summary'}},
        {'event_type': 'step.stop', 'index': 1},
    ]
    rows[-1]['interaction']['steps'] = [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': 'O'}, {'type': 'text', 'text': 'K'}]},
        {'type': 'thought', 'signature': 'terminal opaque signature'},
    ]
    result = parse(wire(rows))
    assert result.success and result.text == 'OK'
    rows[-1]['interaction']['steps'].pop()
    assert parse(wire(rows)).success  # The terminal can omit redundant thoughts.


def test_terminal_cannot_merge_distinct_output_slots_even_with_equal_total_text():
    rows = events()
    rows[5:5] = [
        {'event_type': 'step.start', 'index': 1, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'text', 'text': 'MORE'}},
        {'event_type': 'step.stop', 'index': 1},
    ]
    rows[-1]['interaction']['steps'] = [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': 'OKMORE'}]},
    ]
    assert not parse(wire(rows)).success


@pytest.mark.parametrize('change', [{}, {'id': 'other'}, {'name': 'other'}, {'arguments': {'city': 'other'}},
                                    {'arguments': {'city': 'Beijing', 'extra': True}}])
def test_terminal_function_call_must_match_streamed_identity_and_arguments(change):
    rows = events()
    call = {'type': 'function_call', 'id': 'call1', 'name': 'get_weather', 'arguments': {'city': 'Beijing'}}
    rows[2]['step'] = {**call, 'arguments': {}}
    rows[3]['delta'] = {'type': 'arguments_delta', 'arguments': '{"city":"Beijing"}'}
    rows[-1]['interaction']['status'] = 'requires_action'
    rows[-1]['interaction']['steps'] = [{**call, **change}]
    result = parse(wire(rows), body={'model': MODEL, 'input': 'weather', 'stream': True, 'store': False,
                                   'tools': [{'type': 'function', 'name': 'get_weather'}]})
    assert result.success is (not change)
    if change:
        assert 'interaction_stream_terminal_content_mismatch' in result.response_json['_stream_validation']['errors']


def test_invalid_utf8_is_charged_to_byte_limits_before_replacement_decoding():
    protocol = InteractionStream(MODEL)
    list(protocol.payloads([b'\xff' * (1024 * 1024)] * 9))
    assert 'interaction_stream_utf8_invalid' in protocol.errors
    assert 'interaction_stream_byte_limit' in protocol.errors
    assert len(protocol.raw_lines) == 1  # A second line would cross the 2 MiB frame cap.


def test_blank_line_separators_count_towards_total_stream_budget():
    protocol = InteractionStream(MODEL)
    # Four ~2 MiB frames fit by content bytes alone. Their many empty line
    # delimiters must also count, otherwise this stream slips past 8 MiB.
    def lines():
        for _ in range(4):
            yield b':' + b'x' * (2 * 1024 * 1024 - 1025)
            yield b''
            for _ in range(1024):
                yield b''
    list(protocol.payloads(lines()))
    assert 'interaction_stream_byte_limit' in protocol.errors
