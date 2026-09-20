"""Prepare or explicitly dispatch six new M2.5 JSON stream controls once.

The stopped parent's punctuation mismatch is retained unchanged. These are six
new requests with a JSON semantic fixture, never a resumption of its five unused
text controls. The CLI prepares a fresh shared workflow from the public baseline;
private historical replay is available only through the separate legacy helpers.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'packages/model-profile-db')]
import requests
from lib.credential_security import ProviderCredential
from lib.report_retention import DEFAULT_REPORT_ROOT, initialize_report_retention, _batch_directory, _read_manifest, _snapshot
from scripts import run_kimi_minimax_stream_reference as parent
from scripts.prepare_zai_general_reference import Reports, canonical_bytes, digest, read_frozen_package, response_deadline, strict_json_loads, utc_now

PARENT_BATCH = 'kimi_minimax_stream_20260908T183226Z_235d9088'
PARENT_PACKAGE_SHA256 = '728d3bf11d145a764be8fe02cbd99f2dd01c65efda1e4a551c0a50afcdeffd5a'
PARENT_FILES = {
    'request_package.json': '13d6cc0ee7a3ccdd31c7f17aae54307bd0d34488a63678d0050e776386326fe1',
    'dispatch_started.json': '2716893433794e2359902cec530ee6eeb76aeaf1c4bad48c1188ff4da6974f8d',
    'dispatch_finished.json': '78d3d04afc54821f504944d80b08c503ced3c2dbe107798711987168dd126461',
    'case_28_attempt.json': '7afa46f9225a7f94d3ae9406a59e72e5481797b8ba4ee6e12486f4d201ac6294',
    'case_28_observation.json': 'a09a15ef467d4443c102b8c28d993f62ce180d80ee5859be99379a8c9d464c96',
    'case_28_response.bin': 'c38cd65854e290e4d7e12f1d4f148a0a7f65c6e6d70cf5660c3c0722b42b7cd4',
    'summary.json': '4838db7e5906772e56cc15e05e9adf29eb75de9d202328b7005dc01a9fc97a9d',
}
MODEL, PROVIDER, TARGET = 'MiniMax-M2.5', 'minimax_official', 'minimax_m25'
NAMESPACE = 'minimax_m25_json_stream_20260908'
URL = 'https://api.minimax.io/v1/chat/completions'
LABELS = ('nonstream', *parent.LABELS)
REQUEST_CAP, TIMEOUT, MAX_BYTES = 6, 150, 2 * 1024 * 1024
PROMPT = 'Return exactly one JSON object with exactly these two keys and values: marker is the string ALPHA, and answer is the integer 2. Do not include Markdown fences or any other text.'
EXPECTATION = {'kind': 'json', 'value': {'marker': 'ALPHA', 'answer': 2}}
POLICY = {'approval_code': 'R7-ALL-MODELS-EFFECT-PROOF', 'request_cap': 6, 'concurrency': 1, 'retries': 0,
    'output_cap': 1024, 'output_limit_field': 'max_completion_tokens', 'reasoning_split': True,
    'timeout_seconds': TIMEOUT, 'response_byte_cap': MAX_BYTES, 'allow_redirects': False,
    'report_retention': 'P1M', 'api_total_cost_cap': None, 'tools': False, 'media': False, 'state_resources': False,
    'pressure': False, 'response_format_parameter_added': False, 'thinking_switch_added': False,
    'new_baseline_failure_stops_batch': True, 'parent_requests_retried': False, 'parent_unused_controls_resumed': False,
    'parent_false_verdict_preserved': True, 'automatic_retry_or_output_growth': False,
    'type_rejection_assumed': False, 'usage_absence_assumed_for_false_or_omitted': False,
    'full_parameter_matrix_verified': False}
CODE_FILES = ('scripts/run_minimax_m25_json_stream_reference.py', *parent.CODE_FILES)


def prior(*, root=ROOT):
    """Verify the stopped parent and its last false without relabeling it."""
    root = Path(root)
    batch = root / 'reports/approved_live_20260907' / PARENT_BATCH
    with _batch_directory(batch) as fd:
        now = datetime.now(timezone.utc)
        meta = _read_manifest(fd, batch, now)
        expires = datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00'))
        if meta.get('period') != 'P1M' or not now < expires:
            raise ValueError('Parent evidence is outside its approved P1M retention')
        owned = {r['path']: r for r in meta['owned_files']}
        expected_files = {'request_package.json', 'dispatch_started.json', 'dispatch_finished.json', 'summary.json'} | {
            f'case_{n:02d}_{suffix}' for n in range(1, 29) for suffix in ('attempt.json', 'observation.json', 'response.bin')}
        if len(owned) != 88 or set(owned) != expected_files or set(os.listdir(fd)) != expected_files | {'.report-retention.json'}:
            raise ValueError('Parent must retain exactly 28 attempts; its five unused controls must remain unattempted')
        files = {}
        for name in sorted(owned):
            if _snapshot(fd, name) != owned[name]: raise ValueError('Parent retained ownership changed')
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), 'rb') as stream:
                raw = stream.read(owned[name]['size'] + 1)
            if (_snapshot(fd, name) != owned[name] or len(raw) != owned[name]['size']
                    or hashlib.sha256(raw).hexdigest() != owned[name]['sha256']
                    or name in PARENT_FILES and hashlib.sha256(raw).hexdigest() != PARENT_FILES[name]):
                raise ValueError('Parent evidence bytes changed')
            files[name] = raw
    plan = strict_json_loads(files['request_package.json'])
    if digest(plan) != PARENT_PACKAGE_SHA256 or files['request_package.json'] != canonical_bytes(plan) + b'\n':
        raise ValueError('Stopped parent package changed')
    for name in ('scripts/run_kimi_minimax_stream_reference.py', 'scripts/kimi_minimax_stream_evidence.py',
                 'scripts/prepare_zai_general_reference.py', 'lib/report_retention.py'):
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != plan['code_sha256'][name]:
            raise ValueError('Parent pure replay dependency changed')
    start, end = (strict_json_loads(files[name]) for name in ('dispatch_started.json', 'dispatch_finished.json'))
    summary = strict_json_loads(files['summary.json'])
    if (start.get('request_package_sha256') != PARENT_PACKAGE_SHA256
            or end.get('requests_sent') != 28 or end.get('terminal_state') != 'stopped_early'
            or summary.get('requests_sent') != 28 or summary.get('planned_requests') != 33
            or summary.get('all_planned_requests_dispatched') is not False
            or len(summary.get('observations', [])) != 28):
        raise ValueError('Parent terminal dispatch ledger changed')
    for n, case in enumerate(plan['cases'][:28], 1):
        row = strict_json_loads(files[f'case_{n:02d}_observation.json'])
        attempt = strict_json_loads(files[f'case_{n:02d}_attempt.json'])
        if (canonical_bytes(attempt.get('case')) != canonical_bytes(case) or attempt.get('ordinal') != n
                or row.get('ordinal') != n or row.get('client_entered') is not True
                or canonical_bytes(row) != canonical_bytes(summary['observations'][n - 1])
                or any(canonical_bytes(row.get(k)) != canonical_bytes(case[k]) for k in parent.CASE_FIELDS)
                or row.get('response_sha256') != hashlib.sha256(files[f'case_{n:02d}_response.bin']).hexdigest()
                or row.get('stop_batch') is not (n == 28)):
            raise ValueError('Parent request prefix or original verdict ledger changed')
    old_case = plan['cases'][27]
    old_record = strict_json_loads(files['case_28_observation.json'])
    raw = files['case_28_response.bin']
    replay = parent.observe(old_case, {**old_record, 'response_bytes': raw})
    if (old_case['case_id'] != TARGET + '/nonstream' or old_case['model'] != MODEL
            or old_case['expectation'] != {'kind': 'text', 'value': 'OK TOKEN CHECK COMPLETE.'}
            or old_record.get('response_complete') is not True or old_record.get('failure_type') is not None
            or old_record['response_json']['choices'][0]['message']['content'] != 'OK TOKEN CHECK COMPLETE'
            or canonical_bytes(replay) != canonical_bytes(old_record['verdict'])
            or replay['semantic_observation'].get('pass') is not False
            or replay['semantic_observation'].get('diagnostics') != ['semantic_mismatch']
            or replay['semantic_observation'].get('usage_verified') is not True):
        raise ValueError('Parent punctuation-only semantic failure cannot be reproduced')
    row = next(r for r in plan['targets'] if r['target_key'] == TARGET)
    return row, {'batch': str(batch.relative_to(root)), 'package_sha256': PARENT_PACKAGE_SHA256,
        'requests_sent': 28, 'planned_requests': 33, 'terminal_state': 'stopped_early',
        'unused_parent_case_ids': [c['case_id'] for c in plan['cases'][28:]], 'unused_requests': 5,
        'old_case_id': old_case['case_id'], 'old_request_body_sha256': old_case['body_sha256'],
        'old_response_sha256': hashlib.sha256(raw).hexdigest(), 'old_verdict': copy.deepcopy(replay),
        'old_verdict_sha256': digest(replay), 'all_88_owned_files_verified': True,
        'artifacts': {name: {'path': str((batch / name).relative_to(root)), 'sha256': sha} for name, sha in PARENT_FILES.items()},
        'raw_report_retention': 'P1M', 'raw_report_expires_at': meta['expires_at'],
        'old_failure_reclassified': False, 'parent_execution_resumed': False}


def target():
    old, attribution = prior()
    row = {k: copy.deepcopy(old[k]) for k in ('source_id', 'provider_id', 'request_model_id', 'selection',
        'output_cap', 'endpoint', 'auth_mode', 'requires_new_nonstream_bytes_baseline')}
    body = copy.deepcopy(old['baseline_body'])
    body['messages'] = [{'role': 'user', 'content': PROMPT}]
    row.update(target_key=TARGET, baseline_body=body, baseline_body_sha256=digest(body), expectation=copy.deepcopy(EXPECTATION))
    unchanged = copy.deepcopy(body); unchanged['messages'] = copy.deepcopy(old['baseline_body']['messages'])
    if canonical_bytes(unchanged) != canonical_bytes(old['baseline_body']) or set(body) != {'model', 'messages', 'max_completion_tokens', 'reasoning_split', 'stream'}:
        raise ValueError('New M2.5 fixture may change only the user message')
    return row, attribution


def build_package(*, created_at=None):
    timestamp = created_at or utc_now()
    if type(timestamp) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', timestamp):
        raise ValueError('Package timestamp must be UTC seconds')
    datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    row, attribution = target()
    snapshot = parent.binding(row)
    cases = []
    for label in LABELS:
        case = parent.case_for(row, label, snapshot)
        case['case_id'] = NAMESPACE + '/' + label
        cases.append(case)
    return {'schema_version': 1, 'status': 'prepared_only', 'created_at': timestamp, 'requests_sent': 0,
        'policy': copy.deepcopy(POLICY), 'parent_termination': attribution, 'target': row, 'snapshot': snapshot,
        'official_document_review': copy.deepcopy(parent.reference()['official_document_review']['minimax']),
        'cases': cases, 'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES}}


def validate_package(package):
    if type(package) is not dict or type(package.get('created_at')) is not str:
        raise ValueError('Invalid M2.5 JSON stream package')
    expected = build_package(created_at=package['created_at'])
    if canonical_bytes(package) != canonical_bytes(expected) or len(package.get('cases', [])) != 6:
        raise ValueError('M2.5 JSON package source, snapshot, body, policy, parent or code changed')


def prepare(*, root=DEFAULT_REPORT_ROOT):
    package = build_package()
    batch = Path(root).absolute() / ('minimax_m25_json_stream_' + package['created_at'].replace('-', '').replace(':', '') + '_' + uuid.uuid4().hex[:8])
    initialize_report_retention(batch, root=root)
    Reports(batch, root=root).json('request_package.json', package)
    return batch, package


def send_one(session, package, case, key, reports, record, *, positive_control=False):
    validate_package(package)
    ordinal = record['ordinal']
    if type(ordinal) is not int or not 1 <= ordinal <= 6 or canonical_bytes(case) != canonical_bytes(package['cases'][ordinal - 1]):
        raise ValueError('Outbound M2.5 request is not the exact next frozen case')
    if not isinstance(key, ProviderCredential) or key.provider != PROVIDER or key.allowed_origins != frozenset({'https://api.minimax.io'}):
        raise ValueError('M2.5 credential origin or provider changed')
    record.update({k: copy.deepcopy(case[k]) for k in parent.CASE_FIELDS})
    record.update(client_entered=False, response_complete=False, status_code=None, failure_type=None)
    prefix = f'case_{ordinal:02d}'
    reports.json(prefix + '_attempt.json', {'ordinal': ordinal, 'created_at': utc_now(), 'case': case,
        'request_package_sha256': digest(package), 'client_entry_pending': True})
    raw, response, chunks, received, started = bytearray(), None, [], 0, time.monotonic()
    try:
        with response_deadline(TIMEOUT):
            headers = key.auth_headers(url=URL, auth_mode='bearer')
            record['client_entered'] = True
            response = session.request('POST', URL, headers=headers, data=canonical_bytes(case['body']),
                timeout=(15, TIMEOUT), stream=True, allow_redirects=False)
            record.update(status_code=response.status_code, content_type=response.headers.get('Content-Type', ''),
                          content_encoding=response.headers.get('Content-Encoding'))
            for chunk in response.iter_content(chunk_size=1024):
                if type(chunk) is not bytes: raise ValueError('Response chunk is not bytes')
                received += len(chunk)
                if len(raw) + len(chunk) > MAX_BYTES:
                    raw.extend(chunk[:MAX_BYTES - len(raw)]);record['response_byte_cap_exceeded'] = True
                    raise ValueError('Response byte cap exceeded')
                raw.extend(chunk)
                if chunk: chunks.append({'end_byte': len(raw), 'elapsed_seconds': round(time.monotonic() - started, 6)})
            record['response_complete'] = True
            raw.decode('utf-8')
            if record['content_type'].split(';')[0].strip().lower() == 'text/event-stream':
                record['parsed_events'] = parent.evidence.parse_frames(bytes(raw))
            else:
                record['response_json'] = strict_json_loads(bytes(raw))
    except (Exception, KeyboardInterrupt) as exc:
        record.update(failure_type=type(exc).__name__, interrupted=isinstance(exc, KeyboardInterrupt))
    finally:
        if response is not None:
            try: response.close()
            except (Exception, KeyboardInterrupt) as exc:
                record['failure_type'] = record['failure_type'] or type(exc).__name__
    record['response_bytes'] = bytes(raw)
    record['verdict'] = parent.observe(case, record, positive_control=positive_control)
    record.pop('response_bytes')
    record.update(elapsed_seconds=round(time.monotonic() - started, 3), response_bytes_received=received,
        response_bytes_captured=len(raw), transport_chunks=chunks, raw_byte_scope='requests_iter_content_decoded_http_entity',
        response_sha256_before_redaction=hashlib.sha256(raw).hexdigest())
    status = record['status_code']
    sse_error = any(parent.terminal_error(frame.get('data')) for frame in record.get('parsed_events', []) if type(frame) is dict)
    native = record['verdict'].get('semantic_observation') or {}
    record['stop_batch'] = bool(record['failure_type'] or type(status) is not int or status in (401, 402, 403, 404, 408, 429)
        or type(status) is int and (300 <= status < 400 or status >= 500)
        or parent.terminal_error(record.get('response_json')) or sse_error
        or case['label'] == 'nonstream' and not (native.get('pass') is True and native.get('usage_verified') is True))
    retained_raw = key.redact(bytes(raw).decode('latin-1')).encode('latin-1')
    record.update(raw_redacted=retained_raw != bytes(raw), response_raw_file=prefix + '_response.bin',
                  response_sha256=hashlib.sha256(retained_raw).hexdigest())
    reports.bytes(record['response_raw_file'], retained_raw)
    public = key.redact(record);record.clear();record.update(public)
    reports.json(prefix + '_observation.json', record)


def summarize(package, records, *, fatal_error=None):
    if len(records) > 6 or any(r.get('ordinal') != n for n, r in enumerate(records, 1)):
        raise ValueError('M2.5 JSON dispatch ledger expanded or reordered')
    sent = sum(r.get('client_entered') is True for r in records)
    complete = sent == len(records) == 6
    stopped = bool(fatal_error or any(r.get('stop_batch') for r in records))
    comparisons = [r for r in parent.usage_comparisons(package, records) if r['target_key'] == TARGET]
    return {'requests_sent': sent, 'planned_requests': 6, 'all_planned_requests_dispatched': complete,
        'terminal_state': 'stopped_early' if stopped else 'completed' if complete else 'incomplete',
        'fatal_error_type': fatal_error, 'request_package_sha256': digest(package), 'report_retention': 'P1M',
        'api_total_cost_cap': None, 'parent_requests_retried': False, 'parent_unused_controls_resumed': False,
        'parent_false_verdict_preserved': True, 'full_parameter_matrix_verified': False,
        'stream_semantics_verified': sum(r.get('verdict', {}).get('stream_semantic_verified') is True for r in records),
        'field_type_rejections_verified': sum(r.get('verdict', {}).get('field_type_rejection_verified') is True for r in records),
        'http_counts': {str(s): sum(r.get('status_code') == s for r in records) for s in sorted({r.get('status_code') for r in records}, key=str)},
        'usage_controls': comparisons, 'observations': records}


def execute(batch, *, root=DEFAULT_REPORT_ROOT, credential_factory=None, session_factory=None):
    batch = Path(batch).absolute()
    if batch.parent != Path(root).absolute() or not batch.name.startswith('minimax_m25_json_stream_'):
        raise ValueError('Wrong independent M2.5 JSON batch')
    package = read_frozen_package(batch, root=root)
    validate_package(package)
    reports = Reports(batch, root=root);reports.require_fresh()
    key = (credential_factory or parent.credential)(PROVIDER)
    reports.json('dispatch_started.json', {'created_at': utc_now(), 'request_package_sha256': digest(package), 'request_cap': 6}, claim=True)
    records, fatal = [], None
    try:
        with (session_factory or requests.Session)() as session:
            session.trust_env = False;session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
            for ordinal, case in enumerate(package['cases'], 1):
                saved = read_frozen_package(batch, root=root)
                if canonical_bytes(saved) != canonical_bytes(package): raise ValueError('Saved M2.5 JSON package changed')
                positive_label = 'usage_on' if case['label'] == 'invalid_usage_type' else 'stream_default'
                positive = next((r['verdict'].get('stream_semantic_verified') is True for r in records if r.get('label') == positive_label), False)
                record = {'ordinal': ordinal};records.append(record)
                send_one(session, package, case, key, reports, record, positive_control=positive)
                print(json.dumps({'ordinal': ordinal, 'case_id': case['case_id'], 'http': record['status_code'],
                    'outcome': record['verdict']['outcome'], 'usage_verified': record['verdict']['usage_verified']}), flush=True)
                if record['stop_batch']: break
    except (Exception, KeyboardInterrupt) as exc:
        fatal = type(exc).__name__
    finally:
        result = key.redact(summarize(package, records, fatal_error=fatal))
        reports.json('summary.json', result)
        reports.json('dispatch_finished.json', {'created_at': utc_now(), 'requests_sent': result['requests_sent'], 'terminal_state': result['terminal_state']})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', type=Path)
    args = parser.parse_args()
    from lib.test_runner.adapters.kimi_minimax import execute_shared_batch, prepare_shared_batch
    if args.execute:
        result = execute_shared_batch(args.execute)
        print(json.dumps({k: result[k] for k in ('requests_sent', 'terminal_state', 'stream_semantics_verified', 'field_type_rejections_verified', 'pass')}))
        return 0 if result['pass'] else 1
    batch, package = prepare_shared_batch(target_keys=[TARGET], variant='json')
    print(json.dumps({'batch': str(batch), 'request_package_sha256': digest(package), 'requests_sent': 0,
                      'planned_requests': package['planned_requests'], 'historical_baseline_replayed': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
