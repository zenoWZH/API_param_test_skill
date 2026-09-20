"""Pure fixed-suite preview from an already resolved, exact source identity."""
from __future__ import annotations

import copy

from .adapters import fixed_parameter as fixed


def fixed_execution_config(config, domain):
    """Supply the reviewed Pro-only beta wire in a runtime copy when absent.

    Root historically had no ordinary beta transport. This exact immutable
    source plan authorizes its own endpoint without changing global API gates.
    An existing interface is never repaired or replaced by this helper.
    """
    from lib.config import get_provider_config
    runtime = copy.deepcopy(config)
    if type(domain) is fixed.beta.DeepSeekBetaReferencePlan:
        provider = domain.snapshot['execution_target']['provider_id']
        selected = get_provider_config(runtime, provider)
        if provider != 'deepseek_official' or str(selected.get('base_url') or '').rstrip('/') not in (
                'https://api.deepseek.com', 'https://api.deepseek.com/v1') or domain.endpoint != fixed.beta.ENDPOINT:
            raise ValueError('Fixed beta execution requires its exact official source endpoint')
        interfaces = runtime['providers'][provider].setdefault('api_interfaces', {})
        if fixed.beta.TRANSPORT not in interfaces:
            interfaces[fixed.beta.TRANSPORT] = {'base_url': 'https://api.deepseek.com',
                'path': '/beta/chat/completions', 'auth': 'bearer'}
    return runtime


def validate_fixed_configuration(config, domain):
    """Reject route/auth changes before any caller constructs its credential."""
    from lib.config import get_provider_config, get_provider_interface
    from lib.gemini_api_version import build_gemini_api_url
    config = fixed_execution_config(config, domain)
    module = fixed._DOMAINS[fixed._kind(domain)]
    provider = domain.snapshot['execution_target']['provider_id']
    anthropic = provider == 'anthropic_official'
    origin = 'https://api.anthropic.com' if anthropic else 'https://api.deepseek.com'
    provider_config = get_provider_config(config, provider)
    if type(domain) is fixed.beta.DeepSeekBetaReferencePlan:
        interface = copy.deepcopy(provider_config['api_interfaces'][module.TRANSPORT])
        interface['base_url'] = str(interface.get('base_url') or provider_config.get('base_url') or '').rstrip('/')
        if not isinstance(interface.get('path'), str):
            raise ValueError('Fixed beta route requires an explicit frozen path')
    else:
        interface = get_provider_interface(config, module.TRANSPORT, provider)
    url = build_gemini_api_url(interface['base_url'], interface['path'],
        api_version=interface.get('api_version') or interface.get('default_api_version'))
    if (str(provider_config.get('base_url') or '').rstrip('/') not in (origin, origin + '/v1')
            or url != module.ENDPOINT or interface.get('auth') != ('anthropic' if anthropic else 'bearer')
            or anthropic and interface.get('anthropic_version', '2023-06-01') != '2023-06-01'):
        raise ValueError('Fixed suite configuration changes the exact official route or authentication')


