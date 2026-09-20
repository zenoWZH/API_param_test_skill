"""Fresh source-scoped Kimi/MiniMax controls on shared dispatch, without private history.

The public request reference supplies immutable bodies and exact source identities.
New nonstream prerequisites replace historical-baseline dependencies; observations
never reclassify historical evidence or certify untyped parameter acceptance.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

import yaml

from scripts import run_kimi_minimax_stream_reference as controls
from scripts import run_minimax_m25_json_stream_reference as json_controls
from scripts import kimi_minimax_stream_evidence as evidence
from .. import CancellationContext, HandlerRegistry, compile_plan, execute_plan, validate_plan
from ..common import digest_json
from ..transport import HttpDispatcher

ROOT = Path(__file__).resolve().parents[3]
FACTORY_ID = 'kimi_minimax_controls'
FACTORY_VERSION = '1'
SHARED_MARKER = 'shared_workflow_plan.json'
SHARED_SOURCE_FILES = (
    'lib/test_runner/adapters/kimi_minimax.py',
    'scripts/run_kimi_minimax_stream_reference.py',
    'scripts/run_minimax_m25_json_stream_reference.py',
    'scripts/kimi_minimax_stream_evidence.py',
    'scripts/prepare_zai_general_reference.py', controls.REFERENCE,
)
RUNTIME_DEPENDENCY_FILES = tuple(dict.fromkeys((
    'lib/test_runner/adapters/kimi_minimax.py', *controls.CODE_FILES,
    'scripts/run_minimax_m25_json_stream_reference.py', 'lib/test_runner/transport.py', controls.REFERENCE,
)))
# These are source-matrix terminal transport conditions, frozen into every plan.
FATAL_HTTP_CODES = [*range(300, 400), 401, 402, 403, 404, 408, 429, *range(500, 600)]
PUBLIC_TARGET_FIELDS = ('target_key', 'source_id', 'provider_id', 'request_model_id', 'selection',
                        'output_cap', 'endpoint', 'auth_mode', 'requires_new_nonstream_bytes_baseline',
                        'baseline_body', 'baseline_body_sha256', 'expectation')


def source_digest():
    return digest_json({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SHARED_SOURCE_FILES})


def execution_digest():
    """Freeze each platform's credential/config/retention glue independently."""
    return digest_json({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in RUNTIME_DEPENDENCY_FILES})


def public_config():
    """Read only checked-in provider routes; no dotenv/private-overlay loading."""
    return yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))


def public_targets():
    return [{key: copy.deepcopy(row[key]) for key in PUBLIC_TARGET_FIELDS} for row in controls.reference()['targets']]


def _select(payload, target_key=None):
    if not isinstance(payload, dict):
        raise ValueError('Kimi/MiniMax selection must be an object')
    chosen = target_key or payload.get('target_key')
    if target_key is not None and payload.get('target_key') not in (None, target_key):
        raise ValueError('Conflicting exact target selectors')
    rows = [row for row in public_targets() if (chosen is None or row['target_key'] == chosen)]
    for field, key in (('provider', 'provider_id'), ('provider_id', 'provider_id'), ('model', 'request_model_id'),
                       ('request_model_id', 'request_model_id'), ('source_id', 'source_id')):
        if payload.get(field) is not None:
            rows = [row for row in rows if row[key] == payload[field]]
    if payload.get('api_form', controls.FORM) != controls.FORM or len(rows) != 1:
        raise ValueError('Expected one exact official Kimi/MiniMax target and Chat API form')
    row = rows[0]
    for field in ('profile_id', 'interface_id', 'contract_id', 'test_binding_id', 'parameter_test_binding_id'):
        if payload.get(field) is not None and payload[field] != row['selection'][field]:
            raise ValueError('Selected Kimi/MiniMax reference identity conflicts with ' + field)
    exact = {'reference_contract_id': row['selection']['contract_id'], 'endpoint': row['endpoint'],
             'base_url': controls.PROVIDERS[row['provider_id']]['base_url'],
             'route_profile': 'vendor_direct', 'transport': 'chat_completions', 'modality': 'text', 'auth_mode': 'bearer'}
    for field, expected in exact.items():
        if payload.get(field) is not None and payload[field] != expected:
            raise ValueError('Selected Kimi/MiniMax execution identity conflicts with ' + field)
    return row


