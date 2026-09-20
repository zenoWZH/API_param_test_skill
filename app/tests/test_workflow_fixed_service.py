"""Nonce-preserving fixed source previews remain pure and complete-scope only."""
import copy
from unittest.mock import Mock

import pytest

from lib.config import load_config
from lib.test_runner.fixed_service import preview_fixed_parameter_plan
from lib.test_runner.adapters import fixed_parameter as fixed
from test_workflow_fixed_parameter import snapshot, fim_domain


@pytest.fixture(autouse=True)
def pure(monkeypatch):
    monkeypatch.setenv('LOADTEST_SKIP_DOTENV', '1')
    monkeypatch.setenv('LLM_API_TEST_PROVIDERS_LOCAL', '/tmp/fixed-preview-no-private.yaml')
    from lib import client
    import requests
    monkeypatch.setattr(client, 'get_api_key', Mock(side_effect=AssertionError('preview read credentials')))
    monkeypatch.setattr(requests.Session, 'request', Mock(side_effect=AssertionError('preview sent HTTP')))


def arguments(database):
    target = database['execution_target']
    return {'model_profile_database': database, 'model_capability_profile': {
        'known_model': True, 'known_api_profile': True, 'route_profile_known': True,
        'parameter_test_enabled': True, 'model_profile_database': database},
        'provider': target['provider_id'], 'model': target['request_model_id'],
        'model_family': database['family_id'], 'api_form': target['api_form'],
        'route_profile': target['route_profile'], 'reference_contract_id': database['reference_contract_id']}


def test_fixed_cache_preview_freezes_once_and_create_reuses_same_two_nonces(monkeypatch):
    model = fixed.cache.MODELS[0]
    database = snapshot(fixed.cache, model)
    random = Mock(side_effect=['c' * 32, 'd' * 32])
    monkeypatch.setattr(fixed.cache.secrets, 'token_hex', random)
    payload = {'type': 'param_test', 'parameter_suite': fixed.cache.suite_id(model)}
    first = preview_fixed_parameter_plan(load_config(), payload, **arguments(database))
    assert random.call_count == 2 and first['request_cap'] == 3 and first['cleanup_request_cap'] == 0
    assert first['job_spec']['schema_version'] == 6
    assert first['job_spec']['fixed_parameter_plan'] == first['plan_seed']
    assert first['job_spec']['execution_plan'] == first['plan']
    second = preview_fixed_parameter_plan(load_config(), {**payload, 'plan_seed': first['plan_seed'],
        'plan_digest': first['plan_digest']}, **arguments(database))
    assert second == first and random.call_count == 2
    with pytest.raises(ValueError, match='seed'):
        preview_fixed_parameter_plan(load_config(), {**payload, 'plan_digest': first['plan_digest']}, **arguments(database))
    assert random.call_count == 2


@pytest.mark.parametrize('change', [{'runs': 2}, {'param_test_runs': True}, {'run_count': 0},
    {'cases': ['any']}, {'cases': []}, {'suite': 'smoke'}, {'type': 'cache_suite'}, {'provider': 'other'}])
def test_fixed_preview_rejects_scope_change_without_credentials_or_nonce(fim_domain, change):
    with pytest.raises(ValueError):
        preview_fixed_parameter_plan(load_config(), {'parameter_suite': fixed.fim.SUITE_ID, **change},
            **arguments(fim_domain.snapshot))


def test_fixed_preview_validates_existing_policy_and_current_route(fim_domain):
    kwargs = arguments(fim_domain.snapshot)
    kwargs['model_capability_profile']['parameter_test_enabled'] = False
    with pytest.raises(ValueError, match='policy'):
        preview_fixed_parameter_plan(load_config(), {'parameter_suite': fixed.fim.SUITE_ID}, **kwargs)
    config = load_config()
    config['providers']['deepseek_official']['api_interfaces'][fixed.fim.TRANSPORT]['path'] = '/changed'
    with pytest.raises(ValueError, match='route'):
        preview_fixed_parameter_plan(config, {'parameter_suite': fixed.fim.SUITE_ID}, **arguments(fim_domain.snapshot))


def test_fixed_preview_detects_stale_digest_and_redundant_snapshot_mismatch(fim_domain):
    kwargs = arguments(fim_domain.snapshot)
    with pytest.raises(ValueError, match='changed since preview'):
        preview_fixed_parameter_plan(load_config(), {'parameter_suite': fixed.fim.SUITE_ID, 'plan_digest': '0' * 64}, **kwargs)
    kwargs['model_capability_profile']['model_profile_database'] = copy.deepcopy(fim_domain.snapshot)
    kwargs['model_capability_profile']['model_profile_database']['source_id'] = 'other'
    with pytest.raises(ValueError, match='conflicting source'):
        preview_fixed_parameter_plan(load_config(), {'parameter_suite': fixed.fim.SUITE_ID}, **kwargs)


def test_app_make_job_spec_reuses_workflow_cache_plan_without_randomizing(monkeypatch):
    import inspect
    from lib.job_spec import make_job_spec
    database = snapshot(fixed.cache, fixed.cache.MODELS[0])
    payload = {'parameter_suite': fixed.cache.suite_id(fixed.cache.MODELS[0])}
    preview = preview_fixed_parameter_plan(load_config(), payload, **arguments(database))
    job = preview['job_spec']
    random = Mock(side_effect=AssertionError('frozen JobSpec randomized cache'))
    monkeypatch.setattr(fixed.cache.secrets, 'token_hex', random)
    allowed = inspect.signature(make_job_spec).parameters
    kwargs = {key: copy.deepcopy(value) for key, value in job.items() if key in allowed}
    rebuilt = make_job_spec(job_type=job['type'], **kwargs)
    assert rebuilt['fixed_parameter_plan'] == job['fixed_parameter_plan']
    assert rebuilt['execution_plan'] == job['execution_plan'] and not random.called
    kwargs['parameter_suite'] = fixed.cache.suite_id(fixed.cache.MODELS[1])
    with pytest.raises(ValueError): make_job_spec(job_type=job['type'], **kwargs)
    assert not random.called


def test_beta_runtime_completion_is_local_and_never_repairs_a_changed_existing_route():
    from lib.test_runner.fixed_service import fixed_execution_config, validate_fixed_configuration
    domain = fixed.beta.build_deepseek_beta_reference_plan(snapshot(fixed.beta, 'deepseek-v4-pro'), endpoint=fixed.beta.ENDPOINT)
    config = load_config()
    config['providers']['deepseek_official']['api_interfaces'].pop(fixed.beta.TRANSPORT, None)
    before = copy.deepcopy(config)
    runtime = fixed_execution_config(config, domain)
    assert config == before and fixed.beta.TRANSPORT not in config['providers']['deepseek_official']['api_interfaces']
    assert runtime['providers']['deepseek_official']['api_interfaces'][fixed.beta.TRANSPORT]['path'] == '/beta/chat/completions'
    validate_fixed_configuration(config, domain)
    runtime['providers']['deepseek_official']['api_interfaces'][fixed.beta.TRANSPORT]['path'] = '/wrong'
    with pytest.raises(ValueError, match='route'):
        validate_fixed_configuration(runtime, domain)
