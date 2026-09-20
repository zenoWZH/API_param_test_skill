import copy
import json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
import yaml
from lib.deepseek_params import build_request
from lib.client import OpenAICompatibleClient

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parent if ROOT.name=='app' else ROOT
MODEL_CASES=[('claude-opus-5','claude','claude_openai_compat'),('claude-fable-5','claude_fable','claude_fable_openai_compat')]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-output-alias-consumer')
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official';return c


def build(c,selection,overrides,*,parameter=False,preserve=False,route='vendor_compat',contract=None):
    model,family,default=selection
    return build_request(c,'compatibility_profiles','claude_sampling',overrides={'model':model,'preserve_rejected_params':preserve,**overrides},
        model_family_override=family,api_form_override='openai_chat_completions',route_profile_override=route,reference_source=contract or default,
        enforce_model_capabilities=False,parameter_test=parameter)


@pytest.mark.parametrize('selection',MODEL_CASES)
def test_explicit_alias_replaces_only_inherited_budget(config,selection):
    before=copy.deepcopy(config)
    body=build(config,selection,{'max_completion_tokens':512}).body
    assert body['max_completion_tokens']==512 and 'max_tokens' not in body
    assert config==before
    config['compatibility_profiles']['claude_sampling']['max_completion_tokens']=1024
    body=build(config,selection,{'max_tokens':4096}).body
    assert body['max_tokens']==4096 and 'max_completion_tokens' not in body


@pytest.mark.parametrize('selection',MODEL_CASES)
@pytest.mark.parametrize('parameter,preserve',[(True,False),(False,True)])
def test_parameter_diagnostics_keep_both_declared_values(config,selection,parameter,preserve):
    body=build(config,selection,{'max_tokens':256,'max_completion_tokens':512},parameter=parameter,preserve=preserve).body
    assert body['max_tokens']==256 and body['max_completion_tokens']==512


@pytest.mark.parametrize('selection',MODEL_CASES)
def test_ordinary_explicit_ambiguity_rejected(config,selection):
    with pytest.raises(ValueError,match='one output-limit field'):build(config,selection,{'max_tokens':256,'max_completion_tokens':512})


def test_other_reference_scope_does_not_inherit_rule(config):
    selection=MODEL_CASES[0]
    body=build(config,selection,{'max_tokens':256,'max_completion_tokens':512},route='vendor_direct').body
    assert body['max_tokens']==256 and body['max_completion_tokens']==512


@pytest.mark.parametrize('ordinal',range(1,41))
def test_original_alias_response_through_actual_client(config,ordinal):
    batch=REPO/'reports/approved_live_20260907/anthropic_output_alias_20260909T104615Z_90bbcbc9'
    if not (batch/'request_package.json').exists():pytest.skip('P1M original output-alias evidence unavailable')
    import hashlib
    p=json.loads((batch/'request_package.json').read_bytes());encoded=json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    assert hashlib.sha256(encoded).hexdigest()=='c87d36430a62735146215006139041121fb1ec84e6bb67d8e9bf880a28c10d71'
    case=p['cases'][ordinal-1];raw=(batch/f'case_{ordinal:02d}_response.bin').read_bytes();old=json.loads((batch/f'case_{ordinal:02d}_observation.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest()==old['response_sha256']
    payload=json.loads(raw);response=SimpleNamespace(status_code=old['status_code'],headers={'content-type':'application/json'},content=raw,text=raw.decode(),json=lambda:payload)
    client=OpenAICompatibleClient.from_config(config,provider='anthropic_official');client.session.post=Mock(return_value=response)
    result=client.chat_completion(case['body'])
    assert result.response_json==payload and result.status_code==old['status_code']
    if old['status_code']==200:assert result.finish_reason==payload['choices'][0]['finish_reason'] and result.usage==payload['usage']
    args,kw=client.session.post.call_args
    assert args==('https://api.anthropic.com/v1/chat/completions',) and kw['json']==case['body'] and kw['allow_redirects'] is False
