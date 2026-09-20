import copy
from types import SimpleNamespace
import pytest
from lib.profile_validation import validate_profile_response

MODEL='gemini-3.7-flash'
CONTRACT='gemini_3_7_flash_interactions'
PROFILE='gemini_3_7_flash_interactions_basic'
URL='https://generativelanguage.googleapis.com/v1beta/interactions'


def payload():
    return {'object':'interaction','model':MODEL,'status':'completed',
        'steps':[{'type':'thought','signature':'opaque'},{'type':'model_output','content':[{'type':'text','text':'42'}]}],
        'usage':{'total_input_tokens':5,'total_output_tokens':2,'total_thought_tokens':1,'total_tokens':8,'total_cached_tokens':0}}


def arguments():
    p=payload()
    return dict(profile=PROFILE,response_json=p,result=SimpleNamespace(success=True,usage=p['usage']),
        request_body={'model':MODEL,'input':'Calculate six times seven','store':False,'stream':False,'background':False},
        transport='gemini_interactions',reference_source=CONTRACT,
        request_context={'requested_model':MODEL,'request_url':URL})


def test_exact_stateless_source_accepts_absent_or_default_empty_id():
    a=arguments();assert validate_profile_response(**a) is None
    a['response_json']['id']='';assert validate_profile_response(**a) is None


@pytest.mark.parametrize('change',['no_context','host','path','query','userinfo','model','source','store','stream','background','tools','previous','stream_profile','image_format','mime_list'])
def test_id_exception_cannot_escape_verified_source_or_mode(change):
    a=arguments()
    if change=='no_context':a['request_context']=None
    if change=='host':a['request_context']['request_url']='https://example.com/v1beta/interactions'
    if change=='path':a['request_context']['request_url']=URL.replace('v1beta','v1')
    if change=='query':a['request_context']['request_url']=URL+'?route=other'
    if change=='userinfo':a['request_context']['request_url']=URL.replace('https://','https://user@')
    if change=='model':a['request_body']['model']='gemini-3.6-flash'
    if change=='source':a['reference_source']='other_contract'
    if change in ('store','stream','background'):a['request_body'][change]=True
    if change=='tools':a['request_body']['tools']=[{'type':'function','name':'tool'}]
    if change=='previous':a['request_body']['previous_interaction_id']='stored_parent'
    if change=='stream_profile':a['profile']='gemini_3_7_flash_interactions_stream'
    if change=='image_format':a['request_body']['response_format']={'type':'image'}
    if change=='mime_list':a['request_body']['response_format']={'type':'text','mime_type':[]}
    assert validate_profile_response(**a)=='interaction_id_missing'


@pytest.mark.parametrize('value',[None,0,False,[],{}])
def test_present_nonstring_id_is_not_accepted(value):
    a=arguments();a['response_json']['id']=value
    assert validate_profile_response(**a)=='interaction_id_invalid'


@pytest.mark.parametrize('bad',['tool_step','image_output','thought_image','thought_nested_image','step_type_list','step_type_dict','status_list','empty_steps','diagnostic_error','bad_total','usage_mismatch','wrong_model'])
def test_stateless_id_exception_does_not_hide_invalid_native_response(bad):
    a=arguments();p=a['response_json']
    if bad=='tool_step':p['steps'].append({'type':'function_call','name':'unexpected'})
    if bad=='image_output':p['steps'][-1]['content']=[{'type':'image','data':'not-requested'}]
    if bad=='thought_image':p['steps'][0]['summary']=[{'type':'image','data':'not-requested'}]
    if bad=='thought_nested_image':p['steps'][0]['summary']=[{'content':{'type':'image','data':'not-requested'}}]
    if bad=='step_type_list':p['steps'][0]['type']=[]
    if bad=='step_type_dict':p['steps'][0]['type']={}
    if bad=='status_list':p['status']=[]
    if bad=='empty_steps':p['steps']=[]
    if bad=='diagnostic_error':p['errors']=[{'code':'failure'}]
    if bad=='bad_total':p['usage']['total_tokens']=True
    if bad=='usage_mismatch':a['result'].usage={**p['usage'],'total_tokens':99}
    if bad=='wrong_model':p['model']='other-model'
    assert validate_profile_response(**a) is not None
