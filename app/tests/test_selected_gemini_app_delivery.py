"""Exact selected Gemini Web/JobSpec delivery boundaries from approvals 17/18."""
import copy
import json
from pathlib import Path
import pytest
import yaml
from lib.job_spec import load_job_spec
from scripts import web_console as web

@pytest.fixture
def context(tmp_path,monkeypatch):
    config=yaml.safe_load((Path(__file__).resolve().parents[1]/'config.yaml').read_text())
    monkeypatch.setattr(web,'load_config',lambda:copy.deepcopy(config))
    monkeypatch.setattr(web,'JOBS_ROOT',tmp_path/'jobs')
    monkeypatch.setattr(web,'REPORTS_ROOT',tmp_path)
    monkeypatch.setattr(web,'provider_has_api_key',lambda *a:True)
    monkeypatch.setattr(web,'image_provider_has_api_key',lambda *a:True)
    monkeypatch.setattr(web.JobManager,'_load_finished_jobs',lambda *a:None)
    monkeypatch.setattr(web.JobManager,'_start_locked',lambda *a:None)
    manager=web.JobManager();monkeypatch.setattr(web,'JOB_MANAGER',manager)
    return config,manager,web.app.test_client()

def test_gemini37_specs_and_job_freeze_exact_beta_interface(context):
    config,manager,http=context
    payload={'type':'param_test','provider':'gemini','model':'gemini-3.7-flash','route_profile':'google_ai_studio',
        'api_form':'gemini_generate_content','reference_contract_id':'gemini_3_7_flash_generate_content','param_test_runs':1}
    response=http.get('/api/param-specs',query_string={**payload,'contract_id':payload['reference_contract_id']})
    assert response.status_code==200 and len(response.json['test_profiles'])==26
    response=http.post('/api/jobs',json=payload);assert response.status_code==201,response.json
    job=manager._jobs[response.json['id']];frozen=load_job_spec(job.report_dir/'job_spec.json');s=frozen['model_profile_database']
    assert s['source_id']=='google_ai_studio' and s['interface_id']=='text/google_ai_studio/gemini/gemini-3.7-flash#gemini-generate-content-default'
    assert s['reference_contract_id']=='gemini_3_7_flash_generate_content' and s['test_binding']['api_version']=='v1beta' and s['interface']['default_api_version']=='v1beta'
    assert job.param_test_runs==1 and frozen['api_form']=='gemini_generate_content'
    assert frozen['schema_version']==6
    assert {key:frozen['parameter_execution'][key] for key in ('schema_version','runs','tool_validation_mode')}=={'schema_version':2,'runs':1,'tool_validation_mode':'auto'}
    assert frozen['parameter_execution']['plan_digest']==frozen['execution_plan']['plan_digest']
    before=json.dumps(frozen,sort_keys=True);config['providers']['gemini']['models']['default']='foreign'
    assert json.dumps(load_job_spec(job.report_dir/'job_spec.json'),sort_keys=True)==before

def test_unselected_gemini37_chat_cannot_enqueue(context):
    _,manager,http=context
    r=http.post('/api/jobs',json={'type':'param_test','provider':'gemini','model':'gemini-3.7-flash',
        'route_profile':'google_ai_studio','api_form':'openai_chat_completions','reference_contract_id':'gemini_openai_compat','param_test_runs':1})
    assert r.status_code==400 and not manager._jobs

def test_lite_native_form_has_own_immutable_job(context):
    _,manager,http=context
    r=http.post('/api/jobs',json={'type':'image_param_test','provider':'gemini','model':'gemini-3.1-flash-lite-image',
        'route_profile':'google_ai_studio','api_form':'gemini_generate_content','image_plan':{'suite':'smoke','no_cross_control':True}})
    assert r.status_code==201,r.json
    job=manager._jobs[r.json['id']];f=load_job_spec(job.report_dir/'job_spec.json');s=f['model_profile_database']
    assert s['source_id']=='google_ai_studio' and s['modality']=='image'
    assert s['interface_id']=='image/google_ai_studio/banana/gemini-3.1-flash-lite-image#gemini-generate-content-default'
    assert s['api_form']=='gemini_generate_content' and s['test_binding']['api_version']=='v1beta' and s['interface']['default_api_version']=='v1beta' and f['type']=='image_param_test'

def test_lite_interactions_remains_separate_closed_selection(context):
    _,manager,http=context
    r=http.post('/api/jobs',json={'type':'image_param_test','provider':'gemini','model':'gemini-3.1-flash-lite-image',
        'route_profile':'google_ai_studio','api_form':'gemini_interactions','image_plan':{'suite':'smoke','no_cross_control':True}})
    assert r.status_code==400 and not manager._jobs


def test_lite_missing_required_no_cross_control_never_enqueues(context):
    _,manager,http=context
    r=http.post('/api/jobs',json={'type':'image_param_test','provider':'gemini','model':'gemini-3.1-flash-lite-image',
        'route_profile':'google_ai_studio','api_form':'gemini_generate_content','image_plan':{'suite':'smoke'}})
    assert r.status_code==400 and 'no_cross_control' in r.json['error'] and not manager._jobs
