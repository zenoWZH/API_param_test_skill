import copy

import pytest

from lib.reference_specs import load_model_capability_profile, _project_gemini37_rejection_expectations


@pytest.fixture
def capability():
    cap = load_model_capability_profile('text', 'gemini', 'gemini-3.7-flash', api_form='gemini_generate_content',
        route_profile='google_ai_studio', reference_source='gemini_3_7_flash_generate_content')
    cap['expectations'] = {}
    cap['parameter_expectations'] = {}
    return cap


def test_effective_model_rejections_supply_named_negative_expectations(capability):
    before = copy.deepcopy(capability)
    result = _project_gemini37_rejection_expectations(capability)
    for profile in ('gemini_native_candidate_count', 'gemini_native_presence_penalty',
                    'gemini_native_frequency_penalty', 'gemini_native_logprobs'):
        assert result['expectations'][profile] == 'unsupported'
    assert result['parameter_expectations']['generationConfig.logprobs'] == 'unsupported'
    assert capability == before


@pytest.mark.parametrize('field,value', [('source_id', 'google_vertex'),
    ('profile_id', 'text/google_ai_studio/gemini/gemini-3.1-pro-preview'),
    ('api_form', 'openai_chat_completions')])
def test_rejection_projection_never_crosses_source_model_or_form(capability, field, value):
    capability[field] = value
    before = copy.deepcopy(capability)
    assert _project_gemini37_rejection_expectations(capability) == before


def test_documented_no_rejection_metadata_is_not_reinterpreted_as_negative(capability):
    capability['parameter_capabilities']['generationConfig.candidateCount']['http_rejection_expected'] = False
    result = _project_gemini37_rejection_expectations(capability)
    assert 'generationConfig.candidateCount' not in result['parameter_expectations']
    assert 'gemini_native_candidate_count' not in result['expectations']


def test_explicit_binding_expectations_are_preserved(capability):
    capability['expectations']['gemini_native_candidate_count'] = 'supported'
    capability['parameter_expectations']['generationConfig.candidateCount'] = 'supported'
    result = _project_gemini37_rejection_expectations(capability)
    assert result['expectations']['gemini_native_candidate_count'] == 'supported'
    assert result['parameter_expectations']['generationConfig.candidateCount'] == 'supported'
