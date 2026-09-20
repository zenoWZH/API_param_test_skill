import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import yaml
from lib.client import OpenAICompatibleClient

ROOT=Path(__file__).resolve().parents[1]
if ROOT.name=='app':ROOT=ROOT.parent
BATCH=ROOT/'reports/approved_live_20260907/anthropic_compat_stream_20260909T065309Z_d095bf45'
PACKAGE_SHA='912ee2a4a1356555b23a732eb02febbf0e93bcddeb350f48b6f899f55e55601b'


@pytest.fixture(scope='module')
def package():
    path=BATCH/'request_package.json'
    if not path.is_file():pytest.skip('P1M stream original unavailable')
    value=json.loads(path.read_text());canonical=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    assert hashlib.sha256(canonical).hexdigest()==PACKAGE_SHA
    return value


class Response:
    def __init__(self,raw):self.raw=raw;self.status_code=200;self.headers={'content-type':'text/event-stream'};self.content=raw;self.text=raw.decode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_lines(self,decode_unicode=True):return iter(self.text.splitlines())


@pytest.mark.parametrize('ordinal',[n for n in range(1,51) if (n-1)%5<3])
def test_same_original_wire_through_actual_client(package,monkeypatch,ordinal):
    case=package['cases'][ordinal-1];raw=(BATCH/f'case_{ordinal:02d}_response.bin').read_bytes()
    original=json.loads((BATCH/f'case_{ordinal:02d}_observation.json').read_text())
    assert hashlib.sha256(raw).hexdigest()==original['response_sha256']
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-compatible-stream-consumer')
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    config=yaml.safe_load((ROOT/'config.yaml').read_text());config['active_provider']='anthropic_official'
    client=OpenAICompatibleClient.from_config(config,provider='anthropic_official')
    client.session.post=Mock(return_value=Response(raw))
    result=client.chat_completion(case['body'])
    assert result.success and result.error_type is None and result.finish_reason=='stop'
    assert json.loads(result.text)=={'color':'blue','count':2}
    assert result.response_json['model']==case['model']
    if case['label']=='usage_true':
        assert result.usage==original['verdict']['final_reported_usage']['native_usage']
    else:assert not result.usage
    args,kw=client.session.post.call_args
    assert args==('https://api.anthropic.com/v1/chat/completions',) and kw['json']==case['body']
    assert kw['stream'] is True and kw['allow_redirects'] is False
