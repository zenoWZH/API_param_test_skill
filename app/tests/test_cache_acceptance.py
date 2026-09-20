from types import SimpleNamespace
import copy
import pytest
from lib.cache_acceptance import cache_telemetry,rates,apply_summary,validate_scenario,is_pro_model
from lib.metrics import RequestRecord,apply_cache_token_audits,CACHE_CONTROL_POSITIVE,CACHE_CONTROL_NEGATIVE,CACHE_CONTROL_ROLE_COLD,CACHE_CONTROL_ROLE_WARM,CACHE_CONTROL_ROLE_UNIQUE


def record(n,cached=None,*,success=True,extra=None):
    usage={'prompt_tokens':n}
    if cached is not None:usage['prompt_tokens_details']={'cached_tokens':cached}
    return SimpleNamespace(success=success,usage=usage,extra=extra or {},is_warmup=False)


def test_weighted_token_ratio_is_not_mean_request_ratio():
    r=rates([record(100,90),record(900,90)])
    assert r['cached_input_token_ratio']==.18
    assert r['cache_hit_request_ratio']==1


def test_missing_and_invalid_cache_values_are_not_zero_or_clipped():
    r=rates([record(100,20),record(100),record(100,150),record(100,0,success=False)])
    assert r['cached_input_token_ratio']==.2 and r['measurement_coverage']==pytest.approx(1/3)
    assert r['missing_measurement_count']==r['invalid_measurement_count']==1
    assert rates([record(100)])['cached_input_token_ratio'] is None


def test_native_anthropic_denominator_includes_read_and_write():
    r=cache_telemetry({'input_tokens':20,'cache_creation_input_tokens':30,'cache_read_input_tokens':50},'claude_messages')
    assert r['input_tokens']==100 and r['cached_input_tokens']==50
    assert cache_telemetry({'input_tokens':20,'cache_creation_input_tokens':30},'claude_messages')['status']=='missing'


def test_customer_rates_exclude_controls_and_structure_probes():
    rows=[record(100,50),record(900,90),record(1000,1000,extra={'cache_control':'positive','control_role':'warm'}),record(1000,0,extra={'cache_structure_probe':True})]
    s={};apply_summary(s,rows)
    assert s['actual_cache_hit_rate']==.14 and s['customer_input_tokens']==1000
    assert s['cache_hit_request_ratio']==1 and not s['cache_official_numeric_reference_required']


def scenario():
    b={'source_id':'x','interface_id':'i'}
    return dict(cache_reads=[0,1,0],input_tokens=[10000]*3,prefixes=['A'*100,'A'*100,'B'*100],settings=[{}]*3,bindings=[b]*3,expected_binding=b)


def test_hit_expectations_need_no_reference_threshold_or_ratio():
    r=validate_scenario(**scenario());assert r['status']=='pass'
    assert r['request_hit_rate']==pytest.approx(1/3) and not r['official_numeric_reference_required']


@pytest.mark.parametrize('change,status',[
 ({'cache_reads':[0,0,0]},'expectation_failed'),({'cache_reads':[0,None,0]},'insufficient_evidence'),
 ({'cache_reads':[1,100,0]},'expectation_failed'),({'cache_reads':[0,10001,0]},'control_failed'),
 ({'prefixes':['A'*100,'A'*100,'A'*100+'new']},'control_failed')])
def test_scenario_failure_kinds_stay_distinct(change,status):
    assert validate_scenario(**(scenario()|change))['status']==status


@pytest.mark.parametrize('model,pro',[('gpt-5.5-pro',True),('deepseek-v4-pro',True),('gemini-3.1-pro-preview',True),('claude-opus-5',False),('gpt-6-astra',False)])
def test_pro_scope(model,pro):assert is_pro_model(model) is pro


def audit_record(hit,role,control):
    return RequestRecord(timestamp=0,task_name='cache:control',profile='cache',group='cache_profiles',method='POST',path='/chat/completions',success=True,latency_ms=1,status_code=200,
        usage={'prompt_tokens':10000,'prompt_tokens_details':{'cached_tokens':hit}},
        extra={'cache_scenario':'shared_prefix','cache_control':control,'control_role':role,'control_pair':0})


