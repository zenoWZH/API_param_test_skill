import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import yaml
from lib.client import OpenAICompatibleClient

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parent if ROOT.name=='app' else ROOT
BATCH=REPO/'reports/approved_live_20260907/anthropic_thinking_wire_20260909T112251Z_2625f24b'
SHA='3effc083437a1619f05da83ddec989cd6f9011cf8785ea69cdd3913ab4ac0ce9'


@pytest.fixture(scope='module')
def package():
    if not (BATCH/'request_package.json').is_file():pytest.skip('P1M thinking originals unavailable')
    p=json.loads((BATCH/'request_package.json').read_bytes());raw=json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    assert hashlib.sha256(raw).hexdigest()==SHA;return p


@pytest.mark.parametrize('ordinal',[n for n in range(1,55) if not 32<=n<=36])
def test_original_thinking_body_and_response_through_client(package,monkeypatch,ordinal):
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-thinking-consumer')
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official'
    case=package['cases'][ordinal-1];raw=(BATCH/f'case_{ordinal:02d}_response.bin').read_bytes();record=json.loads((BATCH/f'case_{ordinal:02d}_observation.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest()==record['response_sha256']
    response=json.loads(raw);fake=SimpleNamespace(status_code=record['status_code'],headers={'content-type':'application/json'},content=raw,text=raw.decode(),json=lambda:response)
    client=OpenAICompatibleClient.from_config(c,provider='anthropic_official');client.session.post=Mock(return_value=fake)
    result=client.chat_completion(case['body'])
    assert result.status_code==record['status_code'] and result.response_json==response
    args,kwargs=client.session.post.call_args
    assert args==('https://api.anthropic.com/v1/chat/completions',) and kwargs['json']==case['body'] and kwargs['allow_redirects'] is False
    if record['status_code']==200:
        assert result.usage==response['usage'] and result.text==response['choices'][0]['message']['content']
