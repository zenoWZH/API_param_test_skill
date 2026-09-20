"""Author nine explicit Anthropic cache suites from existing source evidence.

Only a system-prefix nonce changes per job. A single count-shaped template and
two fixed generation fields define all five wires; this is not a template DSL.
Permanent authoring never reads expiring reports or changes execution gates.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = 'R7-APP-PARITY-CLAUDE-SOURCE-SCOPED'
REFERENCE_APPROVAL = 'R7-ALL-MODELS-AUTO-CACHE-LIVE'
COUNT_APPROVAL = 'R7-ALL-MODELS-TOKEN-AUDIT'
APPROVAL_KEY = 'approved_anthropic_cache_fixed_suites_20260908'
MANIFEST_KEY = 'fixed_cache_suite_manifest_20260908'
OBSERVATION_KEY = 'bounded_cache_observations_20260908'
FACT_FILE = 'references/anthropic_automatic_cache_facts_20260908.json'
FACT_SHA256 = 'da03c240af26f8ea0f6df6c54287116b26b396b12ca25ac2d45992f4e5cfef70'
SUITE_FILE = 'references/anthropic_cache_fixed_suites_20260908.json'
SUITE_SHA256 = '0cf2474faa7c6fbad90bf6232e1262f9515ab8a18cf178a22e2c1026b1473711'
SOURCE, CONTRACT, FORM, API_VERSION = 'anthropic', 'claude_native_messages', 'anthropic_messages', '2023-06-01'
GENERATION_ENDPOINT = 'https://api.anthropic.com/v1/messages'
COUNT_ENDPOINT = GENERATION_ENDPOINT + '/count_tokens'
MINIMUMS = {'claude-haiku-4-5-20251001': 4096, 'claude-opus-4-5-20251101': 4096,
    'claude-opus-4-6': 4096, 'claude-opus-4-7': 2048, 'claude-opus-4-8': 1024,
    'claude-opus-5': 512, 'claude-sonnet-4-5-20250929': 1024, 'claude-sonnet-4-6': 1024, 'claude-sonnet-5': 1024}
MODELS = tuple(MINIMUMS)
LABELS = ('count_cold', 'count_negative', 'cold', 'repeat', 'negative')
PLACEHOLDER = '0' * 32
SUFFIX_SHA256 = {'archive': '4a068984b54bd0b4374e9a6b8764e30d5583417574b838832377b209f077abde',
                'library': '3b07916df01791ea2c8cb5e88f0e09ff5a4c235564782495576e3697c1359428'}
QUESTIONS = {'archive': 'Reply briefly with CACHE_PROBE_OK.',
    'library': 'How many hours is Cedar Town Library open on the ordinary Monday described above? Answer briefly.'}
GENERIC_CASES = tuple('claude_native_' + name for name in (
    'max_tokens', 'stream', 'system', 'temperature', 'temperature_nondefault', 'top_p', 'top_p_compat', 'top_p_nondefault',
    'top_k_explicit', 'stop_sequences', 'tools', 'tool_choice_auto', 'thinking_adaptive', 'thinking_disabled', 'thinking_budget',
    'effort_low', 'effort_medium', 'effort_high', 'effort_xhigh', 'effort_max', 'effort_only_low', 'effort_only_medium',
    'effort_only_high', 'effort_only_xhigh', 'effort_only_max', 'disabled_effort_xhigh', 'disabled_effort_max',
    'manual_tool_choice_any', 'adaptive_tool_choice_any', 'metadata'))


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value): return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError('Duplicate JSON member')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))


def _pinned(root, name, expected):
    path = Path(root) / name
    if path.is_symlink() or not path.is_file(): raise ValueError('Reviewed reference must be a regular file: ' + name)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected: raise ValueError('Reviewed reference bytes changed: ' + name)
    return _json(raw)


def suite_id(model):
    if model not in MODELS: raise ValueError('Unknown exact Anthropic cache model')
    return 'anthropic_cache_' + model.replace('-', '_') + '_20260908'


def identity(model):
    suite_id(model)
    pid = 'text/anthropic/claude/' + model
    return {'source_id': SOURCE, 'family_id': 'claude', 'profile_id': pid,
            'interface_id': pid + '#anthropic-messages-default', 'contract_id': CONTRACT,
            'api_form': FORM, 'api_version': API_VERSION, 'request_model_id': model}


def _sample(facts, model):
    observation = next(row for row in facts['observations'] if row['request_model_id'] == model)
    candidates = [(n, row) for n, row in enumerate(observation['samples']) if row['original_summary'].get('cache_effect_verified') is True]
    if len(candidates) != 1: raise ValueError('Expected one qualified historical cache sample per model')
    index, sample = candidates[0]
    library = model == 'claude-opus-5'
    if library != ('/opus5_cache_text_' in sample['batch']): raise ValueError('Opus5 must use its independent library fixture')
    if library and (index != 1 or len(observation['samples']) != 2 or observation['samples'][0]['refusal_preserved'] is not True
                    or observation['samples'][0]['actual_requests'] != 3
                    or observation['samples'][0]['original_summary']['cache_effect_verified'] is not False):
        raise ValueError('Original Opus5 refusal and unused controls must remain preserved')
    return observation, sample, index


def load_facts(*, root=ROOT):
    facts = _pinned(root, FACT_FILE, FACT_SHA256)
    if (facts.get('source_id') != SOURCE or facts.get('approval_code') != REFERENCE_APPROVAL
            or type(facts.get('http_requests')) is not int or facts['http_requests'] != 48
            or facts.get('original_unexecuted_requests') != 2
            or [r.get('request_model_id') for r in facts.get('observations', [])] != list(MODELS)
            or facts.get('permanent_raw_bodies_or_ids') is not False
            or canonical_bytes(facts['documented']['minimum_cacheable_prefix_tokens']) != canonical_bytes(MINIMUMS)):
        raise ValueError('Cache facts crossed the nine-model historical scope')
    for model in MODELS:
        observation, sample, _ = _sample(facts, model)
        exact = identity(model)
        if (any(observation.get(k) != v for k, v in exact.items() if k != 'api_version')
                or observation.get('parameter') != 'cache_control' or observation.get('cache_sample_verified') is not True
                or observation.get('full_parameter_matrix_verified') is not False
                or observation.get('old_failed_samples_preserved') is not True
                or type(sample.get('actual_requests')) is not int or sample['actual_requests'] != 5
                or sample.get('report_retention') != 'P1M' or sample.get('refusal_preserved') is not False
                or [r.get('label') for r in sample.get('case_observations', [])] != list(LABELS)):
            raise ValueError('Cache sample identity, count or historical limits changed')
        controls = sample['case_observations']
        for n, (label, control) in enumerate(zip(LABELS, controls)):
            observed = control['original_observation']
            if (control['case_id'] != model + '/' + label or control['model'] != model
                    or control['kind'] != ('count' if n < 2 else 'generation')
                    or type(control['http_status']) is not int or control['http_status'] != 200
                    or observed.get('pass') is not True):
                raise ValueError('Selected cache control is not qualified for its exact model and label')
            if n < 2:
                count = observed.get('input_estimate')
                if type(count) is not int or not MINIMUMS[model] + 64 <= count <= 16384:
                    raise ValueError('Cache count control no longer meets its declared qualification')
            elif (observed.get('identity_exact') is not True or observed.get('completed_native_text') is not True
                    or control.get('returned_model') != model or control.get('stop_reason') != 'end_turn'
                    or model == 'claude-opus-5' and control.get('new_library_answer_eight_observed') is not True):
                raise ValueError('Cache native identity or library answer attribution changed')
        cold, repeated, negative = [r['original_observation'] for r in controls[2:]]
        if (cold['cached_read_tokens'] != 0 or negative['cached_read_tokens'] != 0
                or not 0 < repeated['cached_read_tokens'] <= cold['cache_creation_tokens']
                or cold['cache_creation_tokens'] < MINIMUMS[model]
                or cold['total_input_tokens'] != repeated['total_input_tokens']):
            raise ValueError('Historical cache causal controls no longer qualify')
    return facts


def _template(model, template):
    kind = 'library' if model == 'claude-opus-5' else 'archive'
    if (type(template) is not dict or set(template) != {'model', 'system', 'messages', 'thinking', 'cache_control'}
            or template['model'] != model or type(template['system']) is not str
            or not template['system'].startswith(PLACEHOLDER)
            or hashlib.sha256(template['system'][32:].encode()).hexdigest() != SUFFIX_SHA256[kind]
            or canonical_bytes(template['messages']) != canonical_bytes([{'role': 'user', 'content': QUESTIONS[kind]}])
            or canonical_bytes(template['thinking']) != canonical_bytes({'type': 'disabled'})
            or canonical_bytes(template['cache_control']) != canonical_bytes({'type': 'ephemeral'})):
        raise ValueError('Cache request template changed its reviewed public body')
    return kind


def _suite(facts, model, template):
    kind = _template(model, template)
    observation, sample, sample_index = _sample(facts, model)
    minimum = MINIMUMS[model]
    nonce_hashes = sorted({c['nonce_sha256'] for s in observation['samples'] for c in s['case_observations']})
    cases = []
    for n, (label, old) in enumerate(zip(LABELS, sample['case_observations'])):
        count = n < 2
        case = {'case_id': suite_id(model) + '/' + label, 'label': label, 'kind': 'count' if count else 'generation',
            'endpoint_constraint': {'scheme': 'https', 'host': 'api.anthropic.com',
                'path': '/v1/messages/count_tokens' if count else '/v1/messages',
                'url': COUNT_ENDPOINT if count else GENERATION_ENDPOINT},
            'nonce_role': 'negative' if label in ('count_negative', 'negative') else 'positive',
            'generation_fields': {} if count else {'max_tokens': 2048, 'stream': False},
            'expectation': {'kind': 'official_input_estimate', 'minimum_input_tokens': minimum + 64,
                'maximum_input_tokens': 16384, 'precision': 'official_estimate'} if count else
                {'kind': 'native_cache_text', 'fixture_kind': kind, 'returned_model': model,
                 'semantic_requirement': 'library_hours_8' if kind == 'library' else 'nonempty_text',
                 'literal_ack_is_diagnostic_only': kind == 'archive'},
            'historical_case_id': old['case_id'], 'historical_request_sha256': old['request_sha256'],
            'historical_response_sha256': old['response_sha256'], 'historical_observation_artifact': copy.deepcopy(old['evidence'])}
        cases.append({**case, 'case_definition_sha256': digest(case)})
    return {'schema_version': 1, 'suite_id': suite_id(model), **identity(model),
        'approval_leaf': APPROVAL, 'reference_approval_leaf': REFERENCE_APPROVAL, 'count_approval_leaf': COUNT_APPROVAL,
        'selection_mode': 'explicit_only', 'generic_runner_dispatch': False,
        'request_cap': 5, 'count_request_cap': 2, 'generation_request_cap': 3,
        'concurrency': 1, 'retries': 0, 'extra_identity_requests': 0, 'max_tokens': 2048,
        'stream': False, 'thinking': {'type': 'disabled'}, 'cache_control': {'type': 'ephemeral'},
        'tools_allowed': False, 'pressure_test_enabled': False, 'legacy_cache_suite_dispatch': False,
        'state_resources': False, 'timeout_seconds': 150, 'response_byte_cap': 2097152,
        'allow_redirects': False, 'report_retention': 'P1M', 'api_total_cost_cap': None,
        'fixture_kind': kind, 'request_template': copy.deepcopy(template), 'request_template_sha256': digest(template),
        'nonce_policy': {'json_pointer': '/system', 'prefix_characters': 32, 'placeholder': PLACEHOLDER,
            'format': 'lowercase_hex_128', 'roles': ['positive', 'negative'], 'distinct_roles': True,
            'generation_time': 'job_creation_only', 'reuse_within_role': True, 'historical_nonce_sha256': nonce_hashes,
            'rendered_requests_frozen_in_job': True, 'other_body_fields_mutable': False},
        'minimum_prefix_tokens': minimum,
        'count_qualification': {'minimum_input_tokens': minimum + 64, 'maximum_input_tokens': 16384, 'precision': 'official_estimate'},
        'native_response_contract': {'type': 'message', 'role': 'assistant', 'returned_model': model,
            'nonempty_id_required': True, 'stop_reason': 'end_turn', 'content_block_types': ['text'],
            'refusal_content_allowed': False, 'tool_or_thinking_blocks_allowed': False,
            'positive_output_required': True, 'maximum_output_tokens': 2048, 'maximum_full_input_tokens': 16384,
            'required_usage_fields': ['input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'],
            'optional_creation_split_fields': ['ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'],
            'creation_split_must_sum_to_creation': True, 'missing_counter_is_zero': False},
        'cache_effect_requirements': {'full_input_fields': ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'],
            'cold_read_zero': True, 'negative_read_zero': True, 'repeat_read_positive': True,
            'repeat_read_lte_cold_creation': True, 'cold_creation_minimum_tokens': minimum,
            'cold_repeat_full_input_equal': True, 'same_reported_inference_geo': True,
            'count_controls_required_before_generation': True, 'refusal_or_incomplete_is_failure': True},
        'case_definitions': cases,
        'facts_artifact': {'path': FACT_FILE, 'sha256': FACT_SHA256,
            'json_pointer': '/observations/' + str(MODELS.index(model)) + '/samples/' + str(sample_index)},
        'historical_reference': {'batch': sample['batch'], 'sample_sha256': digest(sample),
            'selected_sample_index': sample_index, 'raw_expires_at': sample['raw_expires_at'],
            'old_failed_samples_preserved': True, 'old_opus5_refusal_preserved': model == 'claude-opus-5',
            'old_opus5_unused_requests_preserved': 2 if model == 'claude-opus-5' else 0},
        'official_documents': {'cache': facts['documented']['cache'], 'count': facts['documented']['count'],
            'reviewed_at': '2026-09-08', 'minimum_values_origin': 'official_document_not_inferred_from_sample_hits'},
        'full_parameter_matrix_verified': False, 'token_exact_proof': False, 'physical_region_verified': False,
        'provider_ttl_expiry_verified': False, 'future_pass_predetermined': False}


def render_suite_cases(suite, nonces):
    """Closed five-wire renderer; caller generates both nonces once per job."""
    pinned = load_manifest()['suites'].get(suite.get('suite_id')) if type(suite) is dict else None
    if pinned is None or canonical_bytes(pinned) != canonical_bytes(suite):
        raise ValueError('Only a pinned source-authored cache suite may render wires')
    policy = suite['nonce_policy']
    if (type(nonces) is not dict or set(nonces) != {'positive', 'negative'}
            or any(type(v) is not str or not re.fullmatch('[0-9a-f]{32}', v) or v == PLACEHOLDER
                   or hashlib.sha256(v.encode()).hexdigest() in policy['historical_nonce_sha256'] for v in nonces.values())
            or nonces['positive'] == nonces['negative']):
        raise ValueError('Cache job requires two fresh distinct 128-bit hexadecimal nonces')
    result = []
    for case in suite['case_definitions']:
        body = copy.deepcopy(suite['request_template'])
        body['system'] = nonces[case['nonce_role']] + body['system'][32:]
        body.update(copy.deepcopy(case['generation_fields']))
        result.append({'case_id': case['case_id'], 'label': case['label'], 'kind': case['kind'], 'endpoint': case['endpoint_constraint']['url'],
                       'body': body, 'body_sha256': digest(body), 'expectation': copy.deepcopy(case['expectation'])})
    return result


def build_manifest(*, root=ROOT, catalog=None, bindings=None):
    """Explicit optional audit: replay all 48 originals before deriving templates."""
    from scripts import build_anthropic_cache_facts as replay
    facts = load_facts(root=root)
    rebuilt = replay.build(root=root)
    if canonical_bytes(rebuilt) != canonical_bytes(facts): raise ValueError('Cache raw replay differs from reviewed permanent facts')
    if catalog is None or bindings is None:
        import yaml
        data = Path(root) / 'packages/model-profile-db/model_profile_db/data'
        catalog = yaml.safe_load((data / 'catalog.yaml').read_text())
        bindings = yaml.safe_load((data / 'test_extensions.yaml').read_text())['test_bindings']
    suites = {}
    historical_hashes = {}
    for model in MODELS:
        observation, sample, _ = _sample(facts, model)
        batch = Path(root) / sample['batch']
        package = _json((batch / 'request_package.json').read_bytes())
        original_batch = next(row for row in facts['batches'] if row['batch'] == sample['batch'])
        if digest(package) != original_batch['package_sha256']:
            raise ValueError('Cache request package changed after raw replay')
        old_cases = [row for row in package['cases'] if row['model'] == model]
        if [row['label'] for row in old_cases] != list(LABELS): raise ValueError('Cache template requires exactly five original wires')
        template = copy.deepcopy(old_cases[0]['body'])
        positive, negative = (old_cases[n]['body']['system'][:32] for n in (0, 1))
        template['system'] = PLACEHOLDER + template['system'][32:]
        suite = _suite(facts, model, template)
        for case, old, observed in zip(suite['case_definitions'], old_cases, sample['case_observations']):
            body = copy.deepcopy(template)
            body['system'] = (negative if case['nonce_role'] == 'negative' else positive) + body['system'][32:]
            body.update(case['generation_fields'])
            if canonical_bytes(body) != canonical_bytes(old['body']) or digest(body) != old['body_sha256'] or digest(body) != observed['request_sha256']:
                raise ValueError('Cache template no longer reconstructs each original count/generation wire')
        suites[suite_id(model)] = suite
        exact = identity(model)
        leaf = catalog['profiles'][exact['profile_id']]['interfaces']['anthropic-messages-default']['source_conflicts'][OBSERVATION_KEY]['automatic_prompt_cache']
        historical_hashes[model] = digest(leaf)
    parameter = bindings['parameter/' + CONTRACT]
    if parameter['test_cases'] != list(GENERIC_CASES) or parameter['parameter_coverage'].get('cache_control') != 'not tested':
        raise ValueError('Existing generic Claude coverage changed')
    return {'schema_version': 1, 'kind': 'exact_anthropic_nine_model_fixed_cache_suites', 'suites': suites,
        'preconditions': {'historical_cache_observation_sha256': historical_hashes,
            'generic_test_cases_sha256': digest(parameter['test_cases']), 'generic_parameter_coverage_sha256': digest(parameter['parameter_coverage'])},
        'raw_verification': {'historical_requests_replayed': 48, 'raw_artifacts_verified': 160,
            'selected_suite_requests': 45, 'requests_during_replay': 0, 'old_opus5_refusal_preserved': True,
            'old_opus5_unexecuted_requests_preserved': 2, 'all_selected_five_wires_reconstructed': True,
            'raw_report_retention': 'P1M', 'batch_expiries': {row['batch']: row['expires_at'] for row in facts['batches']}},
        'generic_test_cases_changed': False, 'generic_parameter_coverage_changed': False, 'execution_gates_changed': False}


def load_manifest(*, root=ROOT):
    facts = load_facts(root=root)
    manifest = _pinned(root, SUITE_FILE, SUITE_SHA256)
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('kind') != 'exact_anthropic_nine_model_fixed_cache_suites'
            or set(manifest.get('suites', {})) != {suite_id(m) for m in MODELS}
            or any(manifest.get(k) is not False for k in ('generic_test_cases_changed', 'generic_parameter_coverage_changed', 'execution_gates_changed'))):
        raise ValueError('Cache suite manifest scope changed')
    for model in MODELS:
        suite = manifest['suites'][suite_id(model)]
        if canonical_bytes(suite) != canonical_bytes(_suite(facts, model, suite['request_template'])):
            raise ValueError('Cache fixed case definition, nonce rule or expectation changed')
    return manifest


def manifest_entry(suite):
    return {**identity(suite['request_model_id']), 'suite_id': suite['suite_id'], 'request_cap': 5,
        'count_request_cap': 2, 'generation_request_cap': 3, 'approval_leaf': APPROVAL, 'reference_approval_leaf': REFERENCE_APPROVAL,
        'selection_mode': 'explicit_only', 'generic_runner_dispatch': False,
        'suite_artifact': {'path': SUITE_FILE, 'sha256': SUITE_SHA256}, 'suite_definition_sha256': digest(suite),
        'facts_artifact': copy.deepcopy(suite['facts_artifact']), 'request_template_sha256': suite['request_template_sha256'],
        'case_manifest': [{k: c[k] for k in ('case_id', 'kind', 'nonce_role', 'case_definition_sha256')} for c in suite['case_definitions']],
        'old_failed_samples_preserved': True, 'full_parameter_matrix_verified': False, 'execution_gates_changed': False}


def _target(catalog, bindings, resolved, manifest, model):
    exact = identity(model);pid, iid = exact['profile_id'], exact['interface_id']
    try:
        profile = catalog['profiles'][pid];interface = profile['interfaces']['anthropic-messages-default']
        parameter = bindings['parameter/' + CONTRACT];policy = bindings['interface/' + iid.replace('#', '/')]
    except (KeyError, TypeError) as exc: raise ValueError('Exact cache profile/interface/Bindings missing') from exc
    aliases = [model.rsplit('-', 1)[0], model] if model in ('claude-haiku-4-5-20251001', 'claude-opus-4-5-20251101', 'claude-sonnet-4-5-20250929') else [model]
    if (any(profile.get(k) != v for k, v in {'source_id': SOURCE, 'family_id': 'claude', 'modality': 'text', 'model_slug': model,
            'canonical_model_id': 'claude/' + model, 'lifecycle': 'active'}.items())
            or profile.get('request_model_ids') != aliases or profile.get('profile_id', pid) != pid
            or interface.get('source_id', SOURCE) != SOURCE or interface.get('interface_id', iid) != iid
            or interface.get('profile_id', pid) != pid or interface.get('api_form') != FORM
            or interface.get('routing_mode') != 'vendor_direct' or interface.get('transport_adapter_id') != 'claude_messages'
            or interface.get('contract_ids') != [CONTRACT] or interface.get('default_contract_id') != CONTRACT
            or interface.get('request_model_ids', aliases) != aliases
            or interface.get('default_api_version', API_VERSION) != API_VERSION
            or interface.get('api_versions', {API_VERSION: {'path_template': '/v1/messages'}}) != {API_VERSION: {'path_template': '/v1/messages'}}):
        raise ValueError('Cache suite crossed exact source/model/form/route/path')
    if (parameter.get('source_id') != SOURCE or parameter.get('contract_id') != CONTRACT or parameter.get('extension_type') != 'parameter'
            or policy.get('source_id', SOURCE) != SOURCE or policy.get('interface_id') != iid
            or policy.get('contract_id', CONTRACT) != CONTRACT or policy.get('extension_type') != 'model_test_policy'
            or policy.get('reference_contract_ids') != [CONTRACT] or policy.get('default_reference_contract_id') != CONTRACT):
        raise ValueError('Cache suite Binding identity changed')
    for obj in (profile, interface, parameter, policy):
        if any(k in obj and type(obj[k]) is not bool for k in ('enabled', 'executable', 'runner_enabled', 'parameter_test_enabled', 'pressure_test_enabled')):
            raise ValueError('Cache execution gate must retain its boolean type')
    seen, parent = set(), CONTRACT
    while parent:
        if parent in seen: raise ValueError('Cache Contract ancestry cycle')
        seen.add(parent);contract = resolved.get(parent)
        if (type(contract) is not dict or contract.get('source_id', SOURCE) != SOURCE or contract.get('source_ids') != [SOURCE]
                or contract.get('family_id') != 'claude' or contract.get('api_form') != FORM or contract.get('routing_mode') != 'vendor_direct'):
            raise ValueError('Cache Contract source ancestry changed')
        parent = contract.get('parent_contract_id')
    caps = {**resolved[CONTRACT].get('parameter_capabilities', {}), **interface.get('parameter_capabilities', {})}
    constraints = {**resolved[CONTRACT].get('parameter_constraints', {}), **interface.get('parameter_constraints', {})}
    limits = constraints.get('max_tokens', {})
    if (any(caps.get(name, {}).get('state') != 'supported' for name in ('model', 'messages', 'system', 'thinking.type', 'max_tokens', 'stream', 'cache_control'))
            or 'disabled' not in constraints.get('thinking.type', {}).get('allowed_values', [])
            or not limits.get('inclusive_minimum', 1) <= 2048 <= limits.get('inclusive_maximum', 2048)):
        raise ValueError('Cache fixed request fields are unavailable')
    conflicts = interface.get('source_conflicts')
    pre = manifest['preconditions']
    if (type(conflicts) is not dict or type(conflicts.get(OBSERVATION_KEY)) is not dict
            or digest(conflicts[OBSERVATION_KEY].get('automatic_prompt_cache')) != pre['historical_cache_observation_sha256'][model]
            or parameter.get('test_cases') != list(GENERIC_CASES)
            or digest(parameter['test_cases']) != pre['generic_test_cases_sha256']
            or digest(parameter.get('parameter_coverage')) != pre['generic_parameter_coverage_sha256']):
        raise ValueError('Cache historical observation or generic coverage changed')
    return interface, parameter


def apply_anthropic_cache_fixed_suites(catalog, bindings, additions, *, resolved_contracts):
    if additions.get(APPROVAL_KEY) != APPROVAL: return {'applied': False}
    from scripts.migrate_model_profile_database import _resolve_contracts_for_provenance
    fresh = _resolve_contracts_for_provenance(catalog['contracts'])
    if canonical_bytes(fresh) != canonical_bytes(resolved_contracts): raise ValueError('Stale resolved cache Contracts')
    manifest = load_manifest()
    changed, updated = copy.deepcopy(catalog), copy.deepcopy(bindings)
    overrides = {'test-binding/parameter/' + CONTRACT: '2026-09-08'}
    for model in MODELS:
        interface, parameter = _target(changed, updated, fresh, manifest, model)
        suite = manifest['suites'][suite_id(model)];entry = manifest_entry(suite)
        suites = parameter.get('fixed_case_suites', {})
        if type(suites) is not dict: raise ValueError('Cache fixed_case_suites must be an object')
        if suite_id(model) in suites and canonical_bytes(suites[suite_id(model)]) != canonical_bytes(suite):
            raise ValueError('A different cache suite occupies this exact namespace')
        conflicts = interface['source_conflicts']
        if MANIFEST_KEY in conflicts and canonical_bytes(conflicts[MANIFEST_KEY]) != canonical_bytes(entry):
            raise ValueError('A different cache manifest occupies this Interface leaf')
        parameter.setdefault('fixed_case_suites', {})[suite_id(model)] = copy.deepcopy(suite)
        conflicts[MANIFEST_KEY] = entry
        overrides['interface/' + identity(model)['interface_id'].replace('#', '/')] = '2026-09-08'
    catalog.clear();catalog.update(changed);bindings.clear();bindings.update(updated)
    return {'applied': True, 'interface_count': 9, 'parameter_binding_count': 1, 'fixed_suite_count': 9,
        'fixed_case_count': 45, 'generic_test_cases_changed': False, 'generic_parameter_coverage_changed': False,
        'execution_gates_changed': False, 'capabilities_changed': False, 'provenance_retrieved_at_overrides': overrides}
