import copy
import json
from pathlib import Path
import pytest
import yaml
from lib.client import ChatResult
from scripts.param_test import run_one_profile

ROOT=Path(__file__).resolve().parents[1]
MODEL='claude-opus-4-7'
EXACT='Adaptive thinking is not available via the OpenAI compatibility endpoint.'


@pytest.mark.parametrize('message,parameter,status,expected',[ (EXACT,None,400,True),
    ('max_tokens must be positive',None,400,False),(EXACT,'max_tokens',400,False),
    ('Invalid thinking.budget_tokens',None,400,False),(EXACT,None,403,False)])
def test_formal_runner_requires_exact_adaptive_rejection(monkeypatch,message,parameter,status,expected):
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official'
    c['providers']['anthropic_official']['models']['default_routes'][MODEL]='vendor_compat'
    class Offline:
        calls=0
        def chat_completion(self,body):
            self.calls+=1
            assert body['thinking']=={'type':'adaptive'} and 'extra_body' not in body
            p={'error':{'message':message,'param':parameter}}
            return ChatResult(success=False,status_code=status,latency_ms=1,timestamp=0,response_json=p,raw_text=json.dumps(p),error_type='http_error')
    client=Offline()
    result=run_one_profile(c,client,'anthropic_official',MODEL,'claude','claude_openai_compat','claude',
        'claude_thinking_adaptive',1,{'id':'offline','prompt':'Return JSON answer 42.'})
    assert client.calls==1 and result['pass'] is expected
    if status==400 and not expected:assert result['failure_classification']=='claude_compat_adaptive_rejection_unattributed'


@pytest.mark.parametrize('ordinal',[n for n in range(1,55) if (n-1)%6 in (1,2,3) and not 32<=n<=36])
def test_formal_runner_matches_saved_mode_request_and_outcome(monkeypatch,ordinal):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from lib.client import OpenAICompatibleClient
    repo=ROOT.parent if ROOT.name=='app' else ROOT
    batch=repo/'reports/approved_live_20260907/anthropic_thinking_wire_20260909T112251Z_2625f24b'
    if not (batch/'request_package.json').is_file():pytest.skip('P1M mode originals unavailable')
    package=json.loads((batch/'request_package.json').read_bytes());case=package['cases'][ordinal-1]
    profile={'disabled':'claude_thinking_disabled','enabled_budget':'claude_thinking_budget','adaptive':'claude_thinking_adaptive'}[case['label']]
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official'
    c['providers']['anthropic_official']['models']['default_routes'][case['model']]='vendor_compat'
    c['compatibility_profiles'][profile]['max_tokens']=4096
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-complete-mode-runner')
    raw=(batch/f'case_{ordinal:02d}_response.bin').read_bytes();value=json.loads(raw)
    record=json.loads((batch/f'case_{ordinal:02d}_observation.json').read_bytes())
    import hashlib
    assert hashlib.sha256(raw).hexdigest()==record['response_sha256']
    client=OpenAICompatibleClient.from_config(c,provider='anthropic_official')
    client.session.post=Mock(return_value=SimpleNamespace(status_code=record['status_code'],headers={},content=raw,text=raw.decode(),json=lambda:value))
    client.count_tokens=Mock(return_value=None)
    result=run_one_profile(c,client,'anthropic_official',case['model'],'claude','claude_openai_compat','claude',profile,1,
        {'id':'offline','prompt':case['body']['messages'][0]['content']})
    assert result['pass'] is True
    assert client.session.post.call_count==1 and client.session.post.call_args.kwargs['json']==case['body']
