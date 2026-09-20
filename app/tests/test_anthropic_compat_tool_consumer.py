"""Replay all 77 official tool responses through each consumer's actual client."""
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from lib.client import OpenAICompatibleClient

CONSUMER_ROOT = Path(__file__).resolve().parents[1]
ROOT = CONSUMER_ROOT.parent if CONSUMER_ROOT.name == 'app' else CONSUMER_ROOT
BATCH = ROOT / 'reports/approved_live_20260907/anthropic_compat_tools_20260909T073729Z_f0b4db68'
PACKAGE_SHA = '157361eec8cf6f2b16ddc470142462f7ce611741ae7a4168113ce12d9fef12af'


@pytest.fixture(scope='module')
def package():
    path = BATCH / 'request_package.json'
    if not path.is_file(): pytest.skip('P1M original tool responses unavailable')
    value = json.loads(path.read_text())
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == PACKAGE_SHA
    return value


class Response:
    def __init__(self, status, raw):
        self.status_code = status; self.content = raw; self.text = raw.decode()
        self.headers = {'content-type': 'application/json'}
    def json(self): return json.loads(self.text)
    def close(self): pass


@pytest.mark.parametrize('ordinal', range(1, 78))
def test_same_original_wire_and_tool_envelope_through_actual_client(package, monkeypatch, ordinal):
    case = package['cases'][ordinal - 1]
    raw = (BATCH / f'case_{ordinal:02d}_response.bin').read_bytes()
    observation = json.loads((BATCH / f'case_{ordinal:02d}_observation.json').read_text())
    assert hashlib.sha256(raw).hexdigest() == observation['response_sha256']
    payload = json.loads(raw)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'offline-compatible-tool-consumer')
    monkeypatch.delenv('LOADTEST_PROVIDER', raising=False)
    monkeypatch.delenv('LOADTEST_MODEL', raising=False)
    config = yaml.safe_load((CONSUMER_ROOT / 'config.yaml').read_text())
    config['active_provider'] = 'anthropic_official'
    client = OpenAICompatibleClient.from_config(config, provider='anthropic_official')
    client.session.post = Mock(return_value=Response(observation['status_code'], raw))
    result = client.chat_completion(case['body'])
    assert result.status_code == observation['status_code']
    assert result.response_json == payload
    if observation['status_code'] == 200:
        assert result.response_json['model'] == case['model']
        assert result.finish_reason == payload['choices'][0]['finish_reason']
        assert result.usage == payload['usage']
        assert result.tool_calls == (payload['choices'][0]['message'].get('tool_calls') or [])
        assert result.text == (payload['choices'][0]['message'].get('content') or '')
    else:
        assert not result.success
    client.session.post.assert_called_once()
    args, kwargs = client.session.post.call_args
    assert args == ('https://api.anthropic.com/v1/chat/completions',)
    assert kwargs['json'] == case['body'] and kwargs['allow_redirects'] is False
