"""The existing two probes, four prefix controls and two stop controls, frozen.

The older Pro-only five-observation suite remains a distinct fixed-source adapter.
Every HTTP exchange here passes through RunContext; validators are reused from
the original source-specific probes and controlled-comparison runners.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

import requests
import yaml

from lib import deepseek_prefix_live as probe
from lib.deepseek_beta_prefix import DEEPSEEK_BETA_PREFIX_URL
from lib.credential_security import ProviderCredential
from lib.live_stateful_runners import _response_json
from lib.model_profile_catalog import get_model_profile_catalog
from lib.test_runner import CancellationContext, HandlerRegistry, IntegrityError, compile_plan, execute_plan, validate_plan
from lib.test_runner.cancellation import response_deadline
from lib.test_runner.common import digest_json
from scripts import run_deepseek_prefix_approved_matrix as matrix
from scripts import run_deepseek_prefix_stop_followup as stop

ROOT = Path(__file__).resolve().parents[3]
FACTORY_ID = 'deepseek_beta_prefix'
FACTORY_VERSION = '1'
OBSERVE = 'deepseek.beta.observe.v1'
ASSERT = 'deepseek.beta.assert.v1'
_monotonic = time.monotonic
CASE_IDS = (probe.POSITIVE_CASE, probe.NEGATIVE_CASE, *matrix.CASE_IDS, *stop.CASE_IDS)
SOURCE_FILES = ('lib/test_runner/adapters/deepseek_beta.py', 'lib/deepseek_prefix_live.py', 'lib/deepseek_beta_prefix.py',
                'scripts/deepseek_prefix_execution_candidate.py', 'scripts/deepseek_prefix_stop_followup_candidate.py',
                'scripts/run_deepseek_prefix_approved_matrix.py', 'scripts/run_deepseek_prefix_stop_followup.py')


def source_digest():
    return digest_json({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES})


def execution_digest():
    return digest_json({'factory': source_digest(), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                       for name in ('lib/config.py', 'lib/credential_security.py', 'lib/live_stateful_runners.py', 'lib/model_profile_catalog.py')}})


def public_config():
    """Dedicated legacy runners' existing exact two-model official scope.

    Ordinary Web/CLI preparation passes its actual configuration explicitly;
    this snapshot does not add a provider route to that public configuration.
    """
    config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    provider = config['providers']['deepseek_official']
    provider.setdefault('models', {})['candidates'] = [pair[0] for pair in probe.TARGETS.values()]
    return config


def _binding(resolved):
    fields = ('source_id', 'profile_id', 'interface_id', 'contract_id', 'test_binding_id',
              'parameter_test_binding_id', 'catalog_digest', 'test_extension_digest')
    return {**{field: resolved[field] for field in fields}, 'api_form': 'deepseek_beta_chat_prefix', 'api_version': 'beta'}


def _source_cases(catalog, model, groups):
    matches = [interface for interface, pair in probe.TARGETS.items() if pair[0] == model]
    if len(matches) != 1:
        raise ValueError('Beta workflow requires one exact official Pro or Flash model')
    interface = matches[0]
    cases = []
    resolved = None
    if 'probe' in groups:
        for name in (probe.POSITIVE_CASE, probe.NEGATIVE_CASE):
            resolved, case = probe._bound_case(catalog, interface, model, name)
            probe._prepare(case, model)
            cases.append(copy.deepcopy(case))
    if 'matrix' in groups:
        resolved, rows = matrix.resolve_cases(catalog, interface)
        cases.extend(copy.deepcopy(rows))
    if 'stop' in groups:
        resolved, rows = stop.resolve_stop_cases(catalog, interface)
        cases.extend(copy.deepcopy(rows))
    if resolved is None:
        raise ValueError('Beta workflow has no selected source group')
    return resolved, cases


def _official_config(config, model):
    from lib.config import get_provider_config
    provider = get_provider_config(config, 'deepseek_official')
    if (str(provider.get('base_url', '')).rstrip('/') != 'https://api.deepseek.com'
            or provider.get('reference_source_id') != 'deepseek'):
        raise ValueError('Beta workflow requires the exact official provider source and origin')
    interfaces = provider.get('api_interfaces') or {}
    # Root previously exposed this source only through the dedicated runners.
    # Missing configuration uses that fixed source endpoint; an explicit entry
    # must agree and can never be silently repaired or re-enabled.
    interface = interfaces.get('deepseek-beta-chat-prefix', {'path': '/beta/chat/completions', 'auth': 'bearer'})
    if not isinstance(interface, dict):
        raise ValueError('Beta interface configuration must be an object')
    url = str(interface.get('base_url') or provider['base_url']).rstrip('/') + str(interface.get('path', ''))
    if (url != DEEPSEEK_BETA_PREFIX_URL or interface.get('auth') != 'bearer'
            or interface.get('enabled') is False or interface.get('disabled_reason')):
        raise ValueError('Beta workflow requires the exact configured official endpoint and bearer transport')
    if model not in (provider.get('models') or {}).get('candidates', []):
        raise ValueError('Beta workflow model is absent from its official provider')


def _ref(step, path):
    return {'$result_ref': {'step': step, 'path': path, 'type': 'object'}}


def build_definition(config, model, *, catalog=None, groups=('probe', 'matrix', 'stop'), timeout_sec=120):
    catalog = catalog or get_model_profile_catalog()
    if not isinstance(groups, (list, tuple)) or not groups or set(groups) - {'probe', 'matrix', 'stop'} or len(groups) != len(set(groups)):
        raise ValueError('Unrecognized or duplicated beta source groups')
    if type(timeout_sec) not in (int, float) or not 1 <= timeout_sec <= 120:
        raise ValueError('Beta response deadline must be in [1, 120] seconds')
    _official_config(config, model)
    resolved, cases = _source_cases(catalog, model, groups)
    binding = _binding(resolved)
    rows, steps = [], []
    for case in cases:
        name = case['case_id']
        negative = name in {probe.NEGATIVE_CASE, matrix.CASE_IDS[-1]}
        observation = name + '.observe'
        rows.append({'id': name, 'group': 'probe' if name in (probe.POSITIVE_CASE, probe.NEGATIVE_CASE) else 'matrix' if name in matrix.CASE_IDS else 'stop'})
        item = {'case_id': name, 'model_id': model, 'target_binding': binding, 'body': case['body'],
                'request_sha256': hashlib.sha256(matrix.canonical(case['body'])).hexdigest()}
        inputs = {'item': item}
        if name == matrix.CASE_IDS[-1]:
            inputs['positive'] = _ref(matrix.CASE_IDS[1] + '.observe', ['record'])
        prerequisites = []
        if name == matrix.CASE_IDS[2]:
            prerequisites = [matrix.CASE_IDS[1] + '.observe']
        elif name == stop.CASE_IDS[1]:
            prerequisites = [stop.CASE_IDS[0] + '.observe']
        steps.append({'id': observation, 'case_id': name, 'handler': OBSERVE, 'inputs': inputs,
                      'depends_on': prerequisites, 'request_cap': 1, 'cleanup_request_cap': 0})
        if not negative:
            inputs = {'case_id': name, 'record': _ref(observation, ['record'])}
            if name == matrix.CASE_IDS[1]:
                inputs['control'] = _ref(matrix.CASE_IDS[0] + '.observe', ['record'])
            elif name == matrix.CASE_IDS[2]:
                inputs['positive'] = _ref(matrix.CASE_IDS[1] + '.observe', ['record'])
            elif name == stop.CASE_IDS[1]:
                inputs['baseline'] = _ref(stop.CASE_IDS[0] + '.observe', ['record'])
            steps.append({'id': name, 'case_id': name, 'handler': ASSERT, 'inputs': inputs, 'request_cap': 0, 'cleanup_request_cap': 0})
    return {'workflow_schema_version': 1, 'id': 'deepseek-beta/' + model,
            'target': {'source_id': 'deepseek', 'profile_id': resolved['profile_id'], 'interface_id': resolved['interface_id'],
                       'contract_id': resolved['contract_id'], 'endpoint': DEEPSEEK_BETA_PREFIX_URL, 'api_version': 'beta',
                       'execution_target': {'provider_id': 'deepseek_official', 'request_model_id': model, 'route_profile': 'vendor_direct',
                                            'api_form': 'deepseek_beta_chat_prefix', 'transport_adapter_id': 'deepseek-beta-chat-prefix'}},
            'factory': {'factory_id': FACTORY_ID, 'version': FACTORY_VERSION, 'source_sha256': source_digest()},
            'factory_arguments': {'model': model, 'groups': list(groups), 'timeout_sec': timeout_sec},
            'source_binding': binding, 'source_cases': cases, 'cases': rows, 'steps': steps,
            'limits': {'max_requests': len(cases), 'request_timeout_seconds': timeout_sec,
                       'deadline_seconds': len(cases) * timeout_sec, 'cleanup_max_requests': 0},
            'policy': {'fatal_status_codes': [401, 402, 403, 404, 408, 429, *range(500, 600)]}}


def register_handlers(registry):
    registry.register(OBSERVE, observe, version=FACTORY_VERSION, digest=execution_digest)
    registry.register(ASSERT, assert_semantics, version=FACTORY_VERSION, digest=execution_digest)
    return registry


def prepare_beta_plan(config, model, *, catalog=None, selected_cases=None, runs=1, groups=('probe', 'matrix', 'stop'), timeout_sec=120):
    definition = build_definition(config, model, catalog=catalog, groups=groups, timeout_sec=timeout_sec)
    registry = register_handlers(HandlerRegistry())
    plan = compile_plan(definition, registry, selected_cases=selected_cases, run_count=runs)
    definition['limits']['max_requests'] = sum(step.get('request_cap', 0) for step in plan['ordered_steps'])
    definition['limits']['deadline_seconds'] = max(1, definition['limits']['max_requests']) * timeout_sec
    return compile_plan(definition, registry, selected_cases=selected_cases, run_count=runs), registry


def registry_for_beta_plan(plan, *, config=None, catalog=None):
    """Recreate the exact public source plan before loading any credential."""
    frozen = validate_plan(plan)
    args = frozen['definition']['factory_arguments']
    current, registry = prepare_beta_plan(config or public_config(), args['model'], catalog=catalog,
        selected_cases=frozen['requested_cases'], runs=frozen['run_count'], groups=args['groups'], timeout_sec=args['timeout_sec'])
    if current != frozen:
        raise IntegrityError('Current Beta source/selection differs from the frozen workflow')
    return registry


def _probe_observation(item, receipt):
    """Preserve the original one-probe public verdict, including unknown semantics."""
    name, model, binding = item['case_id'], item['model_id'], item['target_binding']
    case = {'case_id': name, 'body': item['body']}
    _, request = probe._prepare(case, model)
    result = {'approval_code': probe.APPROVAL_CODE, 'source_id': 'deepseek', 'interface_id': binding['interface_id'],
        'api_form': 'deepseek_beta_chat_prefix', 'api_version': 'beta', 'contract_id': binding['contract_id'],
        'model_policy_binding_id': binding['test_binding_id'], 'parameter_binding_id': binding['parameter_test_binding_id'],
        'catalog_digest': binding['catalog_digest'], 'test_extension_digest': binding['test_extension_digest'],
        'request_model_id': model, 'case_id': name, 'request_sha256': item['request_sha256'], 'requests_sent': 1,
        'max_output_tokens': item['body']['max_tokens'], 'success': False, 'response_valid': False,
        'semantic_proof': 'not_established', 'all_models_prefix_proof': False}
    if receipt.get('failure'):
        return {**result, 'failure': receipt['failure']}
    status, envelope = receipt.get('status_code'), receipt.get('response')
    if type(status) is not int or status not in {200, 400, 422}:
        result['failure'] = 'http_error'
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
        return result
    result['http_status'] = status
    try:
        if name == probe.NEGATIVE_CASE:
            error = envelope.get('error')
            if (status not in {400, 422} or set(envelope) != {'error'} or not isinstance(error, dict)
                    or error.get('type') not in {'invalid_request_error', 'validation_error'}
                    or error.get('param') not in {'messages[].prefix', 'messages[1].prefix', 'messages.1.prefix', 'messages[-1].prefix'}):
                result['failure'] = 'negative_rejection_not_attributed'
            else:
                result.update(success=True, response_valid=True, case_observation='attributed_rejection_observed',
                              rejected_parameter='messages[].prefix', semantic_proof='not_applicable_negative_case')
        elif status == 200:
            result.update(probe._positive_observation(request, envelope, item['body']['max_tokens']))
        else:
            result['failure'] = 'positive_case_http_error'
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
        result['failure'] = 'invalid_response_envelope'
    return result


def observe(context, inputs):
    item = inputs['item']
    receipt = context.dispatch({'method': 'POST', 'url': DEEPSEEK_BETA_PREFIX_URL, 'body': item['body'],
                                'case_id': item['case_id'], 'request_sha256': item['request_sha256']})
    record = {'case_id': item['case_id'], 'model_id': item['model_id'], 'target_binding': item['target_binding'],
              'request': item['body'], 'request_sha256': item['request_sha256'], **receipt}
    negative = item['case_id'] in {probe.NEGATIVE_CASE, matrix.CASE_IDS[-1]}
    valid = False
    if item['case_id'] in {probe.POSITIVE_CASE, probe.NEGATIVE_CASE}:
        record['public_result'] = _probe_observation(item, receipt)
        valid = record['public_result']['response_valid']
    elif not receipt.get('failure') and receipt.get('status_code') == 200 and not negative:
        try:
            record['generation'] = matrix.validate_generation(item['body'], receipt['response'])
            valid = True
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            record['validation_error'] = str(exc)
    elif negative and not receipt.get('failure') and 'positive' in inputs:
        record['negative_type_attribution'] = matrix.negative_attribution(inputs['positive'], record)
        valid = record['negative_type_attribution']['attributed']
    if item['case_id'] in stop.CASE_IDS and record.get('generation'):
        record['generation'] = {name: record['generation'][name] for name in
                               ('returned_model_id', 'finish_reason', 'usage', 'content', 'response_valid')}
    return {'status': 'passed' if valid else 'failed', 'accepted': bool(valid and not negative), 'record': record,
            'envelope_completeness': {'status': 'complete' if receipt.get('response_complete') else 'incomplete'},
            'identity_token_audit': {'response_valid': bool(valid)},
            'semantic_effect': {'status': 'not_applicable_negative' if negative and valid else 'separate_assertion'}}


def assert_semantics(context, inputs):
    name, record = inputs['case_id'], inputs['record']
    if name == probe.POSITIVE_CASE:
        result = record['public_result']
        return {'status': 'inconclusive', 'accepted': False, 'public_result': result,
                'semantic_effect': {'status': result['semantic_proof'], 'reason': result.get('semantic_limit')}}
    generation = record.get('generation', {})
    effect = {}
    if name == matrix.CASE_IDS[0]:
        passed = bool(generation.get('response_alone_matches_control_json') and not generation.get('response_alone_matches_json'))
        effect = {'control_exact_json': passed}
    elif name in {matrix.CASE_IDS[1], matrix.CASE_IDS[2]}:
        records = [value for key, value in inputs.items() if key in {'record', 'positive', 'control'}]
        package = {'catalog_digest': context._plan['definition']['source_binding']['catalog_digest'],
                   'test_extension_digest': context._plan['definition']['source_binding']['test_extension_digest']}
        model = record['model_id']
        effect = matrix.summarize(package, records)['models'][model]
        passed = effect['prefix_effect_observed'] if name == matrix.CASE_IDS[1] else effect['stop_effect_observed']
    elif name == stop.CASE_IDS[0]:
        text = generation.get('content', '')
        passed = stop.STOP_LITERAL in text and (stop.exact_target(text) or stop.exact_target(stop.PREFIX + text))
        effect = {'baseline_exact_json_and_literal': passed}
    else:
        effect = stop.judge_pair(inputs['baseline'], record)
        passed = effect['stop_effect_observed']
    return {'status': 'passed' if passed else 'failed', 'accepted': bool(passed), 'semantic_effect': effect}


class BetaDispatcher:
    def __init__(self, plan, *, api_key, request=None, catalog=None):
        self.plan = validate_plan(plan)
        if not isinstance(api_key, str) or not api_key or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
            raise ValueError('An in-memory official key without whitespace is required')
        self.credential = ProviderCredential.create(provider='deepseek_official', secret=api_key, base_urls=[DEEPSEEK_BETA_PREFIX_URL])
        self.request = request
        self.catalog = catalog
        self._redact = lambda value: matrix._redact(value, api_key)
        self.session = None
        self.interruption = None

    def close(self):
        if self.session is not None:
            session = self.session
            self.session = None
            session.close()

    def __call__(self, request, *, timeout, context):
        receipt = {'status_code': None, 'response_complete': False}
        try:
            with response_deadline(timeout):
                try:
                    self._validate_request(request, context)
                except (ValueError, KeyError, TypeError, IndexError):
                    raise IntegrityError('Current Beta catalog/request no longer admits the exact frozen source') from None
                send = self.request
                if send is None:
                    self.session = requests.Session()
                    self.session.trust_env = False
                    self.session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
                    send = self.session.request
                self._exchange(send, matrix.canonical(request['body']), timeout, receipt)
        except TimeoutError:
            receipt.update(failure='response_deadline', response_complete=False)
        except BaseException as exc:
            if not isinstance(exc, Exception) and self.interruption is None:
                self.interruption = exc
            raise
        return receipt

    def _validate_request(self, request, context):
        if context.target != self.plan['target'] or request.get('url') != DEEPSEEK_BETA_PREFIX_URL or request.get('method') != 'POST':
            raise IntegrityError('Beta dispatch changed its exact model/source/endpoint')
        expected = next((case for case in self.plan['definition']['source_cases'] if case['case_id'] == request.get('case_id')), None)
        body = matrix.canonical(request.get('body'))
        if expected is None or body != matrix.canonical(expected['body']) or hashlib.sha256(body).hexdigest() != request.get('request_sha256'):
            raise IntegrityError('Beta dispatch changed a frozen request body')
        catalog = self.catalog or get_model_profile_catalog()
        model = self.plan['definition']['factory_arguments']['model']
        interface = self.plan['definition']['source_binding']['interface_id']
        name = request['case_id']
        if name in (probe.POSITIVE_CASE, probe.NEGATIVE_CASE):
            resolved, case = probe._bound_case(catalog, interface, model, name)
            cases = [case]
        elif name in matrix.CASE_IDS:
            resolved, cases = matrix.resolve_cases(catalog, interface)
        else:
            resolved, cases = stop.resolve_stop_cases(catalog, interface)
        current = next((case for case in cases if case['case_id'] == name), None)
        if (matrix.canonical(current) != matrix.canonical(expected)
                or matrix.canonical(_binding(resolved)) != matrix.canonical(self.plan['definition']['source_binding'])):
            raise IntegrityError('Beta catalog/source changed after the frozen plan')

    def _exchange(self, send, body, timeout, receipt):
        response = None
        started = _monotonic()
        try:
            try:
                response = send('POST', DEEPSEEK_BETA_PREFIX_URL,
                    headers={**self.credential.auth_headers(url=DEEPSEEK_BETA_PREFIX_URL, auth_mode='bearer'), 'Content-Type': 'application/json'},
                    data=body, timeout=timeout, allow_redirects=False, stream=True)
            except TimeoutError:
                raise
            except Exception as exc:
                receipt.update(failure='transport_error', transport_or_read_error=type(exc).__name__)
                return
            receipt['status_code'] = response.status_code
            raw = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if _monotonic() - started >= timeout:
                        receipt['failure'] = 'response_deadline'
                        return
                    if not isinstance(chunk, bytes):
                        receipt['failure'] = 'invalid_response_chunk'
                        return
                    if len(raw) + len(chunk) > probe.MAX_RESPONSE_BYTES:
                        receipt['failure'] = 'response_size_limit'
                        return
                    raw.extend(chunk)
                if _monotonic() - started >= timeout:
                    receipt['failure'] = 'response_deadline'
                    return
            except TimeoutError:
                raise
            except Exception as exc:
                receipt.update(failure='response_read_error', transport_or_read_error=type(exc).__name__)
                return
            try:
                receipt['response'] = self._redact(_response_json(bytes(raw)))
            except (ValueError, UnicodeError):
                receipt['failure'] = 'invalid_response_envelope'
                return
            receipt.update(response_complete=True, response_raw=self._redact(bytes(raw).decode('utf-8')),
                           response_sha256_before_redaction=hashlib.sha256(raw).hexdigest())
        finally:
            try:
                if response is not None:
                    try:
                        response.close()
                    except TimeoutError:
                        raise
                    except Exception:
                        receipt.update(failure='response_close_error', response_complete=False)
                    except BaseException as exc:
                        self.interruption = self.interruption or exc
                        raise
            finally:
                try:
                    self.close()
                except TimeoutError:
                    raise
                except Exception:
                    receipt.update(failure='session_close_error', response_complete=False)
                except BaseException as exc:
                    self.interruption = self.interruption or exc
                    raise


def records_from_report(report):
    return [step['record'] for run in report['runs'] for step in run['steps'].values() if 'record' in step]


def execute_beta_plan(plan, registry, *, api_key, request=None, evidence_dir=None, cancellation=None, report_callback=None, catalog=None):
    validate_plan(plan, registry)
    dispatcher = BetaDispatcher(plan, api_key=api_key, request=request, catalog=catalog)
    cancel = cancellation or CancellationContext()
    try:
        with cancel.install_signal_handlers():
            report = execute_plan(plan, registry, dispatcher, cancellation=cancel, evidence_dir=evidence_dir)
    finally:
        dispatcher.close()
    if report_callback is not None:
        report_callback(report)
    if dispatcher.interruption is not None:
        raise dispatcher.interruption
    if cancel.cancelled:
        raise KeyboardInterrupt(cancel.reason)
    return report


def execute_bound_observation(item, *, group, api_key, catalog, request=None, timeout_sec=120):
    """Compatibility for one old sender: exactly one observed, counted request.

    A single sender cannot establish a paired causal result. Full matrix and
    follow-up entrypoints execute the dependency-bearing complete group plan.
    """
    definition = build_definition(public_config(), item['model_id'], catalog=catalog, groups=(group,), timeout_sec=timeout_sec)
    expected = next((step for step in definition['steps'] if step['id'] == item['case_id'] + '.observe'), None)
    if expected is None or matrix.canonical(expected['inputs']['item']) != matrix.canonical(item):
        raise ValueError('Request differs from its frozen exact Beta source binding')
    definition['cases'] = [row for row in definition['cases'] if row['id'] == item['case_id']]
    definition['steps'] = [{**expected, 'depends_on': [], 'inputs': {'item': item}}]
    definition['limits'].update(max_requests=1, deadline_seconds=timeout_sec)
    registry = register_handlers(HandlerRegistry())
    plan = compile_plan(definition, registry)
    report = execute_beta_plan(plan, registry, api_key=api_key, request=request, catalog=catalog)
    records = records_from_report(report)
    if len(records) != 1:
        raise RuntimeError('The single Beta observation did not produce its counted receipt')
    return records[0]


def freeze_legacy_package(package, group, *, catalog=None):
    """Freeze the old CLI's exact two-model scope into new executable plans."""
    if group not in ('matrix', 'stop'):
        raise ValueError('Only the two existing comparison CLI groups are supported')
    catalog = catalog or get_model_profile_catalog()
    prepared = copy.deepcopy(package)
    plans, items = [], []
    for model, _ in probe.TARGETS.values():
        plan, _registry = prepare_beta_plan(public_config(), model, catalog=catalog, groups=(group,))
        plans.append(plan)
        items.extend(step['inputs']['item'] for step in plan['ordered_steps'] if step['handler'] == OBSERVE)
    if matrix.canonical(items) != matrix.canonical(package['requests']):
        raise ValueError('Legacy request package differs from the exact new workflow scope')
    prepared['execution_plans'] = plans
    prepared['factory'] = plans[0]['definition']['factory']
    return prepared


def execute_legacy_package(package, group, *, api_key, request=None, evidence_dir=None, on_report=None, catalog=None):
    """Execute fresh packages only; historical archives cannot supply requests."""
    frozen = freeze_legacy_package(package, group, catalog=catalog)
    if frozen != package:
        raise IntegrityError('Legacy package is missing or changed its frozen execution plans')
    reports = []
    cancellation = CancellationContext()
    for plan in package['execution_plans']:
        registry = registry_for_beta_plan(plan, catalog=catalog)
        def completed(report):
            reports.append(report)
            if on_report is not None:
                on_report(report)
        report = execute_beta_plan(plan, registry, api_key=api_key, request=request, evidence_dir=evidence_dir,
                                   cancellation=cancellation, report_callback=completed, catalog=catalog)
        if cancellation.cancelled or any(run.get('fatal_reason') for run in report['runs']):
            break
    return reports
