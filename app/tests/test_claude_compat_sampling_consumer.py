import copy,json,hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest,yaml
from lib.deepseek_params import build_request
from lib.client import OpenAICompatibleClient

ROOT=Path(__file__).resolve().parents[1];REPO=ROOT.parent if ROOT.name=='app' else ROOT
BATCH=REPO/'reports/approved_live_20260907/anthropic_compat_sampling_20260909T120825Z_f2b1a3b0'
SHA='a60b2deb94724f7b56cdb3184b2b257c41117b1243ade155b2488433d516010a'

@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-sampling-consumer')
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official';return c

def build(c,profile='claude_sampling',*,fields=None,parameter=False,preserve=False,route='vendor_compat'):
    return build_request(c,'compatibility_profiles',profile,overrides={'model':'claude-opus-4-5-20251101','max_tokens':4096,'preserve_rejected_params':preserve,**(fields or {})},
        model_family_override='claude',api_form_override='openai_chat_completions',route_profile_override=route,
        reference_source='claude_openai_compat',enforce_model_capabilities=False,parameter_test=parameter)

def test_ordinary_default_or_explicit_sampling_selection(config):
    original=copy.deepcopy(config);body=build(config).body
    assert body['temperature']==1 and 'top_p' not in body
    body=build(config,fields={'top_p':0.9}).body;assert body['top_p']==0.9 and 'temperature' not in body
    body=build(config,fields={'temperature':0.5}).body;assert body['temperature']==0.5 and 'top_p' not in body
    with pytest.raises(ValueError,match='one field'):build(config,fields={'temperature':1,'top_p':1})
    assert config==original

@pytest.mark.parametrize('parameter,preserve',[(True,False),(False,True)])
def test_legacy_combination_diagnostic_keeps_both_fields(config,parameter,preserve):
    body=build(config,parameter=parameter,preserve=preserve).body
    assert body['temperature']==body['top_p']==1

@pytest.mark.parametrize('profile,present,absent',[('claude_temperature','temperature','top_p'),('claude_top_p','top_p','temperature')])
def test_new_profiles_isolate_one_field(config,profile,present,absent):
    body=build(config,profile,parameter=True).body
    assert body[present]==1 and absent not in body

def test_other_route_keeps_existing_combination(config):
    body=build(config,route='vendor_direct').body
    assert body['temperature']==body['top_p']==1

@pytest.mark.parametrize('ordinal',[n for n in range(1,73) if n not in (31,32,39,40,47,48,71,72)])
def test_original_sampling_request_and_response_replay(config,ordinal):
    if not (BATCH/'request_package.json').is_file():pytest.skip('P1M sampling originals unavailable')
    package=json.loads((BATCH/'request_package.json').read_bytes());raw_package=json.dumps(package,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    assert hashlib.sha256(raw_package).hexdigest()==SHA
    case=package['cases'][ordinal-1];raw=(BATCH/f'case_{ordinal:02d}_response.bin').read_bytes();record=json.loads((BATCH/f'case_{ordinal:02d}_observation.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest()==record['response_sha256'];value=json.loads(raw)
    client=OpenAICompatibleClient.from_config(config,provider='anthropic_official');client.session.post=Mock(return_value=SimpleNamespace(status_code=record['status_code'],headers={},content=raw,text=raw.decode(),json=lambda:value))
    result=client.chat_completion(case['body']);assert result.response_json==value and result.status_code==record['status_code']
    assert client.session.post.call_count==1 and client.session.post.call_args.kwargs['json']==case['body']
