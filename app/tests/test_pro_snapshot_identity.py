"""Official dated model identities come from supplied immutable MPDB data only."""
import copy
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from lib.model_identity import audit_model_identity
from lib.client import ChatResult
from scripts import param_test

MODELS={'gpt-5-pro':'gpt-5-pro-2025-10-06','gpt-5.5-pro':'gpt-5.5-pro-2026-04-23'}


def provider():
    return {'base_url':'https://api.openai.com/v1','reference_source_id':'openai','models':{},
        'api_interfaces':{'openai_responses':{'path':'/responses','auth':'bearer'}}}


def snapshot(model):
    pid='text/openai/gpt/'+model;iid=pid+'#openai-responses-default'
    return {'source_id':'openai','api_form':'openai_responses','profile_id':pid,'interface_id':iid,
        'execution_target':{'request_model_id':model},
        'interface':{'source_id':'openai','api_form':'openai_responses','interface_id':iid,
            'identity':{'schema_version':1,'kind':'documented_snapshot_aliases','source_id':'openai','api_form':'openai_responses',
                'request_model_id':model,'allowed_response_model_ids':[model,MODELS[model]]}}}


def response(model):
    return {'id':'resp_offline','object':'response','status':'completed','model':MODELS[model],
        'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'OK'}]}],
        'usage':{'input_tokens':5,'output_tokens':2,'total_tokens':7,'output_tokens_details':{'reasoning_tokens':0}}}


def audit(model,p=None,s=None,endpoint='/responses',transport='openai_responses'):
    return audit_model_identity(requested_model=model,result=SimpleNamespace(response_json=response(model),headers={}),
        transport=transport,provider_cfg=p or provider(),exchange='offline',request_endpoint=endpoint,model_profile_database=s)


@pytest.mark.parametrize('model',MODELS)
def test_explicit_snapshot_enables_only_documented_alias(model):
    p=provider();s=snapshot(model);before=copy.deepcopy(s)
    assert audit(model,p,s)['status']=='match'
    assert audit(model,p,None)['status']=='mismatch'
    assert s==before and p==provider()
    assert any(r['kind']=='immutable_model_profile_identity' for r in audit(model,p,s)['evidence'])


@pytest.mark.parametrize('mutation',['provider_origin','auth','path','source','interface','form','target','identity_model','identity_kind','foreign_alias'])
def test_wrong_source_or_snapshot_cannot_authorize_alias(mutation):
    model='gpt-5-pro';p=provider();s=snapshot(model)
    if mutation=='provider_origin':p['base_url']='https://example.invalid/v1'
    elif mutation=='auth':p['api_interfaces']['openai_responses']['auth']='none'
    elif mutation=='path':p['api_interfaces']['openai_responses']['path']='/chat/completions'
    elif mutation=='source':s['source_id']='xai'
    elif mutation=='interface':s['interface_id']='foreign'
    elif mutation=='form':s['api_form']='openai_chat_completions'
    elif mutation=='target':s['execution_target']['request_model_id']='gpt-5.5-pro'
    elif mutation=='identity_model':s['interface']['identity']['request_model_id']='gpt-5.5-pro'
    elif mutation=='identity_kind':s['interface']['identity']['kind']='unverified'
    else:s['interface']['identity']['allowed_response_model_ids'].append('gpt-4o')
    assert audit(model,p,s)['status']=='mismatch'


@pytest.mark.parametrize('endpoint',[None,'https://example.invalid/v1/responses','/chat/completions'])
def test_unknown_actual_endpoint_does_not_get_official_aliases(endpoint):
    assert audit('gpt-5-pro',s=snapshot('gpt-5-pro'),endpoint=endpoint)['status']=='mismatch'


def test_configured_legacy_aliases_remain_supported_without_snapshot():
    p=provider();p['models']={'identity_aliases':{'gpt-5-pro':[MODELS['gpt-5-pro']]}}
    assert audit('gpt-5-pro',p=p)['status']=='match'


@pytest.mark.parametrize('model',MODELS)
def test_actual_identity_probe_uses_its_bound_snapshot(model,monkeypatch):
    root=Path(__file__).resolve().parents[1];c=yaml.load((root/'config.yaml').read_text(),Loader=yaml.CSafeLoader)
    c['_parameter_identity_snapshot']=snapshot(model)
    for key in ['LOADTEST_MODEL','LOADTEST_PROVIDER','LOADTEST_ROUTE_PROFILE','LOADTEST_API_FORM']:
        monkeypatch.delenv(key,raising=False)
    class Client:
        def openai_responses(self,body):
            assert body['model']==model and body['max_output_tokens']>=256
            return ChatResult(success=True,status_code=200,latency_ms=1,timestamp=0,response_json=response(model))
        def count_tokens(self,*args,**kwargs):return None
    r=param_test.run_identity_probe(c,Client(),'openai_official',model,'gpt','openai_responses','gpt')
    assert r['model_identity_audit']['status']=='match'
    assert r['model_identity_audit']['exchanges'][0]['returned_model']==MODELS[model]


@pytest.mark.parametrize('model',MODELS)
def test_actual_parameter_profile_retains_dated_identity_from_saved_response(model,monkeypatch):
    import hashlib,json
    root=Path(__file__).resolve().parents[1];repo=root.parent if root.name=='app' else root
    batches={'gpt-5-pro':'openai_pro_gpt-5-pro_20260909T135449Z_b3ec4acc','gpt-5.5-pro':'openai_pro_gpt-5_5-pro_20260909T135450Z_de48505e'}
    batch=repo/'reports/approved_live_20260907'/batches[model]
    if not (batch/'case_01_response.bin').exists():pytest.skip('P1M Pro raw response unavailable')
    raw=(batch/'case_01_response.bin').read_bytes();record=json.loads((batch/'case_01_observation.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest()==record['response_sha256']
    c=yaml.load((root/'config.yaml').read_text(),Loader=yaml.CSafeLoader);c['active_provider']='openai_official'
    c['_parameter_identity_snapshot']=snapshot(model);c['compatibility_profiles']['openai_responses_reasoning_high']['max_output_tokens']=8192
    for key in ['LOADTEST_MODEL','LOADTEST_PROVIDER','LOADTEST_ROUTE_PROFILE','LOADTEST_API_FORM']:
        monkeypatch.delenv(key,raising=False)
    class Client:
        def openai_responses(self,body):
            assert body['model']==model and body['reasoning']['effort']=='high'
            return ChatResult(success=True,status_code=200,latency_ms=1,timestamp=0,response_json=json.loads(raw))
        def count_tokens(self,*args,**kwargs):return None
    r=param_test.run_one_profile(c,Client(),'openai_official',model,'gpt','openai_responses','gpt','openai_responses_reasoning_high',1,
        {'id':'offline_identity_only','prompt':'Compute 6 multiplied by 7. Return only the two digits of the answer.'})
    assert r['model_identity_audit']['status']=='match'
    assert r['model_identity_audit']['exchanges'][0]['returned_model']==MODELS[model]
    # This replay checks the response identity consumer, not independent token accuracy.
