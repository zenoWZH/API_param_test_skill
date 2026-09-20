"""Actual Claude fixed transport, CLI and served Web integration without API calls."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from lib.client import DeepSeekClient
from lib.config import load_config
from lib import anthropic_prefill_reference as prefill
from lib import anthropic_prefill_runner as runner
from lib import deepseek_beta_runner as bounded
from lib import model_profile_catalog as mpdb
from test_anthropic_prefill_reference import model, records
from test_deepseek_beta_runner import Response


@pytest.fixture(scope="module")
def snapshot(model):
    """Use the real installed shared catalog; no candidate injection here."""
    catalog = mpdb.get_model_profile_catalog()
    scope = prefill.identity(model)
    return mpdb.database_snapshot({**mpdb.catalog_metadata(), **scope, "suite_family_id": "claude",
        "canonical_family_id": "claude", "catalog_resolved": True, "profile": catalog.get_profile(scope["profile_id"]),
        "interface": catalog.get_interface(scope["interface_id"]), "binding_source": "catalog_official_reference",
        "execution_target": {"provider_id": "anthropic_official", "request_model_id": model,
                             "route_profile": "vendor_direct", "api_form": prefill.API_FORM}})


@pytest.fixture
def plan(snapshot, model):
    return prefill.build_prefill_plan(snapshot, suite_id=prefill.suite_id(model))


@pytest.fixture
def client(monkeypatch):
    from lib import client as client_module
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(client_module, "get_api_key", lambda *args: "offline-prefill-secret")
    return DeepSeekClient.from_config(load_config(), "anthropic_official")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    config = load_config()
    config["providers"] = {"anthropic_official": config["providers"]["anthropic_official"]}
    config["active_provider"] = "anthropic_official"
    return config


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


def dispatch(client, plan, directory, responses, calls, *, minimum=256):
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]
    return bounded._execute(client, plan, directory, request, minimum_output_tokens=minimum, on_record=None,
        validate_plan=runner.validate_prefill_client, evaluate_plan=prefill.evaluate_prefill_plan,
        next_request=prefill.next_prefill_request, artifact_prefix="anthropic_prefill", auth_mode="anthropic")


def test_public_runner_sends_exactly_three_native_requests_once(client, plan, records, tmp_path, monkeypatch):
    calls = []
    responses = [Response(row) for row in records]
    fake_session(monkeypatch, responses, calls)
    actual, observed = runner.execute_prefill_plan(client, plan, tmp_path)
    assert len(actual) == len(calls) == 3 and observed["pass"]
    for (method, url, kwargs), request in zip(calls, plan.requests):
        assert method == "POST" and url == prefill.ENDPOINT and kwargs["data"] == request.body_bytes
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (15, 150)
        assert kwargs["headers"]["anthropic-version"] == prefill.API_VERSION
        assert kwargs["headers"]["x-api-key"] == "offline-prefill-secret" and "Authorization" not in kwargs["headers"]
        assert kwargs["stream"] is True and json.loads(kwargs["data"])["stream"] is False
    assert all(r.closed for r in responses)
    assert len(list(tmp_path.glob("anthropic_prefill_*_attempt.json"))) == 3
    assert "offline-prefill-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    with pytest.raises(ValueError, match="ledger"):
        runner.execute_prefill_plan(client, plan, tmp_path)
    assert len(calls) == 3


@pytest.mark.parametrize("status", [301, 401, 402, 403, 404, 408, 429, 500])
def test_fatal_status_stops_without_extra_request(client, plan, records, tmp_path, status):
    calls = []
    actual, observed = dispatch(client, plan, tmp_path, [Response({**records[0], "status_code": status})], calls)
    assert len(calls) == len(actual) == 1 and not observed["pass"] and not observed["complete"]


def test_transport_timeout_preserves_failure_after_valid_json(client, plan, records, tmp_path):
    calls = []
    response = Response(records[0], fail=True)
    actual, observed = dispatch(client, plan, tmp_path, [response], calls)
    assert len(calls) == 1 and response.closed and actual[0]["failure_type"] == "TimeoutError"
    assert not observed["pass"] and not observed["case_results"][0]["pass"]


@pytest.mark.parametrize("mutation", ["provider", "origin", "path", "auth", "floor", "subclass"])
def test_outbound_scope_fails_before_any_attempt(client, plan, tmp_path, mutation):
    if mutation == "provider": client.provider = "relay"
    elif mutation == "origin": client.base_url = "https://other.invalid"
    elif mutation == "path": client.api_interfaces[prefill.TRANSPORT]["path"] = "/unapproved/messages"
    elif mutation == "auth": client.api_interfaces[prefill.TRANSPORT]["auth"] = "bearer"
    elif mutation == "subclass":
        class AlteredPlan(prefill.PrefillPlan):
            @property
            def requests(self): return ()
        plan = AlteredPlan(plan._snapshot_bytes, plan.suite_id)
    calls = []
    with pytest.raises(ValueError):
        dispatch(client, plan, tmp_path, [], calls, minimum=4096 if mutation == "floor" else 256)
    assert calls == [] and not list(tmp_path.iterdir())


def cli_setup(monkeypatch, config, client, tmp_path, model):
    from scripts import param_test as cli
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli.DeepSeekClient, "from_config", lambda *a: client)
    monkeypatch.setattr(cli, "run_identity_probe", lambda *a, **k: pytest.fail("extra identity request"))
    monkeypatch.setattr(cli, "run_param_tests", lambda *a, **k: pytest.fail("generic profile dispatch"))
    monkeypatch.setattr(client, "count_tokens", lambda *a, **k: pytest.fail("extra token count request"))
    for key in ("LOADTEST_REFERENCE_SOURCE", "LOADTEST_JOB_SPEC", "LOADTEST_PARAM_TEST_RUNS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LOADTEST_PROVIDER": "anthropic_official", "LOADTEST_MODEL": model,
        "LOADTEST_API_FORM": prefill.API_FORM, "LOADTEST_ROUTE_PROFILE": "vendor_direct",
        "LOADTEST_PARAMETER_SUITE": prefill.suite_id(model), "LOADTEST_REPORT_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    return cli


def test_actual_cli_dispatches_three_without_identity_count_or_generic(monkeypatch, config, client, records, model, tmp_path, capsys):
    cli = cli_setup(monkeypatch, config, client, tmp_path, model)
    calls = []
    fake_session(monkeypatch, [Response(row) for row in records], calls)
    assert cli.main() in (0, 1)
    capsys.readouterr()
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    rows = json.loads((tmp_path / "param_results.json").read_text())
    assert len(calls) == verdict["total"] == verdict["planned_requests"] == 3
    assert verdict["parameter_suite"] == prefill.suite_id(model)
    assert verdict["identity_probe"] is None and verdict["identity_probe_requests"] == 0
    assert verdict["bounded_prefill_observations"]["pass"] and not verdict["full_parameter_matrix_verified"]
    assert len(verdict["param_specs"]["test_profiles"]) == 3
    assert rows[2]["status"] == "expected_rejection" and all(row["compatibility_pass"] for row in rows)
    assert all(row["provider"] == "anthropic_official" and row["model"] == model and row["transport"] == prefill.TRANSPORT for row in rows)
    assert rows[0]["response_raw"] == records[0]["response_raw"]
    assert "offline-prefill-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())


def test_cli_uses_frozen_job_snapshot_without_resolving_replacement_suite(monkeypatch, config, client, records, snapshot, model, tmp_path, capsys):
    cli = cli_setup(monkeypatch, config, client, tmp_path, model)
    frozen = {"type": "param_test", "parameter_suite": prefill.suite_id(model), "model_profile_database": copy.deepcopy(snapshot)}
    monkeypatch.setattr(cli, "load_job_spec", lambda *a: copy.deepcopy(frozen))
    monkeypatch.setattr(cli, "resolve_runtime_parameter_config", lambda *a, **k: pytest.fail("replaced immutable suite snapshot"))
    calls = []
    fake_session(monkeypatch, [Response(row) for row in records], calls)
    assert cli.main() in (0, 1)
    capsys.readouterr()
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    assert len(calls) == 3
    assert verdict["model_profile_database"]["snapshot_digest"] == snapshot["snapshot_digest"]
    assert verdict["bounded_prefill_observations"]["snapshot_digest"] == snapshot["snapshot_digest"]


def test_cli_fixed_suite_refuses_non_parameter_job_snapshot(monkeypatch, config, client, snapshot, model, tmp_path):
    cli = cli_setup(monkeypatch, config, client, tmp_path, model)
    monkeypatch.setattr(cli, "load_job_spec", lambda *a: {"type": "quick_load", "parameter_suite": prefill.suite_id(model),
                                                        "model_profile_database": copy.deepcopy(snapshot)})
    calls = []
    fake_session(monkeypatch, [], calls)
    with pytest.raises(ValueError): cli.main()
    assert calls == []


@pytest.mark.parametrize("mutation", ["unknown", "repeat", "wrong_form", "other_model_suite", "job_conflict"])
def test_cli_selection_failure_never_sends(monkeypatch, config, client, model, tmp_path, mutation):
    cli = cli_setup(monkeypatch, config, client, tmp_path, model)
    calls = []
    fake_session(monkeypatch, [], calls)
    if mutation == "unknown": monkeypatch.setenv("LOADTEST_PARAMETER_SUITE", "unknown")
    elif mutation == "repeat": monkeypatch.setenv("LOADTEST_PARAM_TEST_RUNS", "2")
    elif mutation == "wrong_form": monkeypatch.setenv("LOADTEST_API_FORM", "openai_chat_completions")
    elif mutation == "other_model_suite": monkeypatch.setenv("LOADTEST_PARAMETER_SUITE", prefill.suite_id(next(m for m in prefill.MODELS if m != model)))
    else: monkeypatch.setattr(cli, "load_job_spec", lambda *a: {"parameter_suite": None})
    with pytest.raises((ValueError, RuntimeError)):
        cli.main()
    assert calls == []


@pytest.fixture
def web_context(monkeypatch, config, model, tmp_path):
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
    payload = {"type": "param_test", "provider": "anthropic_official", "model": model,
        "api_form": prefill.API_FORM, "route_profile": "vendor_direct", "reference_contract_id": prefill.CONTRACT_ID,
        "parameter_suite": prefill.suite_id(model)}
    return web, manager, web.app.test_client(), payload


def test_web_specs_and_job_freeze_three_and_keep_generic_thirty(web_context):
    web, manager, http, payload = web_context
    query = {k: payload[k] for k in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    query["contract_id"] = prefill.CONTRACT_ID
    fixed = http.get("/api/param-specs", query_string=query)
    assert fixed.status_code == 200 and fixed.json["fixed_request_count"] == len(fixed.json["test_profiles"]) == 3
    generic = http.get("/api/param-specs", query_string={k: v for k, v in query.items() if k != "parameter_suite"})
    assert generic.status_code == 200 and len(generic.json["test_profiles"]) == 30
    assert generic.json["available_parameter_suites"][0]["id"] == payload["parameter_suite"]
    response = http.post("/api/jobs", json=payload)
    assert response.status_code == 201
    job = manager._jobs[response.json["id"]]
    assert job.param_test_runs == 1 and job.job_spec["parameter_suite"] == payload["parameter_suite"]
    assert json.loads((job.report_dir / "job_spec.json").read_text())["parameter_suite"] == payload["parameter_suite"]
    assert web._param_progress_counts(job, [1])["total_cells"] == 3


@pytest.mark.parametrize("change", [{"parameter_suite": "unknown"}, {"parameter_suite": True}, {"param_test_runs": True},
    {"param_test_runs": 1.0}, {"param_test_runs": 2}, {"param_test_runs": 0}, {"type": "quick_load"}, {"type": "cache_suite"},
    {"api_form": "openai_chat_completions"}, {"model": "claude-fable-5"}])
def test_web_scope_errors_fail_before_queue(web_context, change):
    _web, manager, http, payload = web_context
    response = http.post("/api/jobs", json={**payload, **change})
    assert response.status_code == 400 and not manager._jobs


def test_result_history_cannot_reuse_generic_other_suite_or_incomplete_success(web_context, records):
    from lib.job_spec import _result_identity_matches
    _web, manager, _http, payload = web_context
    job = manager.create(payload)
    plan = prefill.build_prefill_plan(job.job_spec["model_profile_database"], suite_id=payload["parameter_suite"])
    result = {**copy.deepcopy(job.job_spec), "planned_requests": 3, "pass": True,
              "bounded_prefill_observations": prefill.evaluate_prefill_plan(plan, records)}
    assert _result_identity_matches(job.job_spec, result)[0]
    wrong = copy.deepcopy(result)
    wrong.pop("parameter_suite")
    assert "parameter_suite_mismatch" in _result_identity_matches(job.job_spec, wrong)[1]
    wrong = copy.deepcopy(result)
    wrong["bounded_prefill_observations"]["case_results"].pop()
    assert "fixed_suite_results_incomplete" in _result_identity_matches(job.job_spec, wrong)[1]


def test_latest_history_filters_fixed_suite_separately(web_context, monkeypatch):
    _web, manager, http, payload = web_context
    fixed = manager.create(payload)
    fixed.status, fixed.finished_at, fixed.process = "failed", 10.0, None
    ids = [payload["parameter_suite"] + "/" + label for label in prefill.LABELS]
    (fixed.report_dir / "verdict.json").write_text(json.dumps({"param_specs": {"test_profiles": ids}}))
    generic = copy.deepcopy(fixed)
    generic.id += "-generic"
    generic.report_dir = fixed.report_dir.parent / generic.id
    generic.report_dir.mkdir()
    (generic.report_dir / "verdict.json").write_text(json.dumps({"param_specs": {"test_profiles": fixed.job_spec["model_profile_database"]["parameter_test_binding"]["test_cases"]}}))
    generic.job_spec.pop("parameter_suite")
    generic.finished_at = 20.0
    manager._jobs[generic.id] = generic
    monkeypatch.setattr(manager, "_refresh_locked", lambda job: job)
    monkeypatch.setattr(manager, "public", lambda job, **kw: {"id": job.id, "parameter_suite": job.job_spec.get("parameter_suite")})
    query = {k: payload[k] for k in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    query["contract_id"] = prefill.CONTRACT_ID
    assert http.get("/api/param-results/latest", query_string=query).json["result"]["id"] == fixed.id
    assert http.get("/api/param-results/latest", query_string={k: v for k, v in query.items() if k != "parameter_suite"}).json["result"]["id"] == generic.id


def test_actual_served_assets_and_js_use_three_requests_for_only_exact_model_suite(web_context):
    _web, _manager, http, _payload = web_context
    html = http.get("/").get_data(as_text=True)
    assert 'id="parameterSuite"' in html and "/static/web_console.js" in html
    node = shutil.which("node")
    if node is None: pytest.skip("Node unavailable")
    script = Path(__file__).resolve().parents[1] / "scripts/static/web_console.js"
    assert http.get("/static/web_console.js").get_data(as_text=True) == script.read_text()
    program = r'''
const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
function extract(name){const start=source.search(new RegExp('(?:async )?function '+name+'\\('));
 const next=source.slice(start+1).search(/\n(?:async )?function [A-Za-z_$]/);return source.slice(start,next<0?source.length:start+1+next);}
const models=['claude-haiku-4-5-20251001','claude-opus-4-5-20251101','claude-sonnet-4-5-20250929'];
const ctx={workflowRequestPayload:()=>null,selectedParamWorkflow:()=>null,Number,Math,URLSearchParams,$:()=>({value:17}),timeoutSecValue:()=>150,
 appState:{config:{defaults:{param_test_runs_max:1000}},formsByTab:{param:{provider:'anthropic_official',
 model:models[0],apiForm:'anthropic_messages',routeProfile:'vendor_direct',referenceContractId:'claude_native_messages',
 toolValidationMode:'auto',paramTestRuns:17}}}};
vm.createContext(ctx);vm.runInContext(extract('fixedParamRequestCount')+'\n'+extract('paramTestRunsValue')+'\n'+extract('jobPayload'),ctx);
const f=ctx.appState.formsByTab.param;
for(const model of models){f.model=model;f.parameterSuite='anthropic_prefill_'+model.replaceAll('-','_')+'_20260908';
 if(vm.runInContext('fixedParamRequestCount()',ctx)!==3)throw Error('wrong count');
 const job=vm.runInContext("jobPayload('param_test')",ctx);
 if(job.param_test_runs!==1||job.parameter_suite!==f.parameterSuite)throw Error('lost fixed selection');}
f.model='claude-fable-5';if(vm.runInContext('fixedParamRequestCount()',ctx)!==0)throw Error('crossed model');
f.model=models[0];if(vm.runInContext('fixedParamRequestCount()',ctx)!==0)throw Error('crossed suite');
'''
    subprocess.run([node, "-e", program, str(script)], check=True, capture_output=True, text=True)