def _json_target(row):
    if row['target_key'] != json_controls.TARGET:
        raise ValueError('The JSON stream fixture is scoped only to exact MiniMax M2.5')
    value = copy.deepcopy(row)
    value['baseline_body']['messages'] = [{'role': 'user', 'content': json_controls.PROMPT}]
    value['baseline_body_sha256'] = controls.digest(value['baseline_body'])
    value['expectation'] = copy.deepcopy(json_controls.EXPECTATION)
    return value


def _cases(row, variant, binding_sha256):
    if variant not in {'all', 'stream', 'json'}:
        raise ValueError('Unknown Kimi/MiniMax workflow variant')
    groups = []
    if variant != 'json':
        regular = copy.deepcopy(row)
        regular['requires_new_nonstream_bytes_baseline'] = True
        groups.append((regular, False))
    if variant == 'json' or (variant == 'all' and row['target_key'] == json_controls.TARGET):
        groups.append((_json_target(row), True))
    result = []
    for target, is_json in groups:
        for label in ('nonstream', *controls.LABELS):
            case = controls.case_for(target, label, {})
            case['snapshot_sha256'] = binding_sha256
            if is_json:
                case['case_id'] = json_controls.NAMESPACE + '/' + label
            elif label == 'nonstream' and not row['requires_new_nonstream_bytes_baseline']:
                case['case_id'] = row['target_key'] + '/fresh_nonstream'
            result.append((case, 'json' if is_json else 'stream'))
    return result


def _current_case(context, inputs):
    row = _select({'target_key': context.target['target_key']})
    expected = next((case for case, _ in _cases(row, context.target['variant'], context.target['binding_sha256'])
                     if case['case_id'] == inputs['case'].get('case_id')), None)
    if expected is None or controls.canonical_bytes(expected) != controls.canonical_bytes(inputs['case']):
        raise ValueError('Stream control differs from its exact public source fixture')
    execution = context.target['execution_target']
    if (expected['provider_id'] != execution['provider_id'] or expected['model'] != execution['request_model_id']
            or expected['source_id'] != context.target['source_id'] or expected['url'] != context.target['endpoint']
            or execution['api_form'] != controls.FORM):
        raise ValueError('Stream control execution identity differs from its frozen source')
    return expected


