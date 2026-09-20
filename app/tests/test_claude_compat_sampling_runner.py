import json,hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest,yaml
from lib.client import OpenAICompatibleClient,ChatResult
from scripts.param_test import run_one_profile

ROOT=Path(__file__).resolve().parents[1];REPO=ROOT.parent if ROOT.name=='app' else ROOT
BATCH=REPO/'reports/approved_live_20260907/anthropic_compat_sampling_20260909T120825Z_f2b1a3b0'

@pytest.mark.parametrize('ordinal',[n for n in range(1,73) if (n-1)%8 in (0,4) or n in (8,16,24,56,64)])
def test_formal_sampling_case_matches_actual_body_and_expected_result(monkeypatch,ordinal):
    if not (BATCH/'request_package.json').is_file():pytest.skip('P1M sampling originals unavailable')
    p=json.loads((BATCH/'request_package.json').read_bytes());case=p['cases'][ordinal-1]
    profile={'temperature_one':'claude_temperature','top_p_one':'claude_top_p','both_defaults':'claude_sampling'}[case['label']]
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official';c['providers']['anthropic_official']['models']['default_routes'][case['model']]='vendor_compat';c['compatibility_profiles'][profile]['max_tokens']=4096
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False);monkeypatch.setenv('ANTHROPIC_API_KEY','offline-sampling-runner')
    raw=(BATCH/f'case_{ordinal:02d}_response.bin').read_bytes();record=json.loads((BATCH/f'case_{ordinal:02d}_observation.json').read_bytes());assert hashlib.sha256(raw).hexdigest()==record['response_sha256'];value=json.loads(raw)
    client=OpenAICompatibleClient.from_config(c,provider='anthropic_official');client.session.post=Mock(return_value=SimpleNamespace(status_code=record['status_code'],headers={},content=raw,text=raw.decode(),json=lambda:value));client.count_tokens=Mock(return_value=None)
    result=run_one_profile(c,client,'anthropic_official',case['model'],'claude','claude_openai_compat','claude',profile,1,{'id':'offline','prompt':case['body']['messages'][0]['content']})
    assert result['pass'] is True
    assert client.session.post.call_count==1 and client.session.post.call_args.kwargs['json']==case['body']

@pytest.mark.parametrize('profile,message,param,status,expected',[
 ('claude_top_p','`top_p` is deprecated for this model.',None,400,True),
 ('claude_top_p','max_tokens must be positive',None,400,False),
 ('claude_top_p','`top_p` is deprecated for this model.','temperature',400,False),
 ('claude_sampling','`temperature` and `top_p` cannot both be specified for this model. Please use only one.',None,400,True),
 ('claude_sampling','invalid model',None,400,False),
 ('claude_top_p','`top_p` is deprecated for this model.',None,403,False)])
def test_unrelated_rejection_cannot_pass_sampling_case(monkeypatch,profile,message,param,status,expected):
    model='claude-opus-4-7';c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official';c['providers']['anthropic_official']['models']['default_routes'][model]='vendor_compat'
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    class Offline:
        def chat_completion(self,body):
            p={'error':{'message':message,'param':param}}
            return ChatResult(success=False,status_code=status,latency_ms=1,timestamp=0,response_json=p,raw_text=json.dumps(p),error_type='http_error')
    r=run_one_profile(c,Offline(),'anthropic_official',model,'claude','claude_openai_compat','claude',profile,1,{'id':'offline','prompt':'Reply OK.'})
    assert r['pass'] is expected
