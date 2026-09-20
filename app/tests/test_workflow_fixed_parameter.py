"""Actual fixed source bodies, domain validators and every counted wire, offline."""
import copy
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from lib import model_profile_catalog as mpdb
from lib.test_runner import IntegrityError, compile_plan
from lib.test_runner.adapters import fixed_parameter as fixed


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('LOADTEST_SKIP_DOTENV', '1')
    monkeypatch.setenv('LLM_API_TEST_PROVIDERS_LOCAL', '/tmp/workflow-fixed-no-private.yaml')


def snapshot(module, model):
    anthropic = module in (fixed.prefill, fixed.cache)
    family = 'claude' if anthropic else 'deepseek'
    provider = 'anthropic_official' if anthropic else 'deepseek_official'
    identity = module.identity(model) if anthropic else {
        'source_id': 'deepseek', 'profile_id': module.PROFILE_ID, 'interface_id': module.INTERFACE_ID}
    catalog = mpdb.get_model_profile_catalog()
    return mpdb.database_snapshot({**mpdb.catalog_metadata(), **identity,
        'reference_source_id': identity['source_id'], 'reference_model_id': model,
        'runtime_provider_id': provider, 'runtime_model_id': model, 'runtime_route_profile': 'vendor_direct',
        'suite_family_id': family, 'canonical_family_id': family, 'catalog_resolved': True,
        'profile': catalog.get_profile(identity['profile_id']), 'interface': catalog.get_interface(identity['interface_id']),
        'binding_source': 'catalog_official_reference', 'execution_target': {'provider_id': provider,
            'request_model_id': model, 'route_profile': 'vendor_direct', 'api_form': module.API_FORM}})


@pytest.fixture(scope='module')
def fim_domain():
    return fixed.fim.build_fim_plan(snapshot(fixed.fim, 'deepseek-v4-pro'), suite_id=fixed.fim.SUITE_ID)


def anthropic_domain(module, model):
    kwargs = {}
    if module is fixed.cache:
        nonces = iter(('a' * 32, 'b' * 32))
        kwargs = {'create_runtime_nonce': True, 'nonce_factory': lambda size: next(nonces)}
    build = module.build_cache_plan if module is fixed.cache else module.build_prefill_plan
    return build(snapshot(module, model), suite_id=module.suite_id(model), **kwargs)


