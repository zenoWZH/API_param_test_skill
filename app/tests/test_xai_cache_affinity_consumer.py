import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from lib.client import OpenAICompatibleClient
from lib.credential_security import validate_profile_request_headers

CONSUMER_ROOT=Path(__file__).resolve().parents[1]
ROOT=CONSUMER_ROOT.parent if CONSUMER_ROOT.name=='app' else CONSUMER_ROOT
BATCH=ROOT/'reports/approved_live_20260907/xai_cache_affinity_20260909T094025Z_5599a09c'
PACKAGE_SHA='506c793a42bc8c490c7357860c54a822b480b6ca9d8c7234246c2cb46128ec37'


@pytest.fixture(scope='module')
def package():
    path=BATCH/'request_package.json'
    if not path.is_file():pytest.skip('P1M original affinity responses unavailable')
    p=json.loads(path.read_bytes());raw=json.dumps(p,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    assert hashlib.sha256(raw).hexdigest()==PACKAGE_SHA
    return p


class Response:
    def __init__(self,status,raw):self.status_code=status;self.content=raw;self.text=raw.decode();self.headers={'content-type':'application/json'}
    def json(self):return json.loads(self.text)
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def iter_lines(self,decode_unicode=True):return iter(self.text.splitlines())


def client(monkeypatch,provider='xai_official',base='https://api.x.ai/v1'):
    monkeypatch.setenv('XAI_API_KEY','offline-xai-affinity-client')
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    if provider=='xai_official' and base=='https://api.x.ai/v1':
        config=yaml.safe_load((CONSUMER_ROOT/'config.yaml').read_bytes());return OpenAICompatibleClient.from_config(config,provider=provider)
    return OpenAICompatibleClient(base,'offline-key-for-test',provider=provider)


@pytest.mark.parametrize('ordinal',range(1,45))
def test_original_affinity_wire_and_response_through_actual_client(package,monkeypatch,ordinal):
    case=package['cases'][ordinal-1];raw=(BATCH/f'case_{ordinal:02d}_response.bin').read_bytes();old=json.loads((BATCH/f'case_{ordinal:02d}_observation.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest()==old['response_sha256']
    c=client(monkeypatch);c.session.post=Mock(return_value=Response(old['status_code'],raw))
    result=c.chat_completion(case['request'],cache_affinity_key=case['extra_headers'].get('x-grok-conv-id')) if case['form']=='chat' else c.openai_responses(case['request'])
    assert result.status_code==old['status_code'] and result.response_json==json.loads(raw)
    assert result.success is (old['status_code']==200)
    c.session.post.assert_called_once();args,kwargs=c.session.post.call_args
    assert args==(case['endpoint'],) and kwargs['json']==case['request'] and kwargs['allow_redirects'] is False
    assert kwargs['headers'].get('x-grok-conv-id')==case['extra_headers'].get('x-grok-conv-id')
    assert 'x-grok-conv-id' not in c.session.headers


@pytest.mark.parametrize('provider,base',[('foreign','https://api.x.ai/v1'),('xai_official','https://example.invalid/v1')])
@pytest.mark.parametrize('stream',[False,True])
def test_affinity_key_cannot_cross_provider_or_destination(monkeypatch,provider,base,stream):
    c=client(monkeypatch,provider,base);c.session.post=Mock()
    with pytest.raises(ValueError):c.chat_completion({'model':'grok-4.5','stream':stream},cache_affinity_key='approved_safe')
    assert not c.session.post.called


@pytest.mark.parametrize('key',['',True,{},'line\r\ninjection','contains space','非ASCII'])
def test_affinity_identifier_rejected_before_http(monkeypatch,key):
    c=client(monkeypatch);c.session.post=Mock()
    with pytest.raises(ValueError):c.chat_completion({'model':'grok-4.5','stream':False},cache_affinity_key=key)
    assert not c.session.post.called


def test_general_profile_header_allowlist_remains_closed():
    with pytest.raises(ValueError):validate_profile_request_headers({'x-grok-conv-id':'approved_safe'})


def test_request_scoped_key_works_for_stream_and_does_not_persist(monkeypatch):
    chunk={'id':'offline','created':1,'object':'chat.completion.chunk','model':'grok-4.5','choices':[{'index':0,'delta':{'role':'assistant','content':'ACK'},'finish_reason':'stop'}]}
    raw=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
    c=client(monkeypatch);c.session.post=Mock(return_value=Response(200,raw))
    result=c.chat_completion({'model':'grok-4.5','stream':True},cache_affinity_key='approved_safe')
    assert result.success and result.text=='ACK'
    assert c.session.post.call_args.kwargs['headers']['x-grok-conv-id']=='approved_safe'
    c.chat_completion({'model':'grok-4.5','stream':True})
    assert 'x-grok-conv-id' not in c.session.post.call_args.kwargs['headers']
    assert 'x-grok-conv-id' not in c.session.headers