def preview_fixed_parameter_plan(config, payload, *, model_profile_database, model_capability_profile,
                                 provider, model, model_family, api_form, route_profile, reference_contract_id):
    """Freeze one complete approved experiment; return its reusable nonce seed.

    This function neither resolves credentials nor performs HTTP. The caller
    resolves the model/API/contract through the ordinary exact MPDB policy first.
    A cache preview creates two nonce values once; creation passes back plan_seed
    and plan_digest so it cannot quietly randomize a reviewed body.
    """
    from lib.job_spec import make_job_spec, build_result_validation_contract
    from lib.parameter_job_controls import freeze_workflow_job
    from lib.parameter_output_limit import configured_parameter_test_min_output_tokens
    if not isinstance(payload, dict) or payload.get('type', 'param_test') != 'param_test':
        raise ValueError('Fixed suites require a functional parameter job')
    for field in ('runs', 'param_test_runs', 'run_count'):
        if field in payload and (type(payload[field]) is not int or payload[field] != 1):
            raise ValueError('The fixed source scope requires exactly one complete run')
    if payload.get('suite', 'full') != 'full' or any(payload.get(key) is not None for key in ('cases', 'selected_cases', 'requested_cases')):
        raise ValueError('The fixed source scope requires the full ordered suite')
    if payload.get('workflow_id') or payload.get('workflow_binding_id'):
        raise ValueError('Fixed source suites use their existing exact parameter binding')
    identity = {'provider': provider, 'model': model, 'model_family': model_family,
                'api_form': api_form, 'route_profile': route_profile, 'reference_contract_id': reference_contract_id}
    for field, expected in identity.items():
        if payload.get(field) not in (None, '', expected):
            raise ValueError('Fixed preview selection differs from resolved identity: ' + field)
    snapshot = copy.deepcopy(model_profile_database)
    for field in ('source_id', 'profile_id', 'interface_id', 'test_binding_id'):
        if payload.get(field) not in (None, '', snapshot.get(field)):
            raise ValueError('Fixed preview source selection changed: ' + field)
    if snapshot.get('reference_contract_id') != reference_contract_id:
        raise ValueError('Fixed preview contract differs from its source snapshot')
    target = snapshot.get('execution_target') or {}
    if any(target.get(field) != value for field, value in (
            ('provider_id', provider), ('request_model_id', model), ('api_form', api_form), ('route_profile', route_profile))):
        raise ValueError('Fixed preview runtime differs from its source snapshot')
    capability = copy.deepcopy(model_capability_profile)
    if not isinstance(capability, dict) or any(capability.get(field) is not True for field in (
            'known_model', 'known_api_profile', 'route_profile_known', 'parameter_test_enabled')):
        raise ValueError('Fixed preview requires the existing enabled exact parameter policy')
    if capability.get('model_profile_database') not in (None, snapshot):
        raise ValueError('Fixed preview capability contains a conflicting source snapshot')
    capability['model_profile_database'] = copy.deepcopy(snapshot)
    suite = payload.get('parameter_suite') or None
    seed = payload.get('plan_seed')
    cache_selected = suite in fixed.cache.SUITE_IDS
    if seed is not None and not cache_selected:
        raise ValueError('Only a fixed cache plan carries a nonce seed')
    if payload.get('plan_digest') is not None and cache_selected and seed is None:
        raise ValueError('A reviewed cache plan requires its frozen nonce seed')
    plan, _ = fixed.prepare_fixed_parameter_plan(snapshot, suite_id=suite, frozen_plan=seed,
        create_runtime_nonce=cache_selected and seed is None,
        minimum_output_tokens=configured_parameter_test_min_output_tokens(config))
    domain = fixed.fixed_domain_from_plan(plan)
    validate_fixed_configuration(config, domain)
    if payload.get('plan_digest') is not None and payload['plan_digest'] != plan['plan_digest']:
        raise ValueError('The selected fixed parameter plan changed since preview')
    capability['selected_fixed_case_ids'] = list(plan['selected_cases'])
    if suite:
        capability['fixed_parameter_suite'] = suite
    module = fixed._DOMAINS[fixed._kind(domain)]
    job = make_job_spec(job_type='param_test', provider=provider, model=model, model_family=model_family,
        api_form=api_form, route_profile=route_profile,
        model_profile_id=capability.get('model_api_profile_id'),
        source_id=snapshot['source_id'], profile_id=snapshot['profile_id'], interface_id=snapshot['interface_id'],
        test_binding_id=snapshot['test_binding_id'], reference_contract_id=reference_contract_id,
        model_profile_database=snapshot, model_capability_profile=capability,
        transport=module.TRANSPORT, reference_route_profile='vendor_direct',
        workload='param_test', request_mode='fixed', target_rpm=0.0, target_tpm=0.0,
        param_test_runs=1, tool_validation_mode=payload.get('tool_validation_mode') or 'auto',
        result_contract=build_result_validation_contract(config))
    if suite:
        job['parameter_suite'] = suite
    if cache_selected:
        job['fixed_parameter_plan'] = copy.deepcopy(domain.frozen_payload)
    job = freeze_workflow_job(job, plan, payload.get('tool_validation_mode') or 'auto')
    job['timeout_sec'] = plan['limits']['request_timeout_seconds']
    return {'plan': plan, 'job_spec': job, 'plan_digest': plan['plan_digest'],
        'plan_seed': copy.deepcopy(domain.frozen_payload) if cache_selected else None,
        'selected_cases': list(plan['selected_cases']), 'ordered_steps': copy.deepcopy(plan['ordered_steps']),
        'request_cap': plan['limits']['max_requests'], 'cleanup_request_cap': 0,
        'preconditions': [{'case_id': step['case_id'], 'requirements': copy.deepcopy(step['inputs']['source_prerequisites'])}
            for step in plan['ordered_steps'] if step['inputs'].get('source_prerequisites')],
        'target': copy.deepcopy(plan['target'])}