def observe_control(context, inputs):
    case = _current_case(context, inputs)
    if any(result.get('provider_fatal') or result.get('error') for result in context.results.values()):
        return {'status': 'blocked', 'accepted': False, 'reason': 'terminal_provider_condition'}
    receipt = context.dispatch({'method': 'POST', 'path': '/chat/completions', 'body': case['body'],
                                'capture_raw': True, 'response_byte_limit': controls.MAX_BYTES})
    record = {'status_code': receipt.get('http_status'), 'content_type': receipt.get('content_type', ''),
              'response_complete': receipt.get('response_complete') is True, 'failure_type': receipt.get('error_type'),
              'response_json': None, 'response_bytes': b''}
    frames = []
    captured_sha = receipt.get('captured_bytes_sha256', receipt.get('response_bytes_sha256'))
    raw_redacted = receipt.get('raw_redacted', False)
    raw_hash_verified = False
    request_verified = receipt.get('request_bytes_sha256') == case['body_sha256']
    if not request_verified:
        record.update(failure_type='RequestIntegrityMismatch')
    try:
        raw = base64.b64decode(receipt['raw_base64'], validate=True)
        if (not raw or len(raw) > controls.MAX_BYTES
                or hashlib.sha256(raw).hexdigest() != captured_sha
                or type(raw_redacted) is not bool
                or (not raw_redacted and captured_sha != receipt.get('response_bytes_sha256'))
                or ('captured_byte_length' in receipt and (
                    type(receipt['captured_byte_length']) is not int or receipt['captured_byte_length'] != len(raw)))):
            raise ValueError('Raw response receipt hash or byte allowance mismatch')
        raw_hash_verified = True
        record['response_bytes'] = raw
        if record['content_type'].split(';', 1)[0].strip().lower() == 'text/event-stream':
            frames = evidence.parse_frames(raw)
        else:
            record['response_json'] = evidence.strict_json(raw)
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        record.update(failure_type=record['failure_type'] or type(exc).__name__)
    positive = inputs.get('positive_control') or {}
    positive_verified = positive.get('accepted') is True and positive.get('control_verified') is True
    observation = controls.observe(case, record, positive_control=positive_verified)
    native = observation.get('semantic_observation') or {}
    baseline_verified = case['label'] == 'nonstream' and native.get('pass') is True and observation['usage_verified'] is True
    control_verified = observation['stream_semantic_verified'] is True
    if case['label'] == 'usage_on':
        control_verified = control_verified and observation.get('documented_usage_true_reported') is True
    negative = case['label'].startswith('invalid_')
    rejected = observation['field_type_rejection_verified'] is True
    accepted_observation = observation['outcome'] == 'nonboolean_value_accepted_with_verified_native_semantics'
    status = ('passed' if baseline_verified else 'failed') if case['label'] == 'nonstream' else (
        'passed' if rejected else 'inconclusive' if accepted_observation else 'failed') if negative else (
        'passed' if control_verified else 'failed')
    provider_fatal = (record['status_code'] in FATAL_HTTP_CODES or controls.terminal_error(record['response_json'])
                      or any(controls.terminal_error(frame.get('data')) for frame in frames)
                      or record['failure_type'] is not None)
    result = {'status': status, 'accepted': status == 'passed' and not negative and record['status_code'] == 200,
              'baseline_verified': baseline_verified, 'control_verified': control_verified and not negative,
              'provider_fatal': provider_fatal, 'observation': observation,
              'parameter_outcome': observation['outcome'],
              'parameter_support_verified': False, 'full_parameter_matrix_verified': False,
              'certified_route_contract_pass': False, 'historical_baseline_replayed': False,
              'baseline_provenance': 'fresh_request_in_this_run',
              'request_body_sha256': case['body_sha256'], 'response_bytes_sha256': receipt.get('response_bytes_sha256'),
              'request_bytes_sha256': receipt.get('request_bytes_sha256'), 'request_integrity_verified': request_verified,
              'captured_bytes_sha256': captured_sha, 'raw_redacted': raw_redacted,
              'captured_bytes_verified': raw_hash_verified,
              'original_entity_byte_parity_verified': raw_hash_verified and record['response_complete']
                                                     and not raw_redacted and captured_sha == receipt.get('response_bytes_sha256'),
              'response_body_observation_scope': 'credential_redacted_http_entity' if raw_redacted else 'decoded_http_entity',
              'failure_type': record['failure_type'],
              'http_status': record['status_code'], 'response_complete': record['response_complete'],
              'case_id': case['case_id'], 'target_key': case['target_key'], 'label': case['label'],
              'fixture_variant': 'json' if case['case_id'].startswith(json_controls.NAMESPACE + '/') else 'stream'}
    context.record_event('stream_control_observation', result)
    return result


def register_handlers(registry):
    registry.register('kimi_minimax.control', observe_control, version=FACTORY_VERSION, digest=execution_digest)


