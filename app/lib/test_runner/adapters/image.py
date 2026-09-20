"""Image protocols and domain checks on the common counted execution boundary."""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import io
import math
import os
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
import socket
import tempfile
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

from lib.config import IMAGE_TRANSPORT_INTERFACES, get_image_endpoint, get_provider_config, get_provider_interface, get_timeout_sec
from lib.image_validation import ImageTestCase
from lib import gpt_image_25_responses as responses
from .. import HandlerRegistry, compile_plan, execute_plan
from ..common import digest_json
from ..transport import HttpDispatcher, canonical_bytes

FACTORY_ID = 'image_parameter'
RESPONSES_FACTORY_ID = 'responses_image'
FACTORY_VERSION = '1'


def source_digest():
    """Portable registered factory entrypoint; runtime dependencies bind separately."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def execution_digest():
    root = Path(__file__).resolve().parents[3]
    names = ('lib/test_runner/adapters/image.py', 'scripts/image_param_test.py',
             'lib/test_runner/transport.py', 'lib/credential_security.py', 'lib/parameter_output_limit.py',
             'lib/image_validation.py', 'lib/gpt_image_25.py', 'lib/gpt_image_25_responses.py',
             'lib/gpt_image_25_web_audit.py', 'lib/image_url_safety.py', 'lib/job_spec.py',
             'scripts/run_gpt_image_25_responses_reference.py', 'lib/gpt_image_25_result_policy.py',
             'scripts/workflow_test.py',
             'lib/image_token_expectations.py', 'lib/token_audit.py', 'lib/model_identity.py',
             'lib/image_matrix_reference.py', 'lib/image_reference_candidate.py',
             'lib/gpt_image_resolution_quality.py', 'lib/grok_image_resolution_quality.py',
             'lib/banana_generate_content.py', 'lib/gemini_api_version.py',
             'lib/reference_specs.py', 'lib/model_profile_catalog.py', 'lib/config.py')
    return digest_json({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def _case(row):
    value = copy.deepcopy(row)
    value['tags'] = tuple(value.get('tags', []))
    if value.get('expected_size') is not None:
        value['expected_size'] = tuple(value['expected_size'])
    return ImageTestCase(**value)


def _path(url):
    value = urlsplit(url)
    return value.path + ('?' + value.query if value.query else '')


def _origin(url):
    value = urlsplit(url)
    return value.scheme + '://' + value.netloc


class _Response:
    """A requests-shaped read-only receipt for existing pure image decoders."""
    def __init__(self, receipt):
        self.receipt = receipt
        self.status_code = receipt.get('http_status', receipt.get('status_code'))
        if self.status_code is None:
            raise requests.RequestException(receipt.get('error_type', 'missing HTTP receipt'))
        self.headers = {**receipt.get('response_headers', {}), 'content-type': receipt.get('content_type', 'application/json'),
                        'x-request-id': receipt.get('request_id', '')}
        self._raw = (receipt.get('stream_events_text', '').encode() if receipt.get('stream_events_text') is not None
                     else base64.b64decode(receipt['raw_base64']) if receipt.get('raw_base64')
                     else canonical_bytes(receipt.get('response', {})))
        self.text = self._raw.decode('utf-8', errors='replace')
    def json(self):
        return copy.deepcopy(self.receipt.get('response') or {})
    def iter_lines(self, decode_unicode=False):
        return iter(self._raw.decode().splitlines() if decode_unicode else self._raw.splitlines())
    def iter_content(self, chunk_size=65536):
        return (self._raw[index:index + chunk_size] for index in range(0, len(self._raw), chunk_size))
    def close(self):
        pass


class _Session:
    def __init__(self, context, endpoint):
        self.context, self.endpoint = context, endpoint
    def post(self, url, *, json=None, data=None, files=None, **kwargs):
        if _origin(url) != _origin(self.endpoint):
            raise ValueError('Image request changed its frozen origin')
        request = {'method': 'POST', 'path': _path(url), 'headers': {},
                   'response_byte_limit': responses.MAX_RESPONSE_BYTES}
        if files is not None:
            prepared = requests.Request('POST', url, data=data, files=files).prepare()
            request.update(wire_base64=base64.b64encode(prepared.body).decode(),
                           content_type=prepared.headers['Content-Type'])
        else:
            request['body'] = copy.deepcopy(json)
        receipt = self.context.dispatch(request)
        if receipt.get('response_complete') is not True:
            raise requests.RequestException('incomplete image response receipt')
        return _Response(receipt)
    def get(self, url, **kwargs):
        if _origin(url) != _origin(self.endpoint):
            raise ValueError('Image discovery changed its frozen origin')
        return _Response(self.context.dispatch({'method': 'GET', 'path': _path(url)}, kind='discovery'))


def _remote_image(context, url, timeout):
    receipt = context.dispatch({'method': 'GET', 'url': url, 'public': True, 'image_artifact': True}, kind='public')
    if receipt.get('http_status') != 200 or receipt.get('response_complete') is not True:
        raise ValueError('Image artifact fetch did not complete')
    return base64.b64decode(receipt['raw_base64'], validate=True)


def _count_input(context, plan, body, model, transport):
    spec = plan.get('token_count')
    if not spec or (isinstance(spec.get('transports'), list) and transport not in spec['transports']):
        return None
    payload = copy.deepcopy(body)
    if spec.get('request_model_field'):
        payload[spec['request_model_field']] = 'models/' + model.removeprefix('models/')
    if spec.get('request_wrapper'):
        payload = {spec['request_wrapper']: payload}
    path = str(spec['path']).format(model=model)
    receipt = context.dispatch({'method': 'POST', 'path': path, 'body': payload, 'token_count': True}, kind='helper')
    if receipt.get('http_status') != 200 or receipt.get('response_complete') is not True:
        return None
    value = receipt.get('response')
    field = spec.get('response_field') or 'totalTokens'
    for part in field.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if type(value) is not int or value < 0:
        return None
    return {'tokens': value, 'evidence_level': 'official_count', 'covers_full_input': True,
            'kind': 'provider_count', 'source': 'token_count:' + field,
            'note': 'counted by the configured token-count interface through shared dispatch'}


def _artifact_dir(output_dir, context, name='images'):
    root = Path(output_dir) if output_dir else Path(tempfile.gettempdir()) / 'test-workflow-images'
    directory = root / context.run_id / name
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError('Image artifacts must not traverse a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _generic_once(context, inputs, *, config, output_dir):
    from scripts import image_param_test as domain
    plan, case = inputs['image_plan'], _case(inputs['case'])
    directory = _artifact_dir(output_dir, context, 'images/' + hashlib.sha256(case.name.encode()).hexdigest()[:12]
                              + '/attempt_' + str(inputs.get('attempt_index', 1)))
    result = domain.run_case(
        _Session(context, plan['endpoint']), plan['endpoint'], plan['model'], inputs['prompt'], case,
        timeout=plan['timeout_sec'], images_dir=directory,
        visual_forensics=plan.get('visual_forensics', True), transport=plan['transport'],
        auth_mode=plan.get('auth_mode', 'bearer'), config=config, provider=plan.get('provider'),
        provider_cfg=(get_provider_config(config, plan['provider']) if plan.get('provider') in config.get('providers', {}) else {}),
        api_version=plan.get('api_version'), reference_model=plan.get('reference_model'),
        reference_source=plan.get('source_id'),
        image_fetcher=lambda url, timeout: _remote_image(context, url, timeout),
        input_counter=lambda cfg, provider, model, body, transport: _count_input(context, plan, body, model, transport),
    )
    result['artifacts'] = [str((directory.parent / name).relative_to(Path(output_dir))) if output_dir
                           else str(directory.parent / name) for name in result.get('artifacts', [])]
    passed = result.get('overall_pass', result.get('pass')) is True
    diagnostic = result.get('diagnostic_pass') is True
    return {'status': 'passed' if passed else 'inconclusive' if diagnostic else 'failed',
            'accepted': result.get('status_code', result.get('http_status')) in {200, 201}
                        and passed and case.expected_outcome != 'rejection', 'record': result}


def _generic(context, inputs, *, config, output_dir):
    from scripts import image_param_test as domain
    plan = inputs['image_plan']
    previous = [row['record'] for row in context.results.values() if 'record' in row]
    if any(row.get('stop_reason') for row in previous):
        return {'status': 'blocked', 'accepted': False, 'reason': 'image_suite_stop_policy'}
    sent = sum(row.get('case_attempt', 1) for row in previous)
    cap = plan['max_generation_requests']
    if sent >= cap:
        return {'status': 'blocked', 'accepted': False, 'reason': 'generation_request_budget'}
    attempts, exchanges = [], []
    retries = plan.get('transient_retries', 0)
    for attempt in range(1, retries + 2):
        context.cancel.raise_if_cancelled()
        outcome = _generic_once(context, {**inputs, 'attempt_index': attempt}, config=config, output_dir=output_dir)
        row = outcome['record']
        sent += 1
        audit = copy.deepcopy(row.get('token_audit') or {})
        exchanges.extend({**exchange, 'exchange': f'attempt_{attempt}/' + exchange.get('exchange', 'initial')}
                         for exchange in audit.get('exchanges', []))
        retry = bool(plan.get('reference_candidate') and domain._transient_image_failure(row) and attempt <= retries and sent < cap)
        row.update(case_attempt=attempt, generation_request_number=sent, final_for_case=not retry,
                   retry_scheduled=retry, attempt_token_audit=audit)
        attempts.append(copy.deepcopy(row))
        context.record_event('image_attempt', row)
        if not retry:
            break
        delay = domain._image_retry_delay(row, attempt, plan.get('retry_initial_delay_seconds', 15))
        until = time.monotonic() + delay
        while time.monotonic() < until:
            context.cancel.raise_if_cancelled()
            time.sleep(max(0, min(0.1, until - time.monotonic())))
    if len(attempts) > 1:
        audit = domain.combine_exchange_audits(exchanges)
        row['token_audit'] = audit
        if audit.get('validation_pass') is not True:
            row.update(overall_pass=False, token_validation_pass=False, diagnostic_pass=False)
            outcome.update(status='failed', accepted=False)
    row['attempts'] = attempts
    if row.get('status_code') in {401, 403, 404} and inputs['case'].get('metadata', {}).get('gpt_image_25'):
        row['stop_reason'] = 'model_access_blocked'
    if plan.get('reference_candidate') and (
        (not previous and (row.get('status') != 'observed_acceptance' or row.get('diagnostic_pass') is not True
                           or row.get('dimension_validation_pass') is False))
        or row.get('status_code') in {401, 403, 404, 429}
        or (len(previous) >= 2 and all(item.get('diagnostic_pass') is not True for item in [*previous[-2:], row]))
    ):
        row['stop_reason'] = 'baseline_or_service_failure'
    return outcome


def _models(context, inputs):
    from scripts import image_param_test as domain
    result = domain._list_models(_Session(context, inputs['endpoint']), inputs['url'], inputs['timeout'],
                                SimpleNamespace(auth_headers=lambda **kwargs: {}), inputs['auth_mode'])
    result['requested_models'] = inputs['requested_models']
    result['missing_requested_models'] = [model for model in inputs['requested_models'] if model not in result.get('model_ids', [])]
    return {'status': 'passed', 'accepted': True, 'model_check': result}


def _delete_file(context, inputs):
    file_id = responses.require_id(inputs['file_id'], 'file')
    receipt = context.dispatch({'method': 'DELETE', 'path': '/v1/files/' + file_id})
    body = receipt.get('response') or {}
    deleted = receipt.get('http_status') == 200 and receipt.get('response_complete') is True and body.get('id') == file_id and body.get('deleted') is True
    return {'status': 'passed' if deleted else 'failed', 'accepted': deleted, 'deleted': deleted, 'receipt': receipt}


def _delete_response(context, inputs):
    response_id = responses.require_id(inputs['response_id'], 'resp')
    receipt = context.dispatch({'method': 'DELETE', 'path': '/v1/responses/' + response_id})
    body = receipt.get('response') or {}
    # Official Responses DELETE returns the exact response ID and deleted=true.
    # https://developers.openai.com/api/reference/typescript/resources/responses/methods/delete
    deleted = (receipt.get('http_status') == 200 and receipt.get('response_complete') is True
               and isinstance(body, dict) and body.get('id') == response_id
               and body.get('deleted') is True and not body.get('error'))
    return {'status': 'passed' if deleted else 'failed', 'accepted': deleted, 'deleted': deleted, 'receipt': receipt}


def _upload(context, inputs):
    label = inputs['label']
    if label not in {'image', 'mask'}:
        raise ValueError('Unknown public image fixture')
    raw = responses.fixture_png(mask=label == 'mask')
    prepared = requests.Request('POST', responses.ENDPOINT.replace('/responses', '/files'),
                                data={'purpose': 'vision'}, files={'file': ('public_fixture_' + label + '.png', raw, 'image/png')}).prepare()
    receipt = context.dispatch({'method': 'POST', 'path': '/v1/files',
                                'wire_base64': base64.b64encode(prepared.body).decode(),
                                'content_type': prepared.headers['Content-Type']}, kind='upload', creates_resource=True)
    payload = receipt.get('response') or {}
    file_id = payload.get('id')
    if receipt.get('http_status') in {200, 201} and isinstance(file_id, str) and re.fullmatch(r'file[-_][A-Za-z0-9_-]+', file_id):
        context.register_resource(file_id, cleanup_handler='image.files.delete', cleanup_inputs={'file_id': file_id})
    else:
        status = receipt.get('http_status')
        if type(status) is int and 400 <= status < 500 and status != 408:
            context.creation_failed(definitely_not_created=True)
    # Registration precedes semantic validation, including a malformed success envelope.
    responses.require_id(file_id, 'file')
    if receipt.get('http_status') != 200 or receipt.get('response_complete') is not True:
        raise ValueError('Image fixture upload did not complete')
    return {'status': 'passed', 'accepted': True, 'file_id': file_id, 'receipt': receipt}


def _responses(context, inputs, *, output_dir):
    from scripts.run_gpt_image_25_responses_reference import read_sse, _validate_package_case, _evaluate_package_case
    from lib.gpt_image_25_web_audit import build_case_audit, case_result_gates
    case = inputs['case']
    package = inputs.get('package')
    previous = [row['record'] for row in context.results.values() if 'record' in row]
    if package and (any(row.get('stop_reason') for row in previous)
                    or len(previous) >= inputs['generation_budget']):
        return {'status': 'blocked', 'accepted': False, 'reason': 'reference_generation_budget_or_stop_policy'}
    if package:
        _validate_package_case(package, case)
    else:
        responses.validate_case(case)
    files = inputs.get('files', {})
    body = responses.build_request(case, dependency=inputs.get('dependency'), file_ids=files)
    artifact_dir = _artifact_dir(output_dir, context, hashlib.sha256(case['case_id'].encode()).hexdigest()[:16])
    stream = None
    events = []
    receipt = {}
    cleanup = []
    evaluator = ((lambda *args, **kwargs: _evaluate_package_case(package, *args, **kwargs))
                 if package else responses.evaluate)
    request_started_at = datetime.now(timezone.utc).isoformat()
    try:
        stored = body.get('store') is True
        receipt = context.dispatch({'method': 'POST', 'path': '/v1/responses', 'body': body,
                                    'response_byte_limit': responses.MAX_RESPONSE_BYTES}, creates_resource=stored)
        payload = receipt.get('response') or {}
        if stored:
            response_id = payload.get('id') if isinstance(payload, dict) else None
            if receipt.get('http_status') == 200 and isinstance(response_id, str) and re.fullmatch(r'resp_[A-Za-z0-9_-]+', response_id):
                context.register_resource(response_id, cleanup_handler='image.responses.delete', cleanup_inputs={'response_id': response_id})
            elif type(receipt.get('http_status')) is int and 400 <= receipt['http_status'] < 500 and receipt['http_status'] != 408:
                context.creation_failed(definitely_not_created=True)
        if case['stream'] and receipt.get('http_status') == 200:
            events = read_sse(_Response(receipt))
            payload, stream = responses.validate_stream(events, case, artifact_dir,
                                                         expectation_policy=responses.CURRENT_EXPECTATION_POLICY)
        verdict = evaluator(case, receipt.get('http_status'), payload, artifact_dir,
                                     partial_count=(stream or {}).get('partial_count', 0),
                                     partial_counts_by_item_id=(stream or {}).get('partial_counts_by_item_id'),
                                     expectation_policy=responses.CURRENT_EXPECTATION_POLICY)
        if stream is not None:
            verdict['stream'] = stream
            verdict['pass'] = verdict['pass'] and stream['pass']
        if receipt.get('response_complete') is not True:
            verdict.update({'pass': False, 'envelope_pass': False})
            if package:
                verdict.update(diagnostic_pass=False, assessment='unresolved')
    except (ValueError, TypeError, OSError, requests.RequestException) as exc:
        payload = {'transport_or_setup_error_type': type(exc).__name__, 'message': str(exc)[:300]}
        verdict = evaluator(case, receipt.get('http_status'), payload, artifact_dir,
                            expectation_policy=responses.CURRENT_EXPECTATION_POLICY)
        verdict.update({'pass': False, 'envelope_pass': False})
    finally:
        for file_id in reversed(list(files.values())):
            resource = context.cleanup_resource(file_id)
            cleanup.append({'file_id': file_id, 'cleanup_pass': resource['status'] == 'deleted'})
    if case['input_kind'] == 'file_id':
        verdict['file_cleanup'] = {'required': True, 'pass': all(row['cleanup_pass'] for row in cleanup), 'files': cleanup}
    row = {'case_id': case['case_id'], 'case': 'gpt_image_25_responses_' + case['name'],
           'endpoint': responses.ENDPOINT, 'mainline_model': responses.MAINLINE_MODEL, 'tool_model': case['tool_model'],
           'status_code': receipt.get('http_status'), 'request': body, 'response': payload,
           'stream_events': events,
           'request_id': receipt.get('request_id'), 'response_bytes_sha256': receipt.get('response_bytes_sha256'),
           'request_sha256': responses.digest(body), 'verdict': verdict}
    if package and package.get('package_kind'):
        from lib.image_reference_candidate import image_exchange_evidence
        artifacts = copy.deepcopy(verdict.get('artifacts', []))
        for artifact in artifacts:
            if artifact.get('path') and output_dir:
                artifact['path'] = str(Path(artifact['path']).relative_to(Path(output_dir)))
        row.update(overall_pass=False, diagnostic_pass=verdict.get('diagnostic_pass') is True,
                   status=verdict.get('assessment'), token_validation_pass=case_result_gates(verdict)['token_validation_pass'],
                   compatibility_pass=False, certified_route_contract_pass=False,
                   case=case['name'], model=case['tool_model'], api_form='openai_responses',
                   metadata=copy.deepcopy(case.get('metadata', {})), requested=copy.deepcopy(body),
                   effective_request_parameters=copy.deepcopy(case['parameters']),
                   actual_images=artifacts, images=artifacts,
                   artifacts=[artifact['path'] for artifact in artifacts if artifact.get('path')])
        row.update(image_exchange_evidence(endpoint=responses.ENDPOINT, request_body=body,
                   response_payload=payload, request_started_at=request_started_at))
        row['pass'] = False
        if ((not previous and verdict.get('assessment') != 'observed_acceptance')
                or receipt.get('http_status') in {403, 404}
                or (len(previous) >= 2 and all(item.get('diagnostic_pass') is not True for item in [*previous[-2:], row]))):
            row['stop_reason'] = 'baseline_or_service_failure'
        return {'status': 'inconclusive' if row['diagnostic_pass'] else 'failed',
                'accepted': verdict.get('assessment') == 'observed_acceptance', 'record': responses.public_payload(row)}
    actual = receipt.get('request_bytes_sha256')
    expected = hashlib.sha256(canonical_bytes(body)).hexdigest()
    integrity = {'status': 'pass' if actual == expected else 'fail',
                 'expected_sha256': responses.digest(body), 'actual_sha256': responses.digest(body),
                 'wire_sha256': responses.digest(body) if actual == expected else actual,
                 'public_request_sha256': responses.digest(responses.public_payload(body)),
                 'verification_source': 'counted_dispatch_bytes_and_frozen_request_reconstruction'}
    audit = build_case_audit(case, row, integrity)
    gates = case_result_gates(verdict)
    passed = gates['overall_pass'] is True and audit['validation_pass'] is True and all(item['cleanup_pass'] for item in cleanup)
    row.update({'pass': verdict['pass'], 'compatibility_pass': verdict['pass'], 'overall_pass': passed, 'token_audit': audit,
                'token_validation_pass': audit['validation_pass'], 'request_input_integrity': integrity,
                'artifacts': verdict.get('artifacts', []), 'file_cleanup': verdict.get('file_cleanup'),
                'status': 'pass' if passed else 'failed', 'http_status': receipt.get('http_status'),
                'model': context.target['execution_target']['request_model_id'],
                'model_identity_pass': gates['model_identity_pass'], 'overall_failures': gates['overall_failures']})
    from lib.gpt_image_25_web_audit import POLICY_NAME
    images = copy.deepcopy(verdict.get('artifacts', []))
    for artifact in images:
        if artifact.get('path') and output_dir:
            artifact['path'] = str(Path(artifact['path']).relative_to(Path(output_dir)))
    row.update(images=images, artifacts=[artifact['path'] for artifact in images if artifact.get('path')],
               validation_policy=POLICY_NAME, model_identity_scope=gates['model_identity_scope'],
               token_validation_scope=gates['token_validation_scope'], validation_scope=gates['validation_scope'],
               image_tool_token_accuracy=verdict.get('image_tool_token_accuracy'),
               image_output_token_accuracy_pass=verdict.get('image_output_token_accuracy_pass'),
               token_accuracy_pass=verdict.get('image_output_token_accuracy_pass'),
               expected_outcome=verdict.get('effective_expected_outcome', verdict.get('expected_outcome')))
    return {'status': 'passed' if passed else 'failed',
            'accepted': passed and receipt.get('http_status') == 200
                        and verdict.get('effective_expected_outcome', verdict.get('expected_outcome')) != 'rejection',
            'record': responses.public_payload(row)}


def _register(config, output_dir):
    registry = HandlerRegistry()
    digest = execution_digest
    registry.register('image.case', lambda ctx, value: _generic(ctx, value, config=config, output_dir=output_dir), version='1', digest=digest)
    registry.register('image.responses', lambda ctx, value: _responses(ctx, value, output_dir=output_dir), version='1', digest=digest)
    registry.register('image.files.upload', _upload, version='1', digest=digest)
    registry.register('image.files.delete', _delete_file, version='1', digest=digest)
    registry.register('image.responses.delete', _delete_response, version='1', digest=digest)
    registry.register('image.models', _models, version='1', digest=digest)
    return registry


def prepare_cases_plan(config, image_plan, cases, *, prompt=None, output_dir=None, run_count=1, selected_cases=None):
    """Compile already validated image CLI/JobSpec cases without reading a key."""
    from scripts import image_param_test as domain
    plan = copy.deepcopy(image_plan)
    if plan['transport'] == 'gemini-interactions':
        raise ValueError('Ordinary Gemini Interactions remains disabled')
    model, provider = plan['model'], plan.get('provider') or '__image_cli__'
    snapshot = plan.get('model_profile_database') or {}
    interface = snapshot.get('interface') or {}
    target = {key: plan.get(key) or snapshot.get(key) for key in ('source_id', 'profile_id', 'interface_id')}
    target['contract_id'] = (plan.get('reference_contract_id') or snapshot.get('reference_contract_id')
                             or (snapshot.get('parameter_test_binding') or {}).get('contract_id')
                             or interface.get('default_contract_id'))
    target.update(modality='image', endpoint=plan['endpoint'], base_url=_origin(plan['endpoint']),
                  route_profile=plan.get('route_profile') or (snapshot.get('execution_target') or {}).get('route_profile') or '',
                  image_transport=plan['transport'], auth_mode=plan.get('auth_mode', 'bearer'),
                  api_version=plan.get('api_version'),
                  image_request_model_ids=sorted({case.model_override or model for case in cases}),
                  execution_target={'provider_id': provider, 'request_model_id': model, 'api_form': plan['api_form'],
                                    'transport_adapter_id': interface.get('transport_adapter_id') or plan['transport']})
    plan.setdefault('timeout_sec', 600)
    plan.setdefault('max_generation_requests', len(cases))
    if type(plan['max_generation_requests']) is not int or plan['max_generation_requests'] < 1:
        raise ValueError('Image generation request budget must be a positive integer')
    if type(plan.get('transient_retries', 0)) is not int or plan.get('transient_retries', 0) < 0:
        raise ValueError('Image retry count must be a nonnegative integer')
    delay = plan.get('retry_initial_delay_seconds', 15)
    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
        raise ValueError('Image retry delay must be finite and nonnegative')
    if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', case.name) or '..' in case.name for case in cases):
        raise ValueError('Image case names must be safe artifact names')
    plan['provider'] = provider
    if provider in config.get('providers', {}):
        raw = (get_provider_config(config, provider).get('api_interfaces') or {}).get('token_count')
        if isinstance(raw, dict):
            token = {**raw, 'base_url': raw.get('base_url') or get_provider_config(config, provider).get('base_url', '')}
            if not isinstance(token.get('path'), str) or not token['path'].startswith('/'):
                raise ValueError('Image token count requires an explicit configured path')
            plan['token_count'] = {key: token[key] for key in ('base_url', 'path', 'auth', 'transports', 'request_wrapper', 'request_model_field', 'response_field') if key in token}
            target['token_count'] = copy.deepcopy(plan['token_count'])
    runtime_plan = {key: copy.deepcopy(plan[key]) for key in (
        'endpoint', 'model', 'provider', 'transport', 'api_form', 'api_version', 'timeout_sec',
        'auth_mode', 'visual_forensics', 'source_id', 'reference_model', 'token_count',
        'reference_candidate', 'max_generation_requests', 'transient_retries', 'retry_initial_delay_seconds',
    ) if key in plan}
    runtime_plan['source_id'] = target['source_id']
    runtime_plan['reference_model'] = (plan.get('reference_model') or snapshot.get('reference_model_id')
                                        or (snapshot.get('profile') or {}).get('model_slug'))
    steps, suite, cleanup, requests_cap = [], [], 0, 0
    if plan['transport'] == 'openai-responses-image':
        if plan['endpoint'] != responses.ENDPOINT:
            raise ValueError('Responses image workflows require the exact official endpoint')
        selected = [case.name.removeprefix('gpt_image_25_responses_') for case in cases]
        package = plan.get('responses_package')
        rows = (copy.deepcopy(package['cases']) if package else responses.expand_responses_case_dependencies(
            [row for row in responses.responses_cases(model)], [model + '/responses/' + name for name in selected]))
        for row in rows:
            case_id = row['case_id']
            suite.append({'id': case_id})
            inputs, dependencies = {'case': row, 'files': {}}, []
            if package:
                inputs['package'] = {key: copy.deepcopy(package[key]) for key in ('package_kind', 'models')}
                inputs['generation_budget'] = package['generation_request_budget']
            if row.get('depends_on'):
                dependencies.append(row['depends_on'])
                inputs['dependency'] = {'$ref': {'step': row['depends_on'], 'path': ['record'], 'type': 'object'}}
            if row['input_kind'] == 'file_id':
                labels = ['image'] + (['mask'] if row['parameters'].get('input_image_mask', {}).get('file_id') else [])
                for label in labels:
                    upload_id = case_id + '/upload/' + label
                    steps.append({'id': upload_id, 'case_id': case_id, 'handler': 'image.files.upload', 'inputs': {'label': label}})
                    dependencies.append(upload_id)
                    inputs['files'][label] = {'$ref': {'step': upload_id, 'path': ['file_id'], 'type': 'string'}}
                    cleanup += 1
                    requests_cap += 1
            steps.append({'id': case_id, 'case_id': case_id, 'handler': 'image.responses', 'inputs': inputs,
                          'depends_on': dependencies, 'requires': [{'step': dep, 'path': ['accepted'], 'equals': True} for dep in dependencies]})
            requests_cap += 1
            if row['name'] == 'multiturn_seed':
                cleanup += 1
    else:
        if cases:
            steps.append({'id': 'image.models', 'case_id': cases[0].name, 'handler': 'image.models',
                          'inputs': {'endpoint': plan['endpoint'], 'url': domain.models_endpoint(plan['endpoint'], plan['transport'], plan['family']),
                                     'timeout': plan['timeout_sec'], 'auth_mode': plan.get('auth_mode', 'bearer'),
                                     'requested_models': sorted({case.model_override or model for case in cases})}})
            requests_cap += 1
        for case in cases:
            suite.append({'id': case.name})
            steps.append({'id': case.name, 'case_id': case.name, 'handler': 'image.case',
                          'inputs': {'image_plan': runtime_plan, 'case': case.public(), 'prompt': prompt or domain.DEFAULT_PROMPT}})
            # One generation, an optional count endpoint, and each requested image fetch.
            n = case.parameters.get('n', 1)
            requests_cap += 2 + (n if type(n) is int and 0 < n <= 16 else 1)
        requests_cap *= 1 + plan.get('transient_retries', 0)
    registry = _register(config, output_dir)
    definition = {'workflow_schema_version': 1, 'id': f'image/{provider}/{model}/{plan["transport"]}',
                  'target': target, 'cases': suite, 'steps': steps, 'image_plan': copy.deepcopy(plan),
                  'factory_arguments': {'prompt': prompt},
                  'factory': {'factory_id': RESPONSES_FACTORY_ID if plan['transport'] == 'openai-responses-image' else FACTORY_ID,
                              'version': FACTORY_VERSION, 'source_sha256': source_digest()},
                  'limits': {'max_requests': max(1, requests_cap), 'request_timeout_seconds': plan['timeout_sec'],
                             'deadline_seconds': max(1, requests_cap) * plan['timeout_sec'],
                             'cleanup_max_requests': max(1, cleanup), 'cleanup_deadline_seconds': max(30, cleanup * 30)},
                  'policy': {'fatal_status_codes': [401, 403] if plan.get('reference_candidate') and plan.get('transient_retries', 0) else [401, 403, 429]}}
    return compile_plan(definition, registry, selected_cases=selected_cases, run_count=run_count), registry


def registry_for_image_plan(plan, config, *, output_dir=None, cleanup_only=False):
    """Restore only reviewed image handlers for a frozen service/CLI plan."""
    from .. import validate_plan
    if plan.get('definition', {}).get('factory', {}).get('factory_id') not in {FACTORY_ID, RESPONSES_FACTORY_ID}:
        raise ValueError('Not a registered image workflow factory')
    registry = _register(config, output_dir)
    validate_plan(plan, None if cleanup_only else registry)
    return registry


def bind_frozen_image_plan(compiled, config, job_spec, *, output_dir=None):
    """Keep the exact v6 plan while rejecting conflicting CLI target/case inputs."""
    if not job_spec or job_spec.get('schema_version') != 6:
        return compiled, registry_for_image_plan(compiled, config, output_dir=output_dir)
    frozen = job_spec['execution_plan']
    from ..service import validate_current_image_job
    registry = validate_current_image_job(config, job_spec, output_dir=output_dir)
    for name in ('execution_target', 'endpoint', 'source_id', 'profile_id', 'interface_id', 'contract_id', 'api_version', 'route_profile', 'auth_mode', 'image_request_model_ids'):
        if compiled['target'].get(name) != frozen['target'].get(name):
            raise ValueError('Image CLI target differs from the frozen plan: ' + name)
    if compiled['selected_cases'] != frozen['selected_cases']:
        raise ValueError('Image CLI case selection differs from the frozen plan')
    expected = {step['id']: step for step in frozen['ordered_steps']}
    for step in compiled['ordered_steps']:
        original = expected.get(step['id'])
        if original is None or step['handler'] != original['handler']:
            raise ValueError('Image CLI step differs from the frozen plan')
        for key in ('case', 'prompt', 'files', 'dependency'):
            if step.get('inputs', {}).get(key) != original.get('inputs', {}).get(key):
                raise ValueError('Image CLI request inputs differ from the frozen plan: ' + key)
        for key in ('endpoint', 'model', 'transport', 'api_form', 'api_version', 'timeout_sec', 'auth_mode', 'visual_forensics', 'token_count'):
            if step.get('inputs', {}).get('image_plan', {}).get(key) != original.get('inputs', {}).get('image_plan', {}).get(key):
                raise ValueError('Image CLI controls differ from the frozen plan: ' + key)
    return frozen, registry


def claim_image_execution(plan, output_dir, *, job_spec=None, already_claimed=False):
    """Own exactly one image execution before reading credentials or sending HTTP."""
    from lib.report_retention import initialize_report_retention, register_report_files
    directory = Path(output_dir).absolute()
    if type(already_claimed) is not bool:
        raise ValueError('Image parent claim state must be boolean')
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError('Image evidence must not traverse a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if job_spec is None and (directory / 'job_spec.json').exists():
        raise ValueError('An existing image JobSpec requires the same explicit frozen job')
    if any((directory / name).exists() for name in ('plan.json', 'case_results.json', 'summary.json', 'workflow_report.json', 'workflow_runs')):
        raise ValueError('Image report directory already contains business evidence')
    for name, value in (('execution_plan.json', plan), ('job_spec.json', job_spec)):
        path = directory / name
        if path.is_symlink():
            raise ValueError('Image job evidence cannot be a symbolic link')
        if value is not None and path.exists() and json.loads(path.read_text(encoding='utf-8')) != value:
            raise ValueError('Image evidence belongs to another frozen job')
    marker = directory / 'dispatch_started.json'
    identity = {'plan_digest': plan['plan_digest'], 'target': plan['target']}
    if already_claimed:
        with os.fdopen(os.open(marker, os.O_RDONLY | os.O_NOFOLLOW), encoding='utf-8') as stream:
            claim = json.load(stream)
        if any(claim.get(key) != value for key, value in identity.items()):
            raise ValueError('Image dispatch claim differs from the frozen plan')
    else:
        with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w', encoding='utf-8') as stream:
            json.dump({**identity, 'created_at': datetime.now(timezone.utc).isoformat()}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    activation = directory / 'image_dispatch_entered.json'
    with os.fdopen(os.open(activation, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w', encoding='utf-8') as stream:
        json.dump(identity, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    files = [marker, activation]
    for name, value in (('execution_plan.json', plan), ('job_spec.json', job_spec)):
        if value is None:
            continue
        path = directory / name
        if path.is_symlink():
            raise ValueError('Image job evidence cannot be a symbolic link')
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) != value:
                raise ValueError('Image evidence belongs to another frozen job')
        else:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        files.append(path)
    initialize_report_retention(directory, root=directory.parent)
    register_report_files(directory, files, root=directory.parent)


def register_image_execution(output_dir, summary):
    """Register only named image results, their artifacts and actual run ledgers."""
    from lib.report_retention import register_report_files
    directory = Path(output_dir).absolute()
    report = summary['workflow_result']
    files = [directory / name for name in ('summary.json', 'plan.json', 'workflow_report.json',
             'case_results.json', 'model_check.json', 'attempt_results.json')]
    for run in report['runs']:
        files.extend([Path(run['ledger_path']), Path(run['ledger_path']).parent / 'run.lock'])
    for row in json.loads((directory / 'case_results.json').read_text(encoding='utf-8')):
        files.extend(directory / path for path in row.get('artifacts', []) if isinstance(path, str))
        for artifact in (row.get('verdict') or {}).get('stream', {}).get('partial_images', []):
            if isinstance(artifact, dict) and artifact.get('path'):
                path = Path(artifact['path'])
                files.append(path if path.is_absolute() else directory / path)
    if (directory / 'reference_candidate.json').is_file():
        files.append(directory / 'reference_candidate.json')
    register_report_files(directory, [path for path in files if path.is_file()], root=directory.parent)


def prepare_image_plan(config, payload, *, provider=None, model=None, output_dir=None):
    """Pure preview shared by service, CLI and Web; credentials are not resolved."""
    from lib.job_spec import resolve_image_plan
    if not isinstance(payload, dict) or not isinstance(config, dict):
        raise ValueError('Image preparation requires config and payload objects')
    for field, override in (('provider', provider), ('model', model)):
        if override is not None and payload.get(field) not in (None, override):
            raise ValueError('Image payload conflicts with explicit ' + field)
    provider, model = provider or payload.get('provider'), model or payload.get('model')
    requested = copy.deepcopy(payload)
    if requested.get('image_plan') is None:
        requested['image_plan'] = {}
    options = requested['image_plan']
    if not isinstance(options, dict):
        raise ValueError('image_plan must be an object')
    options.setdefault('suite', 'full')
    timeout = payload.get('timeout_sec')
    if timeout in (None, ''):
        timeout = get_timeout_sec(config)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('Image workflow timeout must be a positive integer')
    resolved = resolve_image_plan(config, requested, provider, model, timeout)
    if not resolved.get('model_profile_database'):
        from lib.model_profile_catalog import resolve_runtime_parameter_config
        bound = resolve_runtime_parameter_config(config, provider, model, resolved['family'], resolved['route_profile'],
                                                  resolved['api_form'], modality='image')
        resolved['model_profile_database'] = copy.deepcopy(bound['model_profile_database'])
    if isinstance(resolved.get('model_capability_profile'), dict):
        resolved['model_capability_profile']['model_profile_database'] = copy.deepcopy(resolved['model_profile_database'])
    cases = cases_from_image_plan(resolved)
    return prepare_cases_plan(config, resolved, cases, prompt=payload.get('prompt'), output_dir=output_dir,
                              run_count=payload.get('run_count', 1))


def cases_from_image_plan(plan):
    """Use the same authored case factories and frozen policy as the image CLI."""
    from scripts import image_param_test as domain
    from lib.model_profile_catalog import binding_from_database_snapshot, capability_profile_from_database_snapshot
    model, family, transport = plan['model'], plan['family'], plan['transport']
    binding = binding_from_database_snapshot(plan['model_profile_database'])
    if hasattr(domain, 'capability_profile_from_binding'):
        capability = domain.capability_profile_from_binding(binding)
    else:
        capability = capability_profile_from_database_snapshot(plan['model_profile_database'])
    options = {'include_4k': plan['include_4k'], 'include_negative': not plan['no_negative']}
    exact_gc = family == 'banana' and domain.has_exact_banana_gc_reference(model, capability)
    if exact_gc:
        cases = domain.build_banana_generate_content_cases(model, plan['suite'], capability_profile=capability, **options)
    elif family == 'banana' and model == 'gemini-3.1-flash-lite-image' and plan['route_profile'] == 'google_ai_studio':
        cases = domain.gemini_flash_31_lite_image_profile_cases(plan['suite'], api_form=plan['api_form'], capability_profile=capability, **options)
    elif family == 'banana':
        cases = domain.banana_variant_cases(plan['suite'], model_template=model, include_cross_control=not plan['no_cross_control'],
                  transport='gemini-interactions' if transport == 'gemini-generate-content' else transport, **options)
    elif domain.is_gpt_image_25(model):
        cases = (responses.responses_image_cases(model, plan['suite'], expectation_policy=responses.CURRENT_EXPECTATION_POLICY, **options)
                 if transport == 'openai-responses-image' else domain.gpt_image_25_cases(model, plan['suite'],
                    operation='edit' if transport == 'images-edits' else 'generation', **options))
    elif family == 'gpt-image-2':
        cases = domain.gpt_image_2_cases(plan['suite'], **options)
    else:
        cases = domain.grok_imagine_cases(plan['suite'], include_2k=plan['include_2k'], include_negative=not plan['no_negative'])
    cases = domain._select_cases(cases, plan['cases'])
    if not exact_gc and not domain.is_gpt_image_25(model):
        if hasattr(domain, '_apply_capability_expectations'):
            cases = domain._apply_capability_expectations(cases, family=family, model=model, capability=capability)
        else:
            from lib.reference_specs import resolve_profile_expectation
            revised = []
            for case in cases:
                profile = case.metadata.get('test_profile') or case.name
                expectation = resolve_profile_expectation('image', family, model, profile, capability_profile=capability)
                revised.append(replace(case, expected_outcome='rejection' if expectation == 'unsupported' else
                    'success' if case.expected_outcome == 'rejection' else case.expected_outcome,
                    expected_size=None if expectation == 'unsupported' else case.expected_size,
                    expected_format=None if expectation == 'unsupported' else case.expected_format,
                    metadata={**case.metadata, 'expectation_profile': profile, 'expectation': expectation,
                              'capability_family': family, 'capability_model': model,
                              'capability_api_form': capability['api_form'], 'capability_route_profile': capability['route_profile']}))
            cases = revised
    if family != 'grok-imagine' and transport != 'openai-responses-image':
        cases = [domain._with_output_options(case, plan.get('quality') or 'low', plan.get('output_format') or 'png', transport=transport) for case in cases]
    return cases


def make_image_dispatcher(config, plan, *, credential=None, **kwargs):
    """API requests use shared HTTP; returned images retain DNS/decoder guards."""
    target = plan['target']
    provider = target['execution_target']['provider_id']
    interface_name = IMAGE_TRANSPORT_INTERFACES[target['image_transport']]
    if provider in config.get('providers', {}):
        from scripts import image_param_test as domain
        configured_endpoint = domain.normalize_image_endpoint(get_image_endpoint(config, provider, target['image_transport']),
            target['image_transport'], model=target['execution_target']['request_model_id'], api_version=target.get('api_version') or 'v1')
        if (domain._image_endpoint_contract(configured_endpoint) != domain._image_endpoint_contract(target['endpoint'])
                or get_provider_interface(config, interface_name, provider).get('auth') != target['auth_mode']):
            raise ValueError('Configured image endpoint or authentication differs from the frozen plan')
    configured = copy.deepcopy(config)
    # Pin the already-validated image endpoint; the runtime key remains external.
    cfg = configured.setdefault('providers', {}).setdefault(provider, {})
    interface = cfg.setdefault('api_interfaces', {}).setdefault(interface_name, {})
    interface.update(base_url=target['base_url'], path=_path(target['endpoint']), auth=target['auth_mode'])
    if credential is not None:
        kwargs['credential_factory'] = lambda config, provider: credential
    kwargs.setdefault('session_factory', requests.Session)
    api = HttpDispatcher(configured, provider, interface_name, response_byte_limit=responses.MAX_RESPONSE_BYTES, **kwargs)
    counter = None
    if target.get('token_count'):
        token = target['token_count']
        configured['providers'][provider]['api_interfaces']['token_count'] = copy.deepcopy(token)
        class CountDispatcher(HttpDispatcher):
            def _api_target(self, request, context):
                base = str(token['base_url']).rstrip('/')
                parsed = urlsplit(base)
                allowed_paths = {token['path'].format(model=model) for model in target['image_request_model_ids']}
                expected_path = request.get('path')
                if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or expected_path not in allowed_paths or not expected_path.startswith('/')
                        or expected_path.startswith('//') or '..' in urlsplit(expected_path).path.split('/')):
                    raise ValueError('Image count request differs from its frozen endpoint')
                prefix = parsed.path.rstrip('/')
                url = (_origin(base) + expected_path if prefix and expected_path.startswith(prefix + '/') else base + expected_path)
                return url, str(token.get('auth') or 'bearer'), ''
        counter = CountDispatcher(configured, provider, 'token_count', **kwargs)
    def dispatch(request, *, timeout, context):
        if request.get('image_artifact'):
            from lib.image_url_safety import fetch_remote_image, _resolve_public_addresses
            if request.get('method') != 'GET' or request.get('public') is not True or timeout < 1:
                raise ValueError('Image artifact requires one public GET within a complete bounded second')
            # Validate all DNS answers, then permit exactly one connection attempt.
            def first_public(host, port, **options):
                addresses = _resolve_public_addresses(host, port, resolver=socket.getaddrinfo)
                value = addresses[0]
                family = socket.AF_INET6 if ':' in value else socket.AF_INET
                return [(family, socket.SOCK_STREAM, 6, '', (value, port))]
            remote = fetch_remote_image(request['url'], timeout, resolver=first_public)
            return {'http_status': 200, 'response_complete': True, 'content_type': remote.mime_type,
                    'raw_base64': base64.b64encode(remote.data).decode(),
                    'response_bytes_sha256': hashlib.sha256(remote.data).hexdigest(), 'response_byte_length': len(remote.data)}
        if request.get('token_count'):
            if counter is None:
                raise ValueError('No frozen token count interface')
            proxy = SimpleNamespace(target={**context.target, 'base_url': target['token_count']['base_url']})
            return counter(request, timeout=timeout, context=proxy)
        return api(request, timeout=timeout, context=context)
    return dispatch


def result_rows(report):
    return [copy.deepcopy(row['record']) for run in report['runs'] for row in run['steps'].values() if 'record' in row]


def _responses_run_gates(report, raw_cases, expected_runs):
    from lib.gpt_image_25_result_policy import aggregate_result_gates
    run_gates = [{'run_id': run['run_id'], 'run_index': run['run_index'],
                  'gates': aggregate_result_gates({'cases': raw_cases}, result_rows({'runs': [run]}))}
                 for run in report['runs']]
    if expected_runs == 1 and len(run_gates) == 1:
        return run_gates[0]['gates']
    complete = (len(run_gates) == expected_runs
                and len({run['run_id'] for run in run_gates}) == expected_runs
                and [run['run_index'] for run in run_gates] == list(range(1, expected_runs + 1)))
    gates = [run['gates'] for run in run_gates]
    result = {key: complete and all(gate[key] for gate in gates) for key in (
        'pass', 'overall_pass', 'compatibility_pass', 'parameter_checks_pass', 'all_cases_recorded')}
    for key in ('token_validation_pass', 'model_identity_pass', 'file_cleanup_pass'):
        checks = [gate[key] for gate in gates if gate[key] is not None]
        result[key] = all(checks) if checks else None
    for key in ('overall_passed_cases', 'semantic_reviewed_cases'):
        result[key] = sum(gate[key] for gate in gates)
    for key in ('overall_failed_cases', 'missing_cases', 'semantic_failed_cases', 'file_cleanup_failed_cases'):
        result[key] = [case for gate in gates for case in gate[key]]
    result.update(unexpected_or_duplicate_cases=any(gate['unexpected_or_duplicate_cases'] for gate in gates),
                  required_check_failures={run['run_id']: run['gates']['required_check_failures']
                                           for run in run_gates if run['gates']['required_check_failures']},
                  model_identity_scope='mainline_model_and_exposed_tool_configuration',
                  validation_scope='declared_parameter_checks_and_attached_semantic_review',
                  run_result_gates=run_gates)
    return result


def execute_image_cli_cases(config, plan, registry, *, credential, report_dir):
    """Execute the shared plan and retain the image CLI's report projections."""
    from scripts import image_param_test as domain
    from .. import CancellationContext
    cancellation = CancellationContext()
    with cancellation.install_signal_handlers():
        report = execute_plan(plan, registry, make_image_dispatcher(config, plan, credential=credential),
                              cancellation=cancellation, evidence_dir=Path(report_dir) / 'workflow_runs')
    rows = result_rows(report)
    model_check = next((step['model_check'] for run in report['runs'] for step in run['steps'].values() if 'model_check' in step), {})
    sent = sum(row.get('case_attempt', 1) for row in rows)
    options = next((step['inputs']['image_plan'] for step in plan['definition']['steps'] if 'image_plan' in step['inputs']), {})
    cap = options.get('max_generation_requests', len(plan['selected_cases'])) * plan['run_count']
    planned = len(plan['selected_cases']) * plan['run_count']
    budget = {'generation_request_count': sent, 'planned_case_count': planned,
              'executed_case_count': len(rows), 'not_executed_count': planned - len(rows),
              'max_generation_requests': cap, 'remaining_generation_requests': max(0, cap - sent),
              'budget_exhausted': sent >= cap and len(rows) < planned,
              'transient_retries': options.get('transient_retries', 0),
              'retry_initial_delay_seconds': options.get('retry_initial_delay_seconds', 15),
              'total_request_count': sum(run['request_count'] for run in report['runs']),
              'cleanup_request_count': sum(run['cleanup_request_count'] for run in report['runs']),
              'workflow_status': report['status'],
              'workflow_result': report,
              'stop_reason': next((row['stop_reason'] for row in rows if row.get('stop_reason')), None)}
    for name, value in (('workflow_report.json', report), ('case_results.json', rows), ('model_check.json', model_check),
                        ('attempt_results.json', [attempt for row in rows for attempt in row.get('attempts', [])])):
        domain._write_json(Path(report_dir) / name, value)
    return rows, budget


