"""No-network verification of real FIM transport, CLI and served Web paths."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from lib.client import DeepSeekClient
from lib.config import load_config
from lib import deepseek_beta_runner as bounded
from lib import deepseek_fim_reference as fim
from lib import deepseek_fim_runner as runner
from test_deepseek_fim_reference import snapshot, plan, records
from test_deepseek_beta_runner import Response


@pytest.fixture
def client(monkeypatch):
    from lib import client as client_module
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(client_module, "get_api_key", lambda *args: "offline-fim-test-secret")
    return DeepSeekClient.from_config(load_config(), "deepseek_official")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    c = load_config()
    c["providers"] = {"deepseek_official": c["providers"]["deepseek_official"]}
    c["active_provider"] = "deepseek_official"
    c["providers"]["deepseek_official"]["models"]["default_api_forms"]["deepseek-v4-pro"]["vendor_direct"] = fim.API_FORM
    return c


def dispatch(client, plan, directory, responses, calls, *, minimum=256):
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]
    return bounded._execute(client, plan, directory, request,
        minimum_output_tokens=minimum, on_record=None, validate_plan=runner.validate_fim_client,
        evaluate_plan=fim.evaluate_fim_plan, next_request=fim.next_fim_request, artifact_prefix="fim_causal")


def fake_session(monkeypatch, responses, calls):
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def mount(self, url, adapter):
            assert url == "https://" and adapter.max_retries.total == 0
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            calls.append((method, url, kwargs))
            return responses[len(calls) - 1]
    monkeypatch.setattr(runner.requests, "Session", Session)


def test_public_dispatch_sends_eight_exact_bodies_and_cannot_reenter(client, plan, records, tmp_path, monkeypatch):
    calls = []
    responses = [Response(row) for row in records]
    fake_session(monkeypatch, responses, calls)
    actual, observed = runner.execute_fim_plan(client, plan, tmp_path)
    assert len(actual) == len(calls) == 8 and observed["pass"] and all(r.closed for r in responses)
    for (method, url, kwargs), request in zip(calls, plan.requests):
        assert method == "POST" and url == fim.ENDPOINT
        assert kwargs["data"] == request.body_bytes and kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True and json.loads(kwargs["data"])["stream"] is False
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")
        assert kwargs["timeout"] == (15, 150)
    assert len(list(tmp_path.glob("fim_causal_*_attempt.json"))) == 8
    assert json.loads((tmp_path / "fim_causal_dispatch_finished.json").read_text())["requests_sent"] == 8
    assert "offline-fim-test-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    with pytest.raises(ValueError, match="ledger"):
        runner.execute_fim_plan(client, plan, tmp_path)
    assert len(calls) == 8


@pytest.mark.parametrize("status", [301, 401, 402, 403, 404, 408, 429, 500, 503])
def test_fatal_status_stops_without_retry(client, plan, records, tmp_path, status):
    calls = []
    actual, observed = dispatch(client, plan, tmp_path, [Response({**records[0], "status_code": status})], calls)
    assert len(calls) == len(actual) == 1 and not observed["complete"] and not observed["pass"]


def test_timeout_after_complete_looking_json_preserves_failure(client, plan, records, tmp_path):
    calls = []
    response = Response(records[0], fail=True)
    actual, observed = dispatch(client, plan, tmp_path, [response], calls)
    assert len(calls) == 1 and response.closed
    assert actual[0]["failure_type"] == "TimeoutError" and not actual[0]["response_complete"]
    assert not observed["case_results"][0]["pass"]


@pytest.mark.parametrize("marker", ["fim_causal_dispatch_started.json", "fim_causal_dispatch_finished.json", "fim_causal_01_attempt.json"])
def test_any_prior_dispatch_marker_prevents_automatic_resumption(client, plan, tmp_path, marker):
    (tmp_path / marker).write_text("{}")
    calls = []
    with pytest.raises(ValueError, match="ledger"): dispatch(client, plan, tmp_path, [], calls)
    assert calls == [] and (tmp_path / marker).read_text() == "{}"


@pytest.mark.parametrize("mutation", ["provider", "origin", "path", "auth", "floor", "subclass"])
def test_dispatch_scope_changes_fail_before_attempt(client, plan, tmp_path, mutation):
    if mutation == "provider": client.provider = "relay"
    elif mutation == "origin": client.base_url = "https://other.invalid"
    elif mutation == "path":
        client.api_interfaces[fim.TRANSPORT]["path"] = "/unapproved/completions"
        assert client._transport_url(fim.TRANSPORT) != fim.ENDPOINT
    elif mutation == "auth": client.api_interfaces[fim.TRANSPORT]["auth"] = "anthropic"
    elif mutation == "subclass":
        class OverridePlan(fim.FimPlan):
            @property
            def requests(self): return ()
        plan = OverridePlan(plan._snapshot_bytes)
    calls = []
    with pytest.raises(ValueError): dispatch(client, plan, tmp_path, [], calls, minimum=1024 if mutation == "floor" else 256)
    assert calls == [] and not list(tmp_path.iterdir())


def cli_setup(monkeypatch, config, client, tmp_path):
    from scripts import param_test as cli
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli.DeepSeekClient, "from_config", lambda *a: client)
    monkeypatch.setattr(cli, "run_identity_probe", lambda *a, **k: pytest.fail("extra identity request"))
    monkeypatch.setattr(cli, "run_param_tests", lambda *a, **k: pytest.fail("generic profile dispatch"))
    monkeypatch.setattr(client, "count_tokens", lambda *a, **k: pytest.fail("extra count request"))
    for key in ("LOADTEST_REFERENCE_SOURCE", "LOADTEST_JOB_SPEC", "LOADTEST_PARAM_TEST_RUNS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LOADTEST_PROVIDER": "deepseek_official", "LOADTEST_MODEL": "deepseek-v4-pro",
        "LOADTEST_API_FORM": fim.API_FORM, "LOADTEST_ROUTE_PROFILE": "vendor_direct",
        "LOADTEST_PARAMETER_SUITE": fim.SUITE_ID, "LOADTEST_REPORT_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    return cli


def test_actual_cli_dispatches_eight_without_identity_count_or_generic_and_keeps_echo_raw(monkeypatch, config, client, records, tmp_path, capsys):
    cli = cli_setup(monkeypatch, config, client, tmp_path)
    calls, views = [], []
    responses = [Response(row) for row in records]
    fake_session(monkeypatch, responses, calls)
    original = cli._audit_exchange_safely
    def audit(config, body, native, *args, **kwargs):
        views.append((copy.deepcopy(body), copy.deepcopy(native.response_json)))
        return original(config, body, native, *args, **kwargs)
    monkeypatch.setattr(cli, "_audit_exchange_safely", audit)
    assert cli.main() in (0, 1)
    capsys.readouterr()
    assert len(calls) == 8
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    rows = json.loads((tmp_path / "param_results.json").read_text())
    assert verdict["parameter_suite"] == fim.SUITE_ID and verdict["total"] == verdict["planned_requests"] == 8
    assert verdict["identity_probe"] is None and verdict["identity_probe_requests"] == 0
    assert verdict["bounded_fim_observations"]["pass"] and not verdict["full_parameter_matrix_verified"]
    assert verdict["param_specs"]["test_profiles"] == list(fim.CASE_IDS)
    assert rows[2]["status"] == "expected_rejection" and rows[2]["response_json"].get("choices") is None
    assert all(row["compatibility_pass"] for row in rows)
    prompt = rows[7]["request_body"]["prompt"]
    assert rows[7]["response_raw"] == records[7]["response_raw"]
    assert rows[7]["response_json"]["choices"][0]["text"] == prompt + "42"
    assert rows[7]["token_audit"]["output_projection"]["raw_response_preserved"] is True
    echo_views = [payload for body, payload in views if body.get("echo") is True and "suffix" not in body]
    assert echo_views and all(p["choices"][0]["text"] == "42" for p in echo_views)
    assert "offline-fim-test-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("mutation", ["unknown_suite", "repeat", "wrong_form", "job_selection_conflict"])
def test_cli_invalid_selection_fails_before_sending(monkeypatch, config, client, tmp_path, mutation):
    cli = cli_setup(monkeypatch, config, client, tmp_path)
    calls = []
    fake_session(monkeypatch, [], calls)
    if mutation == "unknown_suite": monkeypatch.setenv("LOADTEST_PARAMETER_SUITE", "unknown")
    elif mutation == "repeat": monkeypatch.setenv("LOADTEST_PARAM_TEST_RUNS", "2")
    elif mutation == "wrong_form": monkeypatch.setenv("LOADTEST_API_FORM", "openai_chat_completions")
    else: monkeypatch.setattr(cli, "load_job_spec", lambda *a: {"parameter_suite": None})
    with pytest.raises(ValueError): cli.main()
    assert calls == []


@pytest.fixture
def web_context(monkeypatch, config, tmp_path):
    from scripts import web_console as web
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(web, "REPORTS_ROOT", tmp_path)
    monkeypatch.setattr(web, "provider_has_api_key", lambda *a: True)
    monkeypatch.setattr(web.JobManager, "_load_finished_jobs", lambda *a: None)
    monkeypatch.setattr(web.JobManager, "_start_locked", lambda *a: None)
    manager = web.JobManager()
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    payload = {"type": "param_test", "provider": "deepseek_official", "model": "deepseek-v4-pro",
        "api_form": fim.API_FORM, "route_profile": "vendor_direct", "reference_contract_id": fim.CONTRACT_ID,
        "parameter_suite": fim.SUITE_ID}
    return web, manager, web.app.test_client(), payload


def test_actual_web_job_and_specs_freeze_eight_but_preserve_generic_fifteen(web_context):
    web, manager, http, payload = web_context
    query = {k: payload[k] for k in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    query["contract_id"] = fim.CONTRACT_ID
    fixed = http.get("/api/param-specs", query_string=query)
    assert fixed.status_code == 200
    assert fixed.json["test_profiles"] == list(fim.CASE_IDS) and fixed.json["fixed_request_count"] == 8
    generic = http.get("/api/param-specs", query_string={k: v for k, v in query.items() if k != "parameter_suite"})
    assert generic.status_code == 200 and len(generic.json["test_profiles"]) == 15
    assert generic.json["available_parameter_suites"][0]["id"] == fim.SUITE_ID
    response = http.post("/api/jobs", json=payload)
    assert response.status_code == 201
    job = manager._jobs[response.json["id"]]
    assert job.param_test_runs == 1 and job.job_spec["parameter_suite"] == fim.SUITE_ID
    assert json.loads((job.report_dir / "job_spec.json").read_text())["parameter_suite"] == fim.SUITE_ID
    assert len(fim.build_fim_plan(job.job_spec["model_profile_database"], suite_id=fim.SUITE_ID).requests) == 8
    assert web._param_progress_counts(job, [1, 2])["total_cells"] == 8


@pytest.mark.parametrize("change", [
    {"parameter_suite": "unknown"}, {"parameter_suite": True}, {"param_test_runs": True}, {"param_test_runs": 1.0},
    {"param_test_runs": 0}, {"param_test_runs": 2}, {"type": "quick_load"}, {"type": "cache_suite"},
    {"type": "staircase"}, {"type": "soak"}, {"api_form": "openai_chat_completions"},
])
def test_actual_web_api_rejects_scope_changes_before_queue(web_context, change):
    _web, manager, http, payload = web_context
    response = http.post("/api/jobs", json={**payload, **change})
    assert response.status_code == 400
    assert not manager._jobs


@pytest.mark.parametrize("change", [{"parameter_suite": "unknown"}, {"api_form": "openai_chat_completions"}])
def test_specs_endpoint_rejects_unknown_or_cross_form_suite(web_context, change):
    _web, _manager, http, payload = web_context
    query = {k: payload[k] for k in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    response = http.get("/api/param-specs", query_string={**query, **change})
    assert response.status_code == 400


def test_result_identity_rejects_cross_suite_reuse(web_context, plan, records):
    from lib.job_spec import _result_identity_matches
    _web, manager, _http, payload = web_context
    job = manager.create(payload)
    observation = fim.evaluate_fim_plan(fim.build_fim_plan(job.job_spec["model_profile_database"], suite_id=fim.SUITE_ID), records)
    result = {**copy.deepcopy(job.job_spec), "parameter_suite": fim.SUITE_ID, "planned_requests": 8,
              "bounded_fim_observations": observation, "pass": True}
    assert _result_identity_matches(job.job_spec, result)[0]
    generic_result = copy.deepcopy(result)
    generic_result.pop("parameter_suite")
    assert "parameter_suite_mismatch" in _result_identity_matches(job.job_spec, generic_result)[1]
    wrong = copy.deepcopy(result)
    wrong["bounded_fim_observations"]["case_results"].pop()
    assert "fixed_suite_results_incomplete" in _result_identity_matches(job.job_spec, wrong)[1]


def test_latest_history_selects_fixed_and_generic_independently(web_context, monkeypatch):
    _web, manager, http, payload = web_context
    fixed = manager.create(payload)
    fixed.status, fixed.finished_at = "failed", 10.0
    fixed.process = None
    (fixed.report_dir / "verdict.json").write_text(json.dumps({"param_specs": {"test_profiles": list(fim.CASE_IDS)}}))
    generic = copy.deepcopy(fixed)
    generic.id += "-generic"
    generic.report_dir = fixed.report_dir.parent / generic.id
    generic.report_dir.mkdir()
    (generic.report_dir / "verdict.json").write_text(json.dumps({"param_specs": {
        "test_profiles": fixed.job_spec["model_profile_database"]["parameter_test_binding"]["test_cases"]}}))
    generic.job_spec.pop("parameter_suite")
    generic.finished_at = 20.0
    manager._jobs[generic.id] = generic
    monkeypatch.setattr(manager, "_refresh_locked", lambda job: job)
    monkeypatch.setattr(manager, "public", lambda job, **kw: {"id": job.id, "parameter_suite": job.job_spec.get("parameter_suite")})
    query = {k: payload[k] for k in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    query["contract_id"] = fim.CONTRACT_ID
    response = http.get("/api/param-results/latest", query_string=query)
    assert response.status_code == 200 and response.json["result"]["id"] == fixed.id
    response = http.get("/api/param-results/latest", query_string={k: v for k, v in query.items() if k != "parameter_suite"})
    assert response.status_code == 200 and response.json["result"]["id"] == generic.id


def test_actual_served_template_exposes_suite_selector(web_context):
    _web, _manager, http, _payload = web_context
    response = http.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="parameterSuite"' in html
    assert "/static/web_console.js" in html


def test_node_executes_actual_static_fixed_and_beta_run_selection():
    node = shutil.which("node")
    if node is None: pytest.skip("Node is unavailable")
    script_path = Path(__file__).resolve().parents[1] / "scripts/static/web_console.js"
    # Execute the served implementation, including its actual jobPayload and
    # paramTestRunsValue functions, with a minimal DOM and no request capability.
    program = r'''
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const re = new RegExp('(?:async )?function ' + name + '\\(');
  const start = source.search(re); if (start < 0) throw Error('Missing function ' + name);
  const following = source.slice(start + 1).search(/\n(?:async )?function [A-Za-z_$]/);
  return source.slice(start, following < 0 ? source.length : start + 1 + following);
}
const nodes = new Proxy({}, {get(o,k){return o[k] ||= {value:'17',disabled:false,textContent:''};}});
const context = {workflowRequestPayload:()=>null,selectedParamWorkflow:()=>null, console, Number, Math, URLSearchParams, $: id => nodes[id],
  timeoutSecValue: () => 150, appState: {config:{defaults:{param_test_runs_max:1000}},formsByTab:{param:{
    provider:'deepseek_official',model:'deepseek-v4-pro',routeProfile:'vendor_direct',
    apiForm:'openai_fim_completions_beta',referenceContractId:'deepseek_v4_pro_0813_fim_beta',
    parameterSuite:'deepseek_fim_causal_20260908',toolValidationMode:'auto',paramTestRuns:17}}}};
vm.createContext(context);
vm.runInContext(extract('fixedParamRequestCount') + '\n' + extract('paramTestRunsValue') + '\n' + extract('jobPayload'), context);
const fixed = vm.runInContext("jobPayload('param_test')",context);
if(fixed.parameter_suite!=='deepseek_fim_causal_20260908'||fixed.param_test_runs!==1||fixed.api_form!=='openai_fim_completions_beta'||fixed.route_profile!=='vendor_direct') throw Error('Fixed suite selection lost');
context.appState.formsByTab.param.parameterSuite='';
context.appState.formsByTab.param.apiForm='deepseek_beta_chat_prefix';
context.appState.formsByTab.param.referenceContractId='deepseek_v4_pro_0813_chat_prefix_beta';
context.appState.formsByTab.param.paramTestRuns=17;
const beta=vm.runInContext("jobPayload('param_test')",context);
if(beta.param_test_runs!==1||beta.parameter_suite) throw Error('Beta repeated or inherited FIM suite');
context.appState.formsByTab.param.provider='third_party';
context.appState.formsByTab.param.paramTestRuns=17;
if(vm.runInContext("paramTestRunsValue()",context)!==17) throw Error('Official beta scope leaked to third party');
console.log(JSON.stringify({fixed:fixed.param_test_runs,beta:beta.param_test_runs}));
'''
    result = subprocess.run([node, "-e", program, str(script_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"fixed": 1, "beta": 1}


def test_node_actual_specs_create_job_and_history_keep_suite_context():
    node = shutil.which("node")
    if node is None: pytest.skip("Node is unavailable")
    script_path = Path(__file__).resolve().parents[1] / "scripts/static/web_console.js"
    program = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
function extract(name){
 const start=source.search(new RegExp('(?:async )?function '+name+'\\('));
 if(start<0)throw Error('Missing '+name);
 const next=source.slice(start+1).search(/\n(?:async )?function [A-Za-z_$]/);
 return source.slice(start,next<0?source.length:start+1+next);
}
const suite='deepseek_fim_causal_20260908';
const nodes=new Proxy({},{get(o,k){return o[k]||=( {value:'17',innerHTML:'',textContent:'',disabled:false} );}});
const form={provider:'deepseek_official',model:'deepseek-v4-pro',routeProfile:'vendor_direct',
 apiForm:'openai_fim_completions_beta',referenceContractId:'deepseek_v4_pro_0813_fim_beta',
 parameterSuite:suite,toolValidationMode:'auto',paramTestRuns:17};
const queries=[],posts=[],errors=[];
const context={workflowRequestPayload:()=>null,selectedParamWorkflow:()=>null,loadWorkflowPreview:async()=>null,refreshWorkflowPreview:async()=>null,renderParamSpecMode(){},console,Number,Math,URLSearchParams,Promise,$:id=>nodes[id],esc:String,
 appState:{config:{defaults:{param_test_runs_max:1000}},formsByTab:{param:form},paramHistoryRequestId:0},
 renderBusyState(){},renderParamResults(){},showError(msg){if(msg)errors.push(msg)},
 apiFormCapability(){return {profile_id:'exact-profile'};},
 contractById(){return {test_profile_count:15,tested_param_count:8,param_count:16};},
 referenceContractIdForModel(){return form.referenceContractId;},timeoutSecValue(){return 150;},
 isBusy(){return false;},tabForJobType(){return 'param';},selectedProviderForTab(){return {has_key:true};},
 setActiveTab(){},renderJob(job){context.renderedJob=job;},pollJob(){throw Error('Unexpected poll');}};
function response(url,opts){
 if(url==='/api/jobs'){
  posts.push(JSON.parse(opts.body));
  return Promise.resolve({ok:true,json:async()=>({id:'new-job',type:'param_test'})});
 }
 queries.push(url);
 const chosen=new URL(url,'https://console.invalid').searchParams.get('parameter_suite');
 const payload=url.startsWith('/api/param-specs') ? {parameter_suite:chosen||null,
  available_parameter_suites:[{id:suite,label:'FIM controls',request_count:8}],
  test_profiles:Array(chosen?8:15).fill('test'),fixed_request_count:chosen?8:null,comparison:[]} : {result:null};
 return Promise.resolve({ok:true,json:async()=>payload});
}
context.fetch=response;vm.createContext(context);
vm.runInContext(['fixedParamRequestCount','renderParameterSuites','paramTestRunsValue','renderParamRunHint',
 'paramSelectionKey','matchesParamSelection','loadParamSpecs','jobPayload','createJob'].map(extract).join('\n'),context);
(async()=>{
 await vm.runInContext('loadParamSpecs()',context);
 if(queries.length!==2||queries.some(url=>new URL(url,'https://console.invalid').searchParams.get('parameter_suite')!==suite))throw Error('Spec/history suite query missing');
 for(const url of queries){const q=new URL(url,'https://console.invalid').searchParams;
  if(q.get('api_form')!==form.apiForm||q.get('route_profile')!==form.routeProfile)throw Error('Query lost form/route');}
 if(nodes.parameterSuite.value!==suite||nodes.parameterSuite.disabled||nodes.paramTestRuns.value!==1||!nodes.paramTestRuns.disabled)throw Error('Fixed suite selector/runs wrong');
 await vm.runInContext("createJob('param_test')",context);
 if(posts.length!==1||posts[0].parameter_suite!==suite||posts[0].param_test_runs!==1||posts[0].api_form!==form.apiForm||posts[0].route_profile!==form.routeProfile)throw Error('Actual createJob lost fixed scope');
 const job={type:'param_test',provider:form.provider,model:form.model,route_profile:form.routeProfile,
  api_form:form.apiForm,reference_contract_id:form.referenceContractId,model_profile_id:'exact-profile',
  parameter_suite:suite,tool_validation_mode:'auto'};
 context.job=job;if(!vm.runInContext('matchesParamSelection(job)',context))throw Error('Matching suite history rejected');
 delete job.parameter_suite;if(vm.runInContext('matchesParamSelection(job)',context))throw Error('Generic history crossed fixed suite');
 const pending=[];context.fetch=(url,opts)=>new Promise(resolve=>pending.push(()=>response(url,opts).then(resolve)));
 const old=vm.runInContext('loadParamSpecs()',context);
 form.parameterSuite='';context.fetch=response;
 await vm.runInContext('loadParamSpecs()',context);
 pending.forEach(resolve=>resolve());await old;
 if(context.appState.paramSpec.parameter_suite||context.appState.paramSpec.test_profiles.length!==15||nodes.parameterSuite.value!=='')throw Error('Stale fixed response replaced generic selection');
 if(errors.length)throw Error('Unexpected frontend error '+errors.join(','));
 console.log(JSON.stringify({posts:posts.length,stale_response_ignored:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", program, str(script_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"posts": 1, "stale_response_ignored": True}