def build_workflow(config, payload, *, target_key=None, variant='all', catalog=None):
    row = _select(payload, target_key)
    if payload.get('variant') is not None:
        if variant != 'all' and variant != payload['variant']:
            raise ValueError('Conflicting workflow variant selectors')
        variant = payload['variant']
    snapshot = controls.binding(row, catalog, config)
    binding_sha = controls.digest(snapshot)
    entity_snapshot = copy.deepcopy(snapshot)
    for field in controls.GLOBAL_SNAPSHOT_FIELDS:
        entity_snapshot['resolved'].pop(field, None)
    rows = _cases(row, variant, binding_sha)
    target = {key: row['selection'][key] for key in ('profile_id', 'interface_id', 'contract_id', 'test_binding_id', 'parameter_test_binding_id')}
    target.update(source_id=row['source_id'], modality='text', target_key=row['target_key'], variant=variant,
                  base_url=controls.PROVIDERS[row['provider_id']]['base_url'], endpoint=row['endpoint'],
                  binding_sha256=binding_sha, execution_target={'provider_id': row['provider_id'], 'request_model_id': row['request_model_id'],
                     'api_form': controls.FORM, 'transport_adapter_id': 'chat_completions'})
    target['reference_entity_sha256'] = controls.digest(entity_snapshot)
    cases, steps = [], []
    for case, group in rows:
        by_label = {value['label']: value['case_id'] for value, candidate_group in rows if candidate_group == group}
        dependencies = [] if case['label'] == 'nonstream' else [by_label['nonstream']]
        requirements = [{'step': key, 'path': ['baseline_verified'], 'equals': True} for key in dependencies]
        inputs = {'case': case}
        if case['label'].startswith('invalid_'):
            positive = by_label['usage_on' if case['label'] == 'invalid_usage_type' else 'stream_default']
            dependencies.append(positive)
            requirements.append({'step': positive, 'path': ['control_verified'], 'equals': True})
            inputs['positive_control'] = {'$ref': {'step': positive, 'path': [], 'type': 'object'}}
        requirements.extend({'step': key, 'path': ['accepted'], 'equals': True} for key in dependencies)
        cases.append({'id': case['case_id'], 'name': case['case_id'], 'phase': group,
                      'fresh_prerequisite': case['case_id'].endswith('/fresh_nonstream')})
        steps.append({'id': case['case_id'], 'case_id': case['case_id'], 'handler': 'kimi_minimax.control',
                      'inputs': inputs, 'depends_on': dependencies, 'requires': requirements})
    return {'workflow_schema_version': 1, 'id': f'kimi-minimax/{row["target_key"]}/{variant}', 'target': target,
            'cases': cases, 'steps': steps,
            'limits': {'max_requests': len(steps), 'request_timeout_seconds': controls.TIMEOUT,
                       'deadline_seconds': len(steps) * controls.TIMEOUT, 'cleanup_max_requests': 0, 'cleanup_deadline_seconds': 1},
            'policy': {'fatal_status_codes': FATAL_HTTP_CODES},
            'factory': {'factory_id': FACTORY_ID, 'version': FACTORY_VERSION, 'source_sha256': source_digest()},
            'factory_arguments': {'target_key': row['target_key'], 'variant': variant},
            'evidence_scope': {'prior_reference_approval_code': controls.POLICY['approval_code'],
                               'request_reference': {'path': controls.REFERENCE, 'sha256': controls.REFERENCE_SHA256},
                               'prior_stream_package_request_cap': controls.REQUEST_CAP,
                               'fresh_workflow_request_cap': len(steps),
                               'execution_permission_scope': 'current_explicit_exact_target_workflow',
                               'historical_raw_reports_read': False, 'historical_baseline_replayed': False,
                               'native_type_acceptance_is_certification': False, 'full_parameter_matrix_verified': False,
                               'private_reference_paths_exported': False, 'new_prerequisites_are_declared': True}}


def prepare_kimi_minimax_plan(config, payload, *, target_key=None, variant='all', catalog=None):
    workflow = build_workflow(config, payload, target_key=target_key, variant=variant, catalog=catalog)
    registry = HandlerRegistry()
    register_handlers(registry)
    selected = payload.get('cases')
    if selected is not None:
        known = {case['id'] for case in workflow['cases']}
        if not isinstance(selected, list) or not selected or any(key not in known for key in selected):
            raise ValueError('Controls require exact selected case IDs from the declared suite')
    return compile_plan(workflow, registry, selected_cases=selected,
                        run_count=payload.get('runs', payload.get('run_count', 1))), registry


def registry_for_kimi_minimax_plan(plan):
    if plan.get('definition', {}).get('factory') != {
        'factory_id': FACTORY_ID, 'version': FACTORY_VERSION, 'source_sha256': source_digest(),
    }:
        raise ValueError('Not the current registered Kimi/MiniMax workflow factory')
    registry = HandlerRegistry()
    register_handlers(registry)
    validate_plan(plan, registry)
    return registry