def test_current_control_policy_accepts_any_real_hit_and_rejects_negative_read():
    cold=audit_record(0,CACHE_CONTROL_ROLE_COLD,CACHE_CONTROL_POSITIVE);warm=audit_record(1,CACHE_CONTROL_ROLE_WARM,CACHE_CONTROL_POSITIVE)
    negative=audit_record(0,CACHE_CONTROL_ROLE_UNIQUE,CACHE_CONTROL_NEGATIVE)
    apply_cache_token_audits([cold,warm,negative],{'control_evaluation':'hit_expectations','positive_control_cached_ratio_min':.5,'negative_control_cached_ratio_max':.1})
    assert all(r.cache_token_audit['status']=='pass' for r in [cold,warm,negative])
    negative.usage['prompt_tokens_details']['cached_tokens']=1
    apply_cache_token_audits([cold,warm,negative],{'control_evaluation':'hit_expectations'})
    assert negative.cache_token_audit['status']=='fail'


def test_json_boolean_and_integer_controls_are_not_equivalent():
    args=scenario();args['settings']=[{'stream':False},{'stream':0},{'stream':False}]
    assert validate_scenario(**args)['status']=='control_failed'


def test_new_gate_needs_scenarios_not_reference_ratios(tmp_path):
    from lib.threshold import check_cache
    summary={'cache_evaluation_policy':'scenario_expectations_v1','cache_expectations_pass':True,'cache_expectation_status':'pass',
        'cache_usage_accuracy_pass':True,'cached_input_token_ratio':.0001,'cache_measurement_coverage':1.0,'cache_usage_fields_seen':3,
        'cache_control_metrics':{'positive_long_prefix':{'cached_input_token_ratio':.0001},'negative_unique_prefix':{'cached_input_token_ratio':0}}}
    config={'thresholds':{'cache':{'mode':'gate','positive_control_cached_ratio_min':.5,'negative_control_cached_ratio_max':.05}}}
    result=check_cache({'summary':summary},config,tmp_path)
    assert result['pass'] and result['threshold_pass']
    summary.update(cache_expectations_pass=False,cache_expectation_status='unverified')
    result=check_cache({'summary':summary},{'thresholds':{'cache':{'mode':'observe'}}},tmp_path)
    assert not result['pass']


def test_missing_control_telemetry_never_claims_scenario_pass():
    rows=[record(100,10),record(100,0,extra={'cache_control':CACHE_CONTROL_POSITIVE,'control_role':'cold'}),
          record(100,None,extra={'cache_control':CACHE_CONTROL_POSITIVE,'control_role':'warm'}),
          record(100,0,extra={'cache_control':CACHE_CONTROL_NEGATIVE,'control_role':'unique'})]
    s={};apply_summary(s,rows)
    assert not s['cache_expectations_pass'] and s['cache_expectation_status']=='unverified'


def test_pro_cache_stops_before_transport(tmp_path,monkeypatch):
    from lib import cache_suite as suite
    monkeypatch.setattr(suite,'get_active_provider_name',lambda c:'p')
    monkeypatch.setattr(suite,'get_provider_config',lambda c,p:{})
    monkeypatch.setattr(suite,'get_selected_model',lambda c,p:'gpt-5-pro')
    monkeypatch.setattr(suite,'get_model_family',lambda *a:'gpt')
    monkeypatch.setattr(suite,'_prepare_cache_model_profile',lambda *a:pytest.fail('Pro reached preparation'))
    with pytest.raises(ValueError,match='Pro models are excluded'):
        suite.run_cache_suite({'cache_test':{'exclude_pro':True}},object(),tmp_path)


def test_actual_cache_finalizer_uses_all_short_measured_requests(tmp_path):
    from lib.cache_suite import _finalize_cache_suite
    def customer(n,hit):
        return RequestRecord(timestamp=1,task_name='cache:measure',group='cache_profiles',profile='shared',method='POST',path='/chat/completions',success=True,
            usage={'prompt_tokens':n,'prompt_tokens_details':{'cached_tokens':hit}},extra={'cache_scenario':'shared_prefix','transport':'chat_completions'})
    cold=audit_record(0,CACHE_CONTROL_ROLE_COLD,CACHE_CONTROL_POSITIVE)
    warm=audit_record(1,CACHE_CONTROL_ROLE_WARM,CACHE_CONTROL_POSITIVE)
    negative=audit_record(0,CACHE_CONTROL_ROLE_UNIQUE,CACHE_CONTROL_NEGATIVE)
    rows=[customer(100,90),customer(900,90),cold,warm,negative]
    r=_finalize_cache_suite({'thresholds':{'cache':{'control_evaluation':'hit_expectations'}},'metrics':{'cache_min_prompt_tokens':4000}},tmp_path,rows,[],5)
    s=r['summary']
    assert s['cache_min_prompt_tokens']==0
    assert s['actual_cache_hit_rate']==.18 and s['cache_hit_tokens']==180 and s['cache_miss_tokens']==820
    assert s['cache_expectations_pass'] is True
