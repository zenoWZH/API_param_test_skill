"""Retained official tool responses: frozen and current verdicts stay separate."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from lib.profile_validation import validate_profile_response
from lib.report_retention import _batch_directory, _read_manifest, _snapshot

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'references/gemini_interactions_budgeted_facts_20260909.json').is_file():
    ROOT = ROOT.parent
FACT = 'references/gemini_interactions_budgeted_facts_20260909.json'
FACT_SHA = 'df3527c813726a42b4a87434c64892cfefc26b4f843fc5a372b407ec576b7aa3'


def retained(link):
    path = ROOT / link['path']
    assert path.parent.parent == ROOT / 'reports/approved_live_20260907'
    if not path.parent.exists():
        pytest.skip('Local P1M evidence batch is unavailable')
    with _batch_directory(path.parent) as fd:
        now = datetime.now(timezone.utc)
        meta = _read_manifest(fd, path.parent, now)
        assert meta['period'] == 'P1M' and meta['expires_at'] == link['expires_at']
        if now >= datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00')):
            pytest.skip('Local evidence has reached P1M expiry')
        owner = next(row for row in meta['owned_files'] if row['path'] == path.name)
        assert _snapshot(fd, path.name) == owner and owner['sha256'] == link['sha256']
        with os.fdopen(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb') as stream:
            raw = stream.read(owner['size'] + 1)
        assert len(raw) == owner['size'] and hashlib.sha256(raw).hexdigest() == link['sha256']
        assert _snapshot(fd, path.name) == owner
    return json.loads(raw)


def replay():
    raw = (ROOT / FACT).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FACT_SHA
    rows = []
    for sample in json.loads(raw)['parameter_observations']:
        if sample['label'] not in ('tools_auto', 'tools_any', 'tools_validated'):
            continue
        links = {Path(link['path']).name.rsplit('_', 1)[-1]: link for link in sample['evidence_links']}
        attempt = retained(links['attempt.json'])
        payload = retained(links['response.txt'])
        original = retained(links['observation.json'])
        case = attempt['case']
        assert case['profile'] == sample['profile'] and case['body']['store'] is False
        assert case['url'] == 'https://generativelanguage.googleapis.com/v1beta/interactions'
        assert 'id' not in payload and payload['status'] == 'requires_action'
        calls = [step for step in payload['steps'] if step['type'] == 'function_call']
        assert len(calls) == 1 and isinstance(calls[0]['id'], str) and calls[0]['id'].strip()
        assert original['observation'] == sample['original_evaluation']
        old = sample['original_evaluation']['runtime_validation_error']
        assert old == 'interaction_id_missing'
        current = validate_profile_response(sample['profile'], payload,
            SimpleNamespace(success=True, usage=payload['usage']), request_body=case['body'],
            transport='gemini_interactions', reference_source=sample['contract_id'],
            request_context={'requested_model': case['body']['model'], 'request_url': case['url']})
        assert current is None
        rows.append({'profile': sample['profile'], 'original_runtime_validation_error': old,
                     'current_runtime_validation_error': current, 'new_http_requests': 0,
                     'response_sha256': sample['response_sha256'],
                     'whole_parameter_effect_matrix_verified': False})
    assert len(rows) == 3
    assert hashlib.sha256((ROOT / FACT).read_bytes()).hexdigest() == FACT_SHA
    return rows


def test_three_official_tool_responses_keep_frozen_error_and_pass_current_validator(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Retained response replay must not issue HTTP')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    replay()