def usage_comparisons(observations):
    """Keep text and JSON controls separate and never infer effects from counts."""
    groups = sorted({(row['target_key'], row['fixture_variant']) for row in observations})
    result = []
    for target_key, variant in groups:
        source_rows = [{'target_key': row['target_key'], 'label': row['label'], 'verdict': row['observation']}
                       for row in observations if row['target_key'] == target_key and row['fixture_variant'] == variant]
        comparison = next(row for row in controls.usage_comparisons({}, source_rows) if row['target_key'] == target_key)
        result.append({**comparison, 'fixture_variant': variant})
    return result


def make_kimi_minimax_dispatcher(config, plan, **kwargs):
    row = _select({'target_key': plan['target']['target_key']})
    current = controls.binding(row, config=config)
    if controls.digest(current) != plan['target']['binding_sha256']:
        raise ValueError('Kimi/MiniMax reference binding drifted before credential access')
    return HttpDispatcher(config, row['provider_id'], 'chat_completions', response_byte_limit=controls.MAX_BYTES, **kwargs)


def build_shared_package(config=None, *, target_keys=None, variant='all'):
    config = public_config() if config is None else config
    selected = list(controls.TARGETS) if target_keys is None else list(target_keys)
    if not selected or len(selected) != len(set(selected)) or set(selected) - set(controls.TARGETS):
        raise ValueError('Shared package targets must be distinct reviewed exact target keys')
    plans = [prepare_kimi_minimax_plan(config, {}, target_key=key, variant=variant)[0] for key in selected]
    if any(plan['definition']['factory']['source_sha256'] != source_digest() for plan in plans):
        raise ValueError('Stream workflow source changed during preparation')
    return {'workflow_batch_schema_version': 1, 'factory_id': FACTORY_ID, 'target_keys': selected, 'variant': variant,
            'plans': plans, 'planned_requests': sum(plan['limits']['max_requests'] for plan in plans),
            'historical_requests_resumed': False, 'historical_baselines_replayed': False,
            'private_report_dependencies': False, 'parameter_support_certified': False}


def prepare_shared_batch(*, root=None, config=None, target_keys=None, variant='all'):
    from lib.report_retention import DEFAULT_REPORT_ROOT, initialize_report_retention
    from scripts.prepare_zai_general_reference import Reports
    root = Path(root or DEFAULT_REPORT_ROOT).absolute()
    package = build_shared_package(config, target_keys=target_keys, variant=variant)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    batch = root / ('kimi_minimax_workflow_' + timestamp + '_' + uuid.uuid4().hex[:8])
    initialize_report_retention(batch, root=root)
    reports = Reports(batch, root=root)
    reports.json('request_package.json', package)
    reports.json(SHARED_MARKER, {'request_package_sha256': controls.digest(package), 'factory_id': FACTORY_ID})
    return batch, package


