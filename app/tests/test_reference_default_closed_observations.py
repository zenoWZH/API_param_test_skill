import pytest
from lib.reference_specs import load_model_capability_profile


@pytest.mark.parametrize('model,cid', [('glm-5.3','zhipu_glm_5_3_openai_compat'),('glm-5.3-flash','zhipu_glm_5_3_flash_openai_compat')])
def test_default_app_projection_keeps_the_executable_origin(model,cid):
    cap=load_model_capability_profile('text','glm',model,api_form='openai_chat_completions',route_profile='vendor_direct')
    assert cap['interface_id'].endswith('#openai-chat-default')
    assert cap['reference_contract_id']==cid and cap['parameter_test_enabled'] is True


def test_closed_general_observation_stays_read_only():
    cap=load_model_capability_profile('text','glm','glm-5.3',api_form='openai_chat_completions',
        route_profile='vendor_direct',reference_source='zai_general_glm_5_3_chat',read_only=True)
    assert cap['reference_contract_id']=='zai_general_glm_5_3_chat'
    assert cap['parameter_test_enabled'] is False and cap['executable'] is False