def run_responses_image_cli(args, **selection):
    """Reuse the existing read-only Responses preview and execute explicit steps."""
    if getattr(args, 'transient_retries', 0):
        raise ValueError('Responses image workflows do not support transient retry selection')
    from scripts import run_gpt_image_25_responses_reference as domain
    from scripts import image_param_test as image_cli
    from lib.credential_security import ProviderCredential, credential_from_config
    dispatch_claimed = selection.pop('dispatch_claimed', False)
    preview_args = copy.copy(args)
    preview_args.dry_run = True
    captured = io.StringIO()
    with redirect_stdout(captured):
        domain.main_image_cli(preview_args, **selection)
    preview = json.loads(captured.getvalue())
    config = selection.get('config') or {}
    model = selection['model']
    output = image_cli._report_dir(getattr(args, 'output_dir', None), model).absolute()
    cases = [_case(row) for row in preview['cases']]
    candidate = selection.get('reference_candidate')
    if candidate:
        preview['responses_package'] = domain.build_matrix_package(model, [case.name for case in cases], candidate,
                                      max_generation_requests=getattr(args, 'max_generation_requests', None))
    plan, registry = prepare_cases_plan(config, {**preview, 'timeout_sec': getattr(args, 'timeout', 600)}, cases, output_dir=output)
    plan, registry = bind_frozen_image_plan(plan, config, selection.get('job_spec'), output_dir=output)
    preview['execution_plan'] = plan
    if args.dry_run:
        print(json.dumps(preview, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if output.is_symlink() or output.resolve() != output:
        raise ValueError('Image output directory must not traverse a symlink')
    if any((output / name).exists() for name in ('plan.json', 'case_results.json', 'summary.json')):
        raise ValueError('Image output directory already contains evidence')
    claim_image_execution(plan, output, job_spec=selection.get('job_spec'), already_claimed=dispatch_claimed)
    credential_provider = getattr(args, 'credential_provider', None)
    if credential_provider:
        credential = credential_from_config(config, credential_provider)
    elif getattr(args, 'api_key_stdin', False):
        import getpass
        credential = ProviderCredential.create(provider='openai_image_reference', secret=getpass.getpass('OpenAI API key: '), base_urls=[responses.ENDPOINT])
    elif os.getenv(getattr(args, 'api_key_env', 'IMAGE_TEST_API_KEY')):
        credential = ProviderCredential.create(provider='openai_image_reference', secret=os.environ[getattr(args, 'api_key_env', 'IMAGE_TEST_API_KEY')], base_urls=[responses.ENDPOINT])
    else:
        credential = domain.designated_credential()
    output.mkdir(parents=True, exist_ok=True)
    if candidate:
        domain.persist_image_matrix_candidate(args.reference_candidate, output / 'reference_candidate.json', candidate)
    image_cli._write_json(output / 'plan.json', preview)
    rows, budget = execute_image_cli_cases(config, plan, registry, credential=credential, report_dir=output)
    summary = {'pass': budget['workflow_status'] == 'passed' and bool(rows), 'overall_pass': budget['workflow_status'] == 'passed' and bool(rows),
               'case_count': len(rows), 'failure_count': sum(row.get('overall_pass') is not True for row in rows),
               'pass_count': sum(row.get('overall_pass') is True for row in rows),
               'model': model, 'api_form': 'openai_responses', 'transport': 'openai-responses-image',
               'endpoint': responses.ENDPOINT, 'mainline_model': responses.MAINLINE_MODEL,
               'route_profile': preview.get('route_profile'),
               'model_profile_database': preview['model_profile_database'], 'report_dir': str(output), **budget}
    if candidate:
        summary.update({'pass': False, 'overall_pass': False, 'certified': False, 'execution_mode': 'reference_observation',
                        'diagnostic_pass': len(rows) == budget['planned_case_count'] and all(row.get('diagnostic_pass') for row in rows),
                        'compatibility_pass': False, 'certified_route_contract_pass': False,
                        'reference_candidate': copy.deepcopy(candidate)})
        summary.update({key: candidate[key] for key in ('source_id', 'profile_id', 'interface_id')})
    else:
        from lib.gpt_image_25_web_audit import summarize_case_audits, summarize_run_audits, POLICY_NAME
        raw_cases = [step['inputs']['case'] for step in plan['ordered_steps'] if step['handler'] == 'image.responses']
        gates = _responses_run_gates(budget['workflow_result'], raw_cases, plan['run_count'])
        audit_summary = (summarize_case_audits(rows, raw_cases) if plan['run_count'] == 1 else
                         summarize_run_audits(budget['workflow_result']['runs'], raw_cases, plan['run_count']))
        summary.update(gates, token_audit_summary=audit_summary, validation_policy=POLICY_NAME,
                       token_validation_pass=audit_summary['pass'])
        summary['pass'] = summary['overall_pass'] = gates['pass'] and audit_summary['pass'] and budget['workflow_status'] == 'passed'
    image_cli._write_json(output / 'summary.json', summary)
    register_image_execution(output, summary)
    print(json.dumps({'report_dir': str(output), 'overall_pass': summary['overall_pass'],
                      'workflow_status': summary['workflow_status'],
                      'token_validation_pass': summary.get('token_validation_pass')}, sort_keys=True))
    return 0 if (summary.get('diagnostic_pass') if candidate else summary['pass']) else 1


RESPONSES_WORKFLOW_MARKER = 'shared_workflow_plan.json'


def _register_responses_run_evidence(batch, plan, evidence_dir, run_id):
    """Own exact run records and referenced decoded outputs, also on failure."""
    from scripts import run_gpt_image_25_responses_reference as domain
    from scripts.workflow_test import _owned_ledger
    from lib.report_retention import register_report_files
    ledger = evidence_dir / run_id / 'ledger.json'
    if not ledger.exists():
        return
    state = json.loads(_owned_ledger(ledger, {'execution_plan': plan}, run_id))
    files = [ledger, ledger.parent / 'run.lock']
    for step in state['steps'].values():
        row = step.get('record') or {}
        verdict = row.get('verdict') or {}
        artifacts = list(verdict.get('artifacts') or [])
        artifacts.extend((verdict.get('stream') or {}).get('partial_images') or [])
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get('path'):
                path = Path(artifact['path'])
                files.append(path if path.is_absolute() else batch / 'shared' / path)
        files.extend(batch / 'shared' / path for path in row.get('artifacts', []) if isinstance(path, str))
    register_report_files(batch, files, root=domain.REPORT_ROOT)


@contextmanager
def _responses_recovery_evidence(batch, plan, evidence_dir, run_id, *, cleanup_only):
    """Fork registered cleanup evidence without changing any ownership hash."""
    from scripts import run_gpt_image_25_responses_reference as domain
    from scripts.workflow_test import _owned_ledger, _publish_recovery_directory
    from lib.report_retention import register_report_files
    if not cleanup_only:
        try:
            yield evidence_dir
        finally:
            _register_responses_run_evidence(batch, plan, evidence_dir, run_id)
        return
    original = evidence_dir / run_id
    if any(path.is_symlink() for path in (evidence_dir.parent, evidence_dir, original)) or not original.is_dir():
        raise ValueError('Original shared cleanup directory is missing or unsafe')
    descriptor = os.open(original / 'run.lock', os.O_RDWR | os.O_NOFOLLOW)
    job = {'execution_plan': plan}
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = original / 'ledger.json'
        raw = _owned_ledger(source, job, run_id)
        _register_responses_run_evidence(batch, plan, evidence_dir, run_id)
        lineage = batch / 'shared_cleanup_recoveries' / run_id
        if any(path.is_symlink() for path in (lineage.parent, lineage)):
            raise ValueError('Unsafe shared cleanup recovery directory')
        lineage.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = sorted(lineage.iterdir())
        for ordinal, directory in enumerate(existing, 1):
            if directory.name != f'{ordinal:06d}' or directory.is_symlink() or not directory.is_dir():
                raise ValueError('Shared cleanup recovery lineage is not contiguous')
            marker = directory / 'recovery_claim.json'
            if marker.is_symlink() or not marker.is_file():
                raise ValueError('Shared cleanup recovery source claim is missing')
            claim = json.loads(marker.read_text(encoding='utf-8'))
            if (claim.get('schema_version') != 1 or claim.get('publication_protocol') != 'atomic-v1'
                    or claim.get('run_id') != run_id or claim.get('plan_digest') != plan['plan_digest']
                    or claim.get('source_ledger') != str(source.relative_to(batch))
                    or claim.get('source_sha256') != hashlib.sha256(raw).hexdigest()):
                raise ValueError('Shared cleanup recovery source lineage changed')
            register_report_files(batch, [marker], root=domain.REPORT_ROOT)
            next_evidence = directory / 'workflow_runs'
            if next_evidence.is_symlink() or (next_evidence / run_id).is_symlink():
                raise ValueError('Unsafe shared cleanup run directory')
            source = next_evidence / run_id / 'ledger.json'
            raw = _owned_ledger(source, job, run_id)
            _register_responses_run_evidence(batch, plan, next_evidence, run_id)
        destination = lineage / f'{len(existing) + 1:06d}'
        claim = {'schema_version': 1, 'run_id': run_id, 'plan_digest': plan['plan_digest'],
                 'source_ledger': str(source.relative_to(batch)), 'source_sha256': hashlib.sha256(raw).hexdigest(),
                 'created_at': datetime.now(timezone.utc).isoformat()}
        _publish_recovery_directory(lineage, destination, run_id, claim, raw)
        register_report_files(batch, [destination / 'recovery_claim.json'], root=domain.REPORT_ROOT)
        recovered = destination / 'workflow_runs'
        try:
            yield recovered
        finally:
            _register_responses_run_evidence(batch, plan, recovered, run_id)
    finally:
        os.close(descriptor)


def prepare_responses_batch(package, batch, *, write=True):
    """Attach a shared-engine sidecar to a newly prepared dedicated CLI batch.

    The original package and historical replay format remain unchanged. Only
    batches explicitly prepared with this sidecar can execute through the CLI.
    """
    from scripts import run_gpt_image_25_responses_reference as domain
    from lib.model_profile_catalog import load_catalog
    domain.validate_package(package)
    catalog = load_catalog()
    plans = []
    for model in package['models']:
        selected_ids = {case['case_id'] for case in package['cases'] if case['case_id'].startswith(model + '/responses/')}
        if not selected_ids:
            continue
        interface = catalog.resolve_request_model('openai', model, 'openai_responses', modality='image')
        profile = catalog.get_profile(interface['profile_id'])
        reference = catalog.resolve_parameter_config(source_id='openai', modality='image', family_id=profile['family_id'],
            model_slug=profile['model_slug'], interface_id=interface['interface_id'], api_form='openai_responses')
        cases = [case for case in responses.responses_image_cases(model, 'full') if case.metadata['case_id'] in selected_ids]
        if {case.metadata['case_id'] for case in cases} != selected_ids:
            raise ValueError('Dedicated package contains cases outside the registered image factory')
        public_plan = {'provider': 'openai_official', 'source_id': 'openai', 'profile_id': interface['profile_id'],
                       'interface_id': interface['interface_id'], 'reference_contract_id': reference['contract_id'],
                       'model': model, 'family': 'gpt-image-2', 'transport': 'openai-responses-image',
                       'api_form': 'openai_responses', 'endpoint': responses.ENDPOINT, 'auth_mode': 'bearer',
                       'timeout_sec': domain.REQUEST_TIMEOUT[1] if isinstance(domain.REQUEST_TIMEOUT, tuple) else domain.REQUEST_TIMEOUT}
        plan, _ = prepare_cases_plan({}, public_plan, cases, output_dir=Path(batch) / 'shared')
        plans.append(plan)
    sidecar = {'workflow_batch_schema_version': 1, 'package_sha256': responses.digest(package),
               'plans': plans, 'business_resume_allowed': False, 'historical_records_rewritten': False}
    if write:
        domain.write_json(Path(batch), RESPONSES_WORKFLOW_MARKER, sidecar)
    return sidecar


def execute_responses_batch(package, batch, *, names=None, expectation_policy=responses.CURRENT_EXPECTATION_POLICY,
                            credential=None, cleanup_only=False, run_id=None):
    """Execute a new sidecar batch once; existing business sends are not resumed."""
    from scripts import run_gpt_image_25_responses_reference as domain
    from .. import CancellationContext, validate_plan
    batch = Path(batch)
    marker = batch / RESPONSES_WORKFLOW_MARKER
    if not marker.is_file() or marker.is_symlink():
        raise ValueError('Historical batches are replay-only; prepare a new shared-workflow batch for execution')
    frozen = json.loads(marker.read_text(encoding='utf-8'))
    if (frozen.get('workflow_batch_schema_version') != 1 or frozen.get('package_sha256') != responses.digest(package)
            or not isinstance(frozen.get('plans'), list) or not frozen['plans']):
        raise ValueError('Shared image sidecar does not match the frozen package')
    if frozen != prepare_responses_batch(package, batch, write=False):
        raise ValueError('Shared image sidecar identity or definition differs from its registered factory')
    if expectation_policy != responses.CURRENT_EXPECTATION_POLICY:
        raise ValueError('New shared execution uses the current policy; historical interpretations are replay-only')
    expected_ids = {case['case_id'] for case in package['cases']}
    actual_ids = {case for plan in frozen['plans'] for case in plan['selected_cases']}
    if actual_ids != expected_ids or sum(len(plan['selected_cases']) for plan in frozen['plans']) != len(expected_ids):
        raise ValueError('Shared image plans do not cover the exact frozen cases')
    if cleanup_only and (not run_id or len(frozen['plans']) != 1):
        raise ValueError('Cleanup-only requires the exact run ID and a single-model frozen batch')
    if cleanup_only and names:
        raise ValueError('Cleanup-only restores its original case selection; do not supply new cases')
    execution_plan_path = batch / 'shared_execution_plans.json'
    execution_record = None
    if cleanup_only:
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', run_id):
            raise ValueError('Cleanup run ID is invalid')
        if not execution_plan_path.is_file() or execution_plan_path.is_symlink():
            raise ValueError('Original shared execution plan is missing')
        execution_record = json.loads(execution_plan_path.read_text(encoding='utf-8'))
        if execution_record.get('package_sha256') != responses.digest(package) or len(execution_record.get('plans', [])) != 1:
            raise ValueError('Original execution plan belongs to a different package')
    registries, plans = [], []
    for saved in frozen['plans']:
        registry = _register({}, batch / 'shared')
        validate_plan(saved, registry)
        selection = None
        if cleanup_only:
            original = execution_record['plans'][0]
            validate_plan(original, registry)
            if original['definition'] != saved['definition']:
                raise ValueError('Cleanup execution definition differs from the original factory')
            selection = original['requested_cases']
        if names:
            selection = [key for key in saved['selected_cases'] if key in names or key.rsplit('/', 1)[-1] in names]
            if not selection:
                continue
        plans.append(compile_plan(saved['definition'], registry, selected_cases=selection, run_count=saved['run_count']))
        registries.append(registry)
    known = expected_ids | {case['name'] for case in package['cases']}
    if names and set(names) - known:
        raise ValueError('Case selection is outside the frozen shared batch')
    evidence_root = batch / 'shared_workflow'
    if not cleanup_only and (
        execution_plan_path.exists() or list(batch.glob('case_*.json')) or list(batch.glob('started_*.json'))
        or (evidence_root.exists() and any(evidence_root.rglob('ledger.json')))
    ):
        raise ValueError('Existing execution evidence cannot resume business sends; use a new batch or cleanup-only')
    # All plans and source bindings are checked before credentials are resolved.
    credential = credential or domain.designated_credential()
    if not cleanup_only:
        domain.write_json(batch, 'shared_execution_plans.json', {'package_sha256': responses.digest(package), 'plans': plans})
    cancellation = CancellationContext()
    reports, rows = [], []
    with cancellation.install_signal_handlers():
        for plan, registry in zip(plans, registries):
            model = plan['target']['execution_target']['request_model_id']
            evidence_dir = evidence_root / hashlib.sha256(model.encode()).hexdigest()[:16]
            actual_run_id = run_id or uuid.uuid4().hex
            with _responses_recovery_evidence(batch, plan, evidence_dir, actual_run_id, cleanup_only=cleanup_only) as active_evidence:
                report = execute_plan(plan, registry, make_image_dispatcher({}, plan, credential=credential),
                                      evidence_dir=active_evidence, cancellation=cancellation, cleanup_only=cleanup_only,
                                      run_id=actual_run_id, target=plan['target'])
            reports.append(report)
            rows.extend(result_rows(report))
            if cancellation.cancelled:
                break
    result = {'execution_engine': 'shared_workflow_v1', 'cleanup_only': cleanup_only,
              'recorded_cases': len(rows), 'passed_cases': sum(row.get('overall_pass') is True for row in rows),
              'all_cases_recorded': {row['case_id'] for row in rows} == {key for plan in plans for key in plan['selected_cases']},
              'pass': bool(reports) and all(report['status'] == 'passed' for report in reports),
              'reports': reports, 'records': rows}
    result_name = f'shared_workflow_cleanup_{run_id}_{time.time_ns()}.json' if cleanup_only else 'shared_workflow_result.json'
    domain.write_json(batch, result_name, result)
    return result
