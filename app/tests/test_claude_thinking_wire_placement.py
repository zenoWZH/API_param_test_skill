import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import yaml
from lib.deepseek_params import build_request
from lib.client import OpenAICompatibleClient

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv('LOADTEST_PROVIDER',raising=False);monkeypatch.delenv('LOADTEST_MODEL',raising=False)
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-thinking-placement')
    c=yaml.safe_load((ROOT/'config.yaml').read_bytes());c['active_provider']='anthropic_official';return c


def build(c,profile,*,overrides=None,parameter_test=True,group='compatibility_profiles',route='vendor_compat'):
    return build_request(c,group,profile,overrides={'model':'claude-sonnet-4-5-20250929','max_tokens':4096,**(overrides or {})},
        model_family_override='claude',api_form_override='openai_chat_completions',route_profile_override=route,
        reference_source='claude_openai_compat',enforce_model_capabilities=False,parameter_test=parameter_test)


@pytest.mark.parametrize('parameter_test',[False,True])
def test_named_disabled_profile_reaches_top_level_http(config,parameter_test):
    original=copy.deepcopy(config);request=build(config,'claude_thinking_disabled',parameter_test=parameter_test)
    assert request.body['thinking']=={'type':'disabled'} and 'extra_body' not in request.body and request.body['max_tokens']==4096
    assert config==original
    client=OpenAICompatibleClient.from_config(config,provider='anthropic_official')
    client.session.post=Mock(return_value=SimpleNamespace(status_code=400,headers={},content=b'{}',text='{}',json=lambda:{}))
    client.chat_completion(request.body)
    assert client.session.post.call_args.kwargs['json']['thinking']=={'type':'disabled'}
    assert 'extra_body' not in client.session.post.call_args.kwargs['json']


@pytest.mark.parametrize('parameter_test',[False,True])
def test_explicit_thinking_parameter_override_is_not_removed(config,parameter_test):
    body=build(config,'claude_sampling',overrides={'thinking':{'type':'disabled'}},parameter_test=parameter_test).body
    assert body['thinking']=={'type':'disabled'}


def test_shared_basic_stream_default_is_still_normalized(config):
    assert 'thinking' not in build(config,'basic_stream',parameter_test=False).body


def test_other_route_does_not_gain_source_specific_preservation(config):
    body=build(config,'claude_sampling',overrides={'thinking':{'type':'disabled'}},route='vendor_direct').body
    assert 'thinking' not in body


@pytest.mark.parametrize('group,profile',[('throughput_profiles','baseline_short'),('cache_profiles','cache_long_context')])
def test_load_and_cache_defaults_keep_old_behavior(config,group,profile):
    body=build(config,profile,group=group,overrides={'thinking':{'type':'disabled'},'preserve_rejected_params':True}).body
    assert 'thinking' not in body