def native_fim(text):
    return {'id': 'offline-fim', 'created': 1, 'object': 'text_completion', 'model': 'deepseek-v4-pro',
        'choices': [{'index': 0, 'finish_reason': 'stop', 'text': text}],
        'usage': {'prompt_tokens': 30, 'completion_tokens': 9, 'total_tokens': 39,
                  'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 30}}


def fim_responses(domain):
    return [(200, native_fim('42')), (200, native_fim('73')),
        (400, {'error': {'type': 'invalid_request_error', 'param': None, 'message': 'echo should not be used with suffix'}}),
        (400, {'error': {'type': 'invalid_request_error', 'param': None,
                       'message': 'Failed to deserialize suffix: invalid type map, expected a string'}}),
        (200, native_fim('LEAD_CUT_HERE_OMEGA')), (200, native_fim('LEAD_')),
        (200, native_fim('42')), (200, native_fim(domain.requests[7].body['prompt'] + '42'))]


def native_anthropic(model, text, *, stopped=False, cache_read=None, cache_write=0):
    usage = {'input_tokens': 8192 - (cache_read or 0) - cache_write, 'output_tokens': 14}
    if cache_read is not None:
        usage.update(cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write, inference_geo='us')
    return {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'model': model,
        'content': [{'type': 'text', 'text': text}], 'stop_reason': 'stop_sequence' if stopped else 'end_turn',
        'stop_sequence': 'CUT_HERE' if stopped else None, 'usage': usage}


def client(domain):
    module = fixed._DOMAINS[fixed._kind(domain)]
    anthropic = module in (fixed.cache, fixed.prefill)
    origin = 'https://api.anthropic.com' if anthropic else 'https://api.deepseek.com'
    provider = 'anthropic_official' if anthropic else 'deepseek_official'
    credential = SimpleNamespace(provider=provider, allowed_origins=frozenset({origin}),
        auth_headers=lambda **kwargs: {}, redact=copy.deepcopy)
    return SimpleNamespace(provider=provider, base_url=origin, _credential=credential,
        _transport_url=lambda transport: module.ENDPOINT,
        api_interfaces={module.TRANSPORT: {'auth': 'anthropic' if anthropic else 'bearer'}})


def execute(domain, rows, directory, *, on_record=None, execution_plan=None):
    calls = []
    class Response:
        def __init__(self, row): self.status_code, self.payload = row
        def iter_content(self, chunk_size): yield json.dumps(self.payload).encode()
        def close(self): pass
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(rows[len(calls) - 1])
    actual, observed = fixed.execute_fixed_parameter_plan(client(domain), domain, directory, request,
        on_record=on_record, execution_plan=execution_plan)
    return calls, actual, observed


def test_fim_all_eight_native_steps_preserve_bodies_and_attributed_negative_controls(fim_domain, tmp_path):
    plan, _ = fixed.prepare_fixed_parameter_plan(fim_domain)
    fixed.registry_for_fixed_parameter_plan(plan)
    calls, records, observed = execute(fim_domain, fim_responses(fim_domain), tmp_path, execution_plan=plan)
    report = observed['workflow_result']
    assert observed['pass'] is True and report['status'] == 'passed'
    assert report['plan_digest'] == plan['plan_digest']
    started = json.loads((tmp_path / 'fim_causal_dispatch_started.json').read_text())
    assert started['workflow_run_id'] == report['runs'][0]['run_id']
    assert started['workflow_plan_digest'] == report['plan_digest']
    assert len(calls) == len(records) == report['runs'][0]['business_request_count'] == 8
    assert report['runs'][0]['cleanup_request_count'] == 0
    assert [call[2]['data'] for call in calls] == [row.body_bytes for row in fim_domain.requests]
    assert all(call[2]['timeout'] == (15, 150) and call[2]['allow_redirects'] is False for call in calls)
    for index in (3, 4):
        negative = report['runs'][0]['steps'][f'fixed.observe.{index:02d}']
        assert negative['status'] == 'passed' and negative['accepted'] is False
    assert observed['code_executed'] is False and observed['token_exact_proof'] is False


@pytest.mark.parametrize('model', fixed.prefill.MODELS)
def test_prefill_three_native_controls_and_no_invented_full_certificate(model, tmp_path):
    domain = anthropic_domain(fixed.prefill, model)
    rows = [(200, native_anthropic(model, 'LEAD_CUT_HERE_OMEGA"}')),
        (200, native_anthropic(model, 'LEAD_', stopped=True)),
        (400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                'message': 'messages.1.content: Input should be a valid string or list'}})]
    calls, actual, observed = execute(domain, rows, tmp_path)
    assert len(calls) == len(actual) == 3 and observed['pass'] is True
    assert observed['workflow_result']['status'] == 'passed'
    assert [call[2]['data'] for call in calls] == [row.body_bytes for row in domain.requests]
    assert observed['full_parameter_matrix_verified'] is False and observed['token_exact_proof'] is False


@pytest.mark.parametrize('model', fixed.cache.MODELS)
def test_cache_plan_uses_frozen_nonce_cold_repeat_negative_dependencies(model):
    domain = anthropic_domain(fixed.cache, model)
    plan, registry = fixed.prepare_fixed_parameter_plan(domain)
    assert plan['limits']['max_requests'] == 3 and plan['run_count'] == 1
    assert [row.label for row in domain.requests] == ['cold', 'repeat', 'negative']
    wires = [s for s in plan['ordered_steps'] if s['handler'] == 'source_fixed.observe']
    refs = wires[-1]['inputs']['source_prerequisites']
    assert [ref['$ref']['step'] for ref in refs] == ['fixed.observe.01', 'fixed.observe.02']
    assert domain.requests[0].body_bytes == domain.requests[1].body_bytes
    assert domain.requests[1].body_bytes != domain.requests[2].body_bytes
    assert plan == fixed.prepare_fixed_parameter_plan(domain.snapshot, suite_id=domain.suite_id,
        frozen_plan=domain.frozen_payload)[0]
    fixed.registry_for_fixed_parameter_plan(plan)


@pytest.mark.parametrize('model', [fixed.cache.MODELS[0], 'claude-opus-5'])
def test_cache_current_three_calls_have_native_semantics_and_exact_no_randomized_bodies(model, tmp_path, monkeypatch):
    domain = anthropic_domain(fixed.cache, model)
    monkeypatch.setattr(fixed.cache.secrets, 'token_hex', lambda *args: pytest.fail('Frozen dispatch randomized'))
    text = 'The library is open for eight hours.' if model == 'claude-opus-5' else 'CACHE_PROBE_OK'
    rows = [(200, native_anthropic(model, text, cache_read=read, cache_write=0 if read else 4096))
            for read in (0, 4096, 0)]
    calls, actual, observed = execute(domain, rows, tmp_path)
    assert len(calls) == len(actual) == 3 and observed['cache_effect_verified'] is True
    assert observed['workflow_result']['status'] == 'passed'
    assert observed['count_requests_sent'] == observed['identity_probe_requests'] == 0
    assert [call[2]['data'] for call in calls] == [row.body_bytes for row in domain.requests]
    assert observed['physical_region_verified'] is observed['provider_ttl_expiry_verified'] is False