def execute_shared_batch(batch, *, root=None, config=None, credential_factory=None, dispatcher_factory=None):
    from lib.report_retention import DEFAULT_REPORT_ROOT, register_report_files
    from scripts.prepare_zai_general_reference import Reports, read_frozen_package
    root = Path(root or DEFAULT_REPORT_ROOT).absolute()
    batch = Path(batch).absolute()
    if not (batch / SHARED_MARKER).is_file() or (batch / SHARED_MARKER).is_symlink():
        raise ValueError('Historical packages cannot execute through the shared CLI; prepare a new public-source batch')
    package = read_frozen_package(batch, root=root)
    config = public_config() if config is None else config
    expected = build_shared_package(config, target_keys=package.get('target_keys'), variant=package.get('variant'))
    if controls.canonical_bytes(package) != controls.canonical_bytes(expected):
        raise ValueError('Shared package code, reference identity, body or policy drifted')
    marker = json.loads((batch / SHARED_MARKER).read_text(encoding='utf-8'))
    if marker != {'request_package_sha256': controls.digest(package), 'factory_id': FACTORY_ID}:
        raise ValueError('Shared workflow sidecar does not match its package')
    registries = [registry_for_kimi_minimax_plan(plan) for plan in package['plans']]
    reports = Reports(batch, root=root)
    reports.require_fresh()
    if (batch / 'workflow').exists():
        raise ValueError('Shared workflow business execution cannot resume')
    reports.json('dispatch_started.json', {'request_package_sha256': controls.digest(package), 'planned_requests': package['planned_requests']}, claim=True)
    factory = credential_factory or controls.credential
    cancellation = CancellationContext()
    results, observations = [], []
    stopped = False
    with cancellation.install_signal_handlers():
        for plan, registry in zip(package['plans'], registries):
            provider = plan['target']['execution_target']['provider_id']
            # HttpDispatcher defers this designated source credential until send.
            dispatch = (dispatcher_factory(config, plan) if dispatcher_factory else
                        make_kimi_minimax_dispatcher(config, plan, credential_factory=lambda cfg, selected: factory(selected)))
            report = execute_plan(plan, registry, dispatch, cancellation=cancellation,
                                  evidence_dir=batch / 'workflow' / plan['target']['target_key'])
            results.append(report)
            observations.extend(step for run in report['runs'] for step in run['steps'].values() if 'observation' in step)
            fatal = any(run.get('fatal_reason') or any(
                step.get('provider_fatal') or step.get('error') for step in run['steps'].values()
            ) for run in report['runs'])
            if cancellation.cancelled or fatal:
                stopped = True
                break
    sent = sum(run['request_count'] for result in results for run in result['runs'])
    result = {'requests_sent': sent, 'planned_requests': package['planned_requests'],
              'all_planned_requests_dispatched': sent == package['planned_requests'],
              'terminal_state': 'stopped_early' if stopped else 'completed' if sent == package['planned_requests'] else 'incomplete',
              'dispatch_attempts': sent,
              'sent_count_semantics': 'shared dispatch attempts; credential or transport failure can precede HTTP client entry',
              'pass': bool(results) and all(item['status'] == 'passed' for item in results),
              'stream_semantics_verified': sum(item['observation']['stream_semantic_verified'] for item in observations),
              'field_type_rejections_verified': sum(item['observation']['field_type_rejection_verified'] for item in observations),
              'usage_controls': usage_comparisons(observations),
              'historical_baseline_replayed': False, 'full_parameter_matrix_verified': False,
              'observations': observations, 'workflow_reports': results}
    reports.json('summary.json', result)
    reports.json('dispatch_finished.json', {'requests_sent': sent, 'terminal_state': result['terminal_state']})
    register_report_files(batch, [path.relative_to(batch) for path in (batch / 'workflow').rglob('*') if path.is_file()], root=root)
    return result


def refresh_shared_batch(batch, *, root=None, config=None):
    """Re-freeze only an undispatched shared package after catalog metadata drift."""
    from lib.report_retention import DEFAULT_REPORT_ROOT
    from scripts.prepare_zai_general_reference import Reports, read_frozen_package
    root = Path(root or DEFAULT_REPORT_ROOT).absolute()
    batch = Path(batch).absolute()
    if not (batch / SHARED_MARKER).is_file():
        raise ValueError('Historical packages require an explicit new preparation, not shared refresh')
    old = read_frozen_package(batch, root=root)
    Reports(batch, root=root).require_fresh()
    if (batch / 'workflow').exists():
        raise ValueError('Dispatched shared packages cannot refresh or resume')
    config = public_config() if config is None else config
    current = build_shared_package(config, target_keys=old['target_keys'], variant=old['variant'])
    for previous, proposed in zip(old['plans'], current['plans']):
        if (previous['engine_digest'] != proposed['engine_digest']
                or previous['definition']['factory'] != proposed['definition']['factory']
                or previous['target']['reference_entity_sha256'] != proposed['target']['reference_entity_sha256']):
            raise ValueError('Refresh cannot change source code, reference entities or wire controls')
    if len(old['plans']) != len(current['plans']):
        raise ValueError('Refresh target scope changed')
    return prepare_shared_batch(root=root, config=config, target_keys=old['target_keys'], variant=old['variant'])
