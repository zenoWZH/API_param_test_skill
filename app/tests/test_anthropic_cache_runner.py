"""Bounded count/generation transport and actual App entry points, without API traffic."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.client import DeepSeekClient
from lib.config import load_config
from lib import anthropic_cache_reference as cache
from lib import anthropic_cache_runner as runner
from lib import approved_report_retention as retention
from lib import model_profile_catalog as mpdb
from test_anthropic_cache_reference import author, shared, candidate_catalog, snapshots, snapshot, plan, records_for, _plan


def cli_success_records(plan):
    """Mock usage fits the long fixture as well as the cache causal controls."""
    rows = records_for(plan)
    total = 1024 if plan.model == "claude-opus-5" else 8192
    for index, row in enumerate(rows):
        payload = json.loads(row["response_raw"])
        if row["request_kind"] == "count":
            payload["input_tokens"] = total
        else:
            usage = payload["usage"]
            usage["input_tokens"] = total - usage["cache_read_input_tokens"] - usage["cache_creation_input_tokens"]
        row["response_raw"] = cache.canonical_bytes(payload)
    return rows


class Response:
    def __init__(self, record, *, status=200, fail=False):
        raw = record["response_raw"]
        self.raw = raw.encode() if isinstance(raw, str) else raw
        self.status_code, self.fail, self.closed = status, fail, False
    def iter_content(self, chunk_size):
        yield self.raw
        if self.fail: raise TimeoutError("offline incomplete response")
    def close(self): self.closed = True


@pytest.fixture
def client(monkeypatch):
    from lib import client as client_module
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(client_module, "get_api_key", lambda *args: "offline-cache-secret")
    return DeepSeekClient.from_config(load_config(), "anthropic_official")


@pytest.fixture
def report(tmp_path, monkeypatch, plan):
    monkeypatch.setenv("LLM_API_TEST_REPORTS_DIR", str(tmp_path))
    batch = retention.cache_report_directory()
    retention.initialize_cache_report(batch, plan.suite_id, on_exit=False)
    return batch


def session_fixture(monkeypatch, responses, calls):
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def mount(self, url, adapter): assert url == "https://" and adapter.max_retries.total == 0
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            calls.append((method, url, kwargs))
            return responses[len(calls) - 1]
    monkeypatch.setattr(runner.requests, "Session", Session)


@pytest.mark.parametrize("model", cache.MODELS)
def test_public_transport_sends_exact_five_frozen_requests_and_registers_every_closed_file(model, snapshots, client, tmp_path, monkeypatch):
    plan = _plan(snapshots[model])
    monkeypatch.setenv("LLM_API_TEST_REPORTS_DIR", str(tmp_path))
    report = retention.cache_report_directory()
    retention.initialize_cache_report(report, plan.suite_id, on_exit=False)
    random = Mock(side_effect=AssertionError("Dispatch must not randomize"))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    responses, calls = [Response(row) for row in records_for(plan)], []
    session_fixture(monkeypatch, responses, calls)
    original_floor = runner.enforce_parameter_test_output_limit
    floor_kinds = []
    def generation_floor(body, transport, **kwargs):
        assert "max_tokens" in body, "Count endpoint entered the output floor"
        floor_kinds.append("generation")
        return original_floor(body, transport, **kwargs)
    monkeypatch.setattr(runner, "enforce_parameter_test_output_limit", generation_floor)
    actual, observed = runner.execute_cache_plan(client, plan, report)
    assert len(actual) == len(calls) == 5 and observed["pass"] and observed["cache_effect_verified"]
    assert floor_kinds and all(response.closed for response in responses)
    for (method, url, kwargs), request in zip(calls, plan.requests):
        assert method == "POST" and url == request.endpoint and kwargs["data"] == request.body_bytes
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (15, 150)
        assert kwargs["headers"]["x-api-key"] == "offline-cache-secret"
        assert kwargs["headers"]["anthropic-version"] == cache.API_VERSION
    assert [url for _, url, _ in calls] == [cache.COUNT_ENDPOINT] * 2 + [cache.ENDPOINT] * 3
    meta = json.loads((report / ".report-retention.json").read_text())
    assert len(meta["owned_files"]) == 14 and meta["period"] == "P1M"
    assert {row["path"] for row in meta["owned_files"]} == {str(path.relative_to(report)) for path in report.rglob("*") if path.is_file() and path.name != ".report-retention.json"}
    assert "offline-cache-secret" not in "".join(path.read_text() for path in report.rglob("*") if path.is_file())
    with pytest.raises(ValueError): runner.execute_cache_plan(client, plan, report)
    assert len(calls) == 5 and not random.called


@pytest.mark.parametrize("failure", ["count_range", "count_type", "count_error", "redirect", "auth", "quota", "server", "incomplete"])
def test_invalid_first_count_stops_before_any_generation(failure, client, plan, report, monkeypatch):
    row = records_for(plan)[0]
    status = {"redirect": 302, "auth": 403, "quota": 429, "server": 503}.get(failure, 200)
    if failure in {"count_range", "count_type", "count_error"}:
        row = {**row, "response_raw": cache.canonical_bytes({"input_tokens": 1 if failure == "count_range" else True}
            if failure != "count_error" else {"error": {"type": "invalid_request_error"}})}
    calls = []
    session_fixture(monkeypatch, [Response(row, status=status, fail=failure == "incomplete")], calls)
    actual, observed = runner.execute_cache_plan(client, plan, report)
    assert len(actual) == len(calls) == 1 and not observed["pass"]
    assert observed["generation_requests_recorded"] == 0 and calls[0][1] == cache.COUNT_ENDPOINT


@pytest.mark.parametrize("mutation", ["provider", "origin", "path", "auth", "floor", "preview"])
def test_invalid_client_or_preview_never_enters_transport(mutation, client, plan, report, monkeypatch):
    if mutation == "provider": client.provider = "gateway"
    elif mutation == "origin": client.base_url = "https://wrong.invalid"
    elif mutation == "path": client.api_interfaces[cache.TRANSPORT]["path"] = "/unapproved/messages"
    elif mutation == "auth": client.api_interfaces[cache.TRANSPORT]["auth"] = "bearer"
    elif mutation == "preview": plan = cache.build_cache_plan(plan.snapshot, suite_id=plan.suite_id)
    calls = []
    session_fixture(monkeypatch, [], calls)
    with pytest.raises(ValueError): runner.execute_cache_plan(client, plan, report, minimum_output_tokens=4096 if mutation == "floor" else 256)
    assert calls == [] and not list(report.glob("anthropic_cache_*.json"))


def test_closed_partial_immutable_file_is_owned_even_when_write_fails(report, monkeypatch):
    from lib import deepseek_beta_runner as bounded
    original_open = Path.open
    target = report / "anthropic_cache_dispatch_started.json"
    class Partial:
        def __enter__(self):
            self.stream = original_open(target, "xb")
            return self
        def __exit__(self, *args): self.stream.close()
        def write(self, value):
            self.stream.write(value[:1])
            raise OSError("offline interrupted file write")
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs:
        Partial() if path == target and args == ("xb",) else original_open(path, *args, **kwargs))
    with pytest.raises(OSError): bounded._write_new(report, target.name, {"request_cap": 5})
    metadata = json.loads((report / ".report-retention.json").read_text())
    assert [row["path"] for row in metadata["owned_files"]] == [target.name]


@pytest.fixture
def configured(candidate_catalog, monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(mpdb, "get_model_profile_catalog", lambda: candidate_catalog)
    config = load_config()
    config["providers"] = {"anthropic_official": config["providers"]["anthropic_official"]}
    config["active_provider"] = "anthropic_official"
    return config


def cli_setup(monkeypatch, configured, client, plan, tmp_path, *, frozen=True, actual_constructor=False):
    from scripts import param_test as cli
    monkeypatch.setenv("LLM_API_TEST_REPORTS_DIR", str(tmp_path))
    batch = retention.cache_report_directory()
    monkeypatch.setattr(cli, "load_config", lambda: copy.deepcopy(configured))
    if not actual_constructor:
        monkeypatch.setattr(cli.DeepSeekClient, "from_config", lambda *args: client)
    monkeypatch.setattr(cli, "run_identity_probe", lambda *a, **k: pytest.fail("extra identity request"))
    monkeypatch.setattr(cli, "run_param_tests", lambda *a, **k: pytest.fail("generic matrix dispatch"))
    monkeypatch.setattr(client, "count_tokens", lambda *a, **k: pytest.fail("extra implicit count request"))
    job = {"type": "param_test", "parameter_suite": plan.suite_id,
           "model_profile_database": plan.snapshot, "fixed_parameter_plan": plan.frozen_payload} if frozen else None
    monkeypatch.setattr(cli, "load_job_spec", lambda *args: copy.deepcopy(job))
    for key in ("LOADTEST_REFERENCE_SOURCE", "LOADTEST_JOB_SPEC", "LOADTEST_PARAM_TEST_RUNS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LOADTEST_PROVIDER": "anthropic_official", "LOADTEST_MODEL": plan.model,
        "LOADTEST_API_FORM": cache.API_FORM, "LOADTEST_ROUTE_PROFILE": "vendor_direct",
        "LOADTEST_PARAMETER_SUITE": plan.suite_id, "LOADTEST_REPORT_DIR": str(batch)}.items():
        monkeypatch.setenv(key, value)
    return cli, batch, job


@pytest.mark.parametrize("model", ["claude-sonnet-4-5-20250929", "claude-opus-5"])
def test_cli_really_constructs_the_configured_v1_client_before_mocked_http(model, client, tmp_path, monkeypatch, capsys):
    catalog = mpdb.get_model_profile_catalog()
    exact = cache.identity(model)
    snapshot = mpdb.database_snapshot({**mpdb.catalog_metadata(), **exact, "suite_family_id": "claude",
        "canonical_family_id": "claude", "catalog_resolved": True, "profile": catalog.get_profile(exact["profile_id"]),
        "interface": catalog.get_interface(exact["interface_id"]), "binding_source": "catalog_official_reference",
        "execution_target": {"provider_id": "anthropic_official", "request_model_id": model,
            "route_profile": "vendor_direct", "api_form": cache.API_FORM, "transport": cache.TRANSPORT}})
    plan = _plan(snapshot)
    cli, batch, _ = cli_setup(monkeypatch, load_config(), client, plan, tmp_path, actual_constructor=True)
    calls, responses = [], [Response(row) for row in cli_success_records(plan)]
    import requests
    def http(session, method, url, **kwargs):
        assert session.trust_env is False
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]
    # Preserve Session construction and the real from_config classmethod.
    monkeypatch.setattr(requests.sessions.Session, "request", http)
    assert client.base_url == "https://api.anthropic.com/v1"
    assert cli.main() == 0
    capsys.readouterr()
    result = json.loads((batch / "verdict.json").read_text())
    assert len(calls) == 5 and result["bounded_cache_observations"]["cache_effect_verified"]
    assert [url for _, url, _ in calls] == [cache.COUNT_ENDPOINT] * 2 + [cache.ENDPOINT] * 3
    assert all(call[2]["headers"]["x-api-key"] == "offline-cache-secret" for call in calls)


def test_actual_cli_uses_frozen_five_body_job_and_skips_count_generation_audit(configured, client, plan, tmp_path, monkeypatch, capsys):
    cli, batch, _ = cli_setup(monkeypatch, configured, client, plan, tmp_path)
    random = Mock(side_effect=AssertionError("Frozen CLI job randomized"))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    audit = cli._audit_exchange_safely
    audited_bodies = []
    def generation_audit(config, body, *args, **kwargs):
        assert "max_tokens" in body, "Count response entered generated token auditing"
        audited_bodies.append(copy.deepcopy(body))
        return audit(config, body, *args, **kwargs)
    monkeypatch.setattr(cli, "_audit_exchange_safely", generation_audit)
    calls = []
    session_fixture(monkeypatch, [Response(row) for row in cli_success_records(plan)], calls)
    assert cli.main() == 0
    capsys.readouterr()
    verdict = json.loads((batch / "verdict.json").read_text())
    rows = json.loads((batch / "param_results.json").read_text())
    assert len(calls) == verdict["planned_requests"] == verdict["total"] == 5
    assert verdict["bounded_cache_observations"]["pass"] and verdict["fixed_parameter_plan_digest"] == plan.plan_digest
    assert verdict["identity_probe"] is None and verdict["count_precision"] == "official_estimate" and not verdict["token_exact_proof"]
    assert [row["request_kind"] for row in rows] == ["count", "count", "generation", "generation", "generation"]
    assert all(row["token_validation_status"] == "not_applicable" for row in rows[:2])
    assert audited_bodies and not random.called
    assert json.loads((batch / "fixed_parameter_plan.json").read_text()) == plan.frozen_payload
    assert len(json.loads((batch / ".report-retention.json").read_text())["owned_files"]) == 22


def test_cli_missing_frozen_job_plan_is_not_recreated(configured, client, plan, tmp_path, monkeypatch):
    cli, _batch, job = cli_setup(monkeypatch, configured, client, plan, tmp_path)
    job.pop("fixed_parameter_plan")
    monkeypatch.setattr(cli, "load_job_spec", lambda *args: job)
    monkeypatch.setattr(cache.secrets, "token_hex", Mock(side_effect=AssertionError("Missing plan was regenerated")))
    with pytest.raises(ValueError, match="missing its frozen"):
        cli.main()


def test_only_exact_frozen_count_wires_are_excluded_from_generation_audits(plan):
    rows = [{"name": request.case_id, "profile": request.case_id, "request_kind": request.kind,
        "request_endpoint": request.endpoint, "request_body": request.body} for request in plan.requests]
    assert cache.generation_token_audit_results(plan, rows) == rows[2:]
    forged_generation = copy.deepcopy(rows)
    forged_generation[2]["request_kind"] = "count"
    assert cache.generation_token_audit_results(plan, forged_generation) == forged_generation[2:]
    forged_count = copy.deepcopy(rows)
    forged_count[0]["request_body"]["model"] = "another-model"
    assert cache.generation_token_audit_results(plan, forged_count) == [forged_count[0], *forged_count[2:]]
    mismatched_identity = copy.deepcopy(rows)
    mismatched_identity[0]["name"] = rows[2]["name"]
    assert len(cache.generation_token_audit_results(plan, mismatched_identity)) == 4


def test_implausible_generation_usage_still_fails_after_count_exclusion(configured, client, plan, tmp_path, monkeypatch, capsys):
    cli, batch, _ = cli_setup(monkeypatch, configured, client, plan, tmp_path)
    calls = []
    # The old minimum-only mock reports too little input for this long fixture.
    # Keep it as a negative example rather than weakening gross plausibility.
    session_fixture(monkeypatch, [Response(row) for row in records_for(plan)], calls)
    code = cli.main()
    capsys.readouterr()
    result = json.loads((batch / "verdict.json").read_text())
    assert code == 1 and result["pass"] is False and result["bounded_cache_observations"]["pass"] is True
    summary = result["token_audit_summary"]
    assert summary["missing_audit_result_count"] == 0 and summary["gross_failure_count"] == 3
    assert summary["validation_failure_count"] == 3 and result["token_validation_pass"] is False


def test_standalone_cli_creates_one_discoverable_job_and_exactly_two_nonces(configured, client, plan, tmp_path, monkeypatch, capsys):
    plan = cache.build_cache_plan(plan.snapshot, suite_id=plan.suite_id, create_runtime_nonce=True)
    cli, batch, _ = cli_setup(monkeypatch, configured, client, plan, tmp_path, frozen=False)
    random = Mock(side_effect=["3" * 32, "4" * 32])
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    calls = []
    session_fixture(monkeypatch, [Response(row) for row in cli_success_records(plan)], calls)
    assert cli.main() == 0
    capsys.readouterr()
    job = json.loads((batch / "job_spec.json").read_text())
    frozen = json.loads((batch / "fixed_parameter_plan.json").read_text())
    assert random.call_count == 2 and job["fixed_parameter_plan"] == frozen
    assert job["parameter_suite"] == plan.suite_id and job["type"] == "param_test"
    assert frozen["nonces"] == {"positive": "3" * 32, "negative": "4" * 32} and len(calls) == 3
    assert frozen["schema_version"] == 2 and frozen["request_cap"] == 3
    random.side_effect = AssertionError("Existing job was recreated")
    with pytest.raises(ValueError): cli.main()
    assert len(calls) == 3 and random.call_count == 2


@pytest.mark.parametrize("model", ["claude-sonnet-4-5-20250929", "claude-opus-5"])
@pytest.mark.parametrize("version", [1, 2])
def test_actual_installed_suite_snapshot_reaches_cli_without_candidate_injection(model, version, client, tmp_path, monkeypatch, capsys):
    catalog = mpdb.get_model_profile_catalog()
    exact = cache.identity(model)
    snapshot = mpdb.database_snapshot({**mpdb.catalog_metadata(), **exact, "suite_family_id": "claude",
        "canonical_family_id": "claude", "catalog_resolved": True, "profile": catalog.get_profile(exact["profile_id"]),
        "interface": catalog.get_interface(exact["interface_id"]), "binding_source": "catalog_official_reference",
        "execution_target": {"provider_id": "anthropic_official", "request_model_id": model,
            "route_profile": "vendor_direct", "api_form": cache.API_FORM, "transport": cache.TRANSPORT}})
    plan = _plan(snapshot) if version == 1 else cache.build_cache_plan(snapshot,
        suite_id=cache.suite_id(model), create_runtime_nonce=True)
    config = load_config()
    cli, batch, _ = cli_setup(monkeypatch, config, client, plan, tmp_path)
    calls = []
    session_fixture(monkeypatch, [Response(row) for row in cli_success_records(plan)], calls)
    assert cli.main() == 0
    capsys.readouterr()
    verdict = json.loads((batch / "verdict.json").read_text())
    assert len(calls) == plan.request_cap and verdict["planned_requests"] == plan.request_cap
    assert verdict["bounded_cache_observations"]["pass"]
    assert verdict["count_requests"] == (2 if version == 1 else 0) and verdict["generation_requests"] == 3
    assert verdict["model_profile_database"]["catalog_digest"] == catalog.digest


@pytest.fixture
def web_context(configured, plan, tmp_path, monkeypatch):
    from scripts import web_console as web
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    monkeypatch.setenv("LLM_API_TEST_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(configured))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(web, "REPORTS_ROOT", tmp_path)
    monkeypatch.setattr(web, "provider_has_api_key", lambda *args: True)
    monkeypatch.setattr(web.JobManager, "_load_finished_jobs", lambda *args: None)
    monkeypatch.setattr(web.JobManager, "_start_locked", lambda *args: None)
    manager = web.JobManager()
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    body = {"type": "param_test", "provider": "anthropic_official", "model": plan.model,
        "api_form": cache.API_FORM, "route_profile": "vendor_direct", "reference_contract_id": cache.CONTRACT_ID,
        "parameter_suite": plan.suite_id}
    return web, manager, web.app.test_client(), body


def test_real_web_lists_prefill_and_cache_without_random_preview_and_freezes_once(web_context, monkeypatch):
    web, manager, http, body = web_context
    random = Mock(side_effect=AssertionError("Preview generated nonce"))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    query = {key: body[key] for key in ("provider", "model", "api_form", "route_profile", "parameter_suite")}
    query["contract_id"] = cache.CONTRACT_ID
    fixed = http.get("/api/param-specs", query_string=query)
    assert fixed.status_code == 200 and fixed.json["fixed_request_count"] == 3
    assert fixed.json["count_request_count"] == 0 and fixed.json["minimum_prefix_tokens"] is None
    assert len(fixed.json["available_parameter_suites"]) == 2
    generic = http.get("/api/param-specs", query_string={k: v for k, v in query.items() if k != "parameter_suite"})
    assert generic.status_code == 200 and len(generic.json["test_profiles"]) == 30 and not random.called
    random.side_effect = ["3" * 32, "4" * 32]
    response = http.post("/api/jobs", json=body)
    assert response.status_code == 201
    job = manager._jobs[response.json["id"]]
    assert random.call_count == 2 and job.param_test_runs == 1
    saved = json.loads((job.report_dir / "job_spec.json").read_text())
    assert saved["fixed_parameter_plan"] == job.job_spec["fixed_parameter_plan"]
    random.side_effect = AssertionError("Progress or history randomized")
    assert web._param_progress_counts(job, [1, 2])["total_cells"] == 3 and random.call_count == 2
    frozen = cache.build_cache_plan(job.job_spec["model_profile_database"], suite_id=body["parameter_suite"], frozen_plan=saved["fixed_parameter_plan"])
    from lib.job_spec import _result_identity_matches
    result = {**copy.deepcopy(job.job_spec), "pass": True, "planned_requests": 3,
        "fixed_parameter_plan_digest": frozen.plan_digest, "bounded_cache_observations": cache.evaluate_cache_plan(frozen, records_for(frozen))}
    assert _result_identity_matches(job.job_spec, result)[0]
    changed_policy = copy.deepcopy(result)
    changed_policy["bounded_cache_observations"]["cache_evaluation_policy"]["expected_hits"] = [0, 1, 0]
    assert "fixed_cache_evaluation_policy_mismatch" in _result_identity_matches(job.job_spec, changed_policy)[1]
    result["fixed_parameter_plan_digest"] = "0" * 64
    assert "fixed_cache_plan_digest_mismatch" in _result_identity_matches(job.job_spec, result)[1]


def test_legacy_web_progress_keeps_five_frozen_requests(web_context, plan):
    web, manager, http, body = web_context
    response = http.post("/api/jobs", json=body)
    assert response.status_code == 201
    job = manager._jobs[response.json["id"]]
    historical_job = copy.copy(job)
    historical_job.job_spec = copy.deepcopy(job.job_spec)
    historical_job.job_spec["schema_version"] = 4
    historical_job.job_spec.pop("execution_plan", None)
    historical_job.job_spec.pop("parameter_execution", None)
    historical_job.job_spec["model_profile_database"] = plan.snapshot
    historical_job.job_spec["fixed_parameter_plan"] = plan.frozen_payload
    assert web._param_progress_counts(historical_job, [1, 2])["total_cells"] == 5


@pytest.mark.parametrize("change", [{"param_test_runs": 2}, {"param_test_runs": True}, {"type": "cache_suite"},
    {"type": "quick_load"}, {"model": "claude-fable-5"}, {"provider": "gateway"}, {"parameter_suite": "unknown"}])
def test_web_scope_errors_never_enqueue_or_randomize(web_context, monkeypatch, change):
    _web, manager, http, body = web_context
    random = Mock(side_effect=AssertionError("Invalid job generated nonce"))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    result = http.post("/api/jobs", json={**body, **change})
    assert result.status_code == 400 and not manager._jobs and not random.called