def test_missing_positive_native_response_blocks_consumer_without_dispatch(fim_domain, tmp_path):
    rows = fim_responses(fim_domain)
    rows[0] = (200, {'error': {'message': 'wrong native envelope'}})
    calls, _, observed = execute(fim_domain, rows, tmp_path)
    run = observed['workflow_result']['runs'][0]
    assert observed['workflow_result']['status'] != 'passed'
    assert run['steps']['fixed.observe.03']['status'] == 'blocked'
    assert all(call[2]['data'] != fim_domain.requests[2].body_bytes for call in calls)


@pytest.mark.parametrize('mutation', ['body', 'scope', 'nonce', 'floor', 'runs', 'subset'])
def test_self_redigested_plan_change_is_rejected_by_source_factory(fim_domain, mutation):
    plan, registry = fixed.prepare_fixed_parameter_plan(fim_domain)
    workflow = copy.deepcopy(plan['definition'])
    if mutation == 'body': workflow['steps'][0]['inputs']['ordinal'] = 1
    elif mutation == 'scope': workflow['target']['provider'] = 'gateway'
    elif mutation == 'nonce': workflow['fixed_parameter']['frozen_cache_plan'] = {'nonces': {}}
    elif mutation == 'floor': workflow['minimum_output_tokens'] = 1024
    changed = compile_plan(workflow, registry, run_count=2 if mutation == 'runs' else 1,
        selected_cases=[fixed.fim.CASE_IDS[0]] if mutation == 'subset' else None)
    with pytest.raises((ValueError, IntegrityError)):
        fixed.registry_for_fixed_parameter_plan(changed)


def test_stale_plan_is_rejected_before_client_or_artifact(fim_domain, tmp_path):
    plan, _ = fixed.prepare_fixed_parameter_plan(fim_domain)
    plan['plan_digest'] = '0' * 64
    with pytest.raises((ValueError, IntegrityError)):
        fixed.execute_fixed_parameter_plan(object(), fim_domain, tmp_path,
            lambda *args, **kwargs: pytest.fail('unexpected wire'), execution_plan=plan)
    assert list(tmp_path.iterdir()) == []


def test_sigterm_finishes_inflight_record_then_stops_without_next_request(fim_domain, tmp_path):
    def stop(records, observed): os.kill(os.getpid(), signal.SIGTERM)
    calls, records, observed = execute(fim_domain, fim_responses(fim_domain), tmp_path, on_record=stop)
    assert len(calls) == len(records) == 1
    assert observed['workflow_result']['status'] == 'cancelled' and observed['pass'] is False
    ledger = Path(observed['workflow_result']['runs'][0]['ledger_path'])
    assert ledger.is_file() and json.loads(ledger.read_text())['active_request'] is None


def test_beta_pro_only_five_scope_remains_separate_from_generic_beta_eight():
    domain = fixed.beta.build_deepseek_beta_reference_plan(snapshot(fixed.beta, 'deepseek-v4-pro'), endpoint=fixed.beta.ENDPOINT)
    plan, _ = fixed.prepare_fixed_parameter_plan(domain)
    assert plan['selected_cases'] == list(fixed.beta.CASE_IDS) and plan['limits']['max_requests'] == 5
    changed = domain.snapshot
    changed['execution_target']['request_model_id'] = 'deepseek-v4-flash'
    changed['snapshot_digest'] = fixed.digest_json({k:v for k,v in changed.items() if k != 'snapshot_digest'})
    with pytest.raises(ValueError): fixed.prepare_fixed_parameter_plan(changed)


def test_cancel_after_last_wire_cannot_turn_complete_domain_evidence_green(fim_domain, tmp_path):
    def stop(records, observed):
        if len(records) == fim_domain.request_cap:
            os.kill(os.getpid(), signal.SIGTERM)
    calls, records, observed = execute(fim_domain, fim_responses(fim_domain), tmp_path, on_record=stop)
    assert len(calls) == fim_domain.request_cap and observed['domain_validation_pass'] is True
    assert observed['workflow_result']['status'] == 'cancelled'
    assert observed['workflow_execution_pass'] is observed['pass'] is False
