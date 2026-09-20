"""No-network tests of the real fixed transport and CLI/Web entry points."""
import copy
import json

import pytest

from lib.client import DeepSeekClient
from lib.config import load_config
from lib import deepseek_beta_runner as runner
from lib import deepseek_beta_reference as beta
from test_deepseek_beta_reference import snapshot, plan, records


@pytest.fixture
def client(monkeypatch):
    from lib import client as client_module
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(client_module, "get_api_key", lambda *args: "offline-beta-test-secret")
    return DeepSeekClient.from_config(load_config(), "deepseek_official")


class Response:
    def __init__(self, record, *, fail=False):
        self.status_code = record["status_code"]
        self.raw = record["response_raw"].encode()
        self.fail = fail
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.raw
        if self.fail:
            raise TimeoutError("offline timeout")

    def close(self):
        self.closed = True


def dispatch(client, plan, tmp_path, responses, calls):
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]
    return runner._execute(client, plan, tmp_path, request, minimum_output_tokens=256, on_record=None)


def test_real_dispatch_uses_five_exact_bodies_and_preserves_strict_failure(client, plan, records, tmp_path):
    calls = []
    responses = [Response(r) for r in records]
    actual, observed = dispatch(client, plan, tmp_path, responses, calls)
    assert len(actual) == len(calls) == 5 and all(r.closed for r in responses)
    assert observed["bounded_observations_pass"] and not observed["pass"]
    assert observed["case_results"][1]["strict_target_pass"] is False
    for (_, url, args), item in zip(calls, plan.requests):
        assert url == beta.ENDPOINT and args["data"] == item.body_bytes
        assert args["stream"] is True and args["allow_redirects"] is False
        assert args["headers"]["Authorization"].startswith("Bearer ")
        assert json.loads(args["data"])["stream"] is False
    with pytest.raises(ValueError, match="ledger"):
        dispatch(client, plan, tmp_path, responses, calls)
    assert len(calls) == 5
    assert "offline-beta-test-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("status", [301, 401, 402, 403, 404, 408, 429, 500, 503])
def test_account_transport_and_redirect_failures_stop_without_retry(client, plan, records, tmp_path, status):
    row = {**records[0], "status_code": status}
    calls = []
    actual, observation = dispatch(client, plan, tmp_path, [Response(row)], calls)
    assert len(calls) == len(actual) == 1
    assert not observation["complete"] and not observation["pass"]


def test_timeout_after_json_keeps_transport_failure_and_stops(client, plan, records, tmp_path):
    calls = []
    actual, observation = dispatch(client, plan, tmp_path, [Response(records[0], fail=True)], calls)
    assert len(calls) == 1 and actual[0]["failure_type"] == "TimeoutError"
    assert not actual[0]["response_complete"] and not observation["case_results"][0]["pass"]


@pytest.mark.parametrize("mutation", ["provider", "origin", "path", "auth", "floor"])
def test_dispatch_rejects_changed_source_route_auth_and_output_floor(client, plan, tmp_path, mutation):
    if mutation == "provider": client.provider = "relay"
    if mutation == "origin": client.base_url = "https://other.invalid"
    if mutation == "path":
        client.api_interfaces[beta.TRANSPORT]["path"] = "/unapproved/chat/completions"
        assert client._transport_url(beta.TRANSPORT) != beta.ENDPOINT
    if mutation == "auth": client.api_interfaces[beta.TRANSPORT]["auth"] = "anthropic"
    with pytest.raises(ValueError):
        runner._execute(client, plan, tmp_path, lambda *a, **kw: pytest.fail("unexpected send"),
            minimum_output_tokens=1024 if mutation == "floor" else 256, on_record=None)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    c = load_config()
    c["providers"] = {"deepseek_official": c["providers"]["deepseek_official"]}
    c["active_provider"] = "deepseek_official"
    c["providers"]["deepseek_official"]["models"]["default_api_forms"]["deepseek-v4-pro"]["vendor_direct"] = beta.API_FORM
    return c


def test_cli_uses_fixed_runner_without_identity_count_or_generic_profiles(monkeypatch, config, client, records, tmp_path):
    from scripts import param_test as cli
    calls = []
    responses = [Response(r) for r in records]
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def mount(self, _url, adapter): assert adapter.max_retries.total == 0
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            calls.append((method, url, kwargs))
            return responses[len(calls) - 1]
    monkeypatch.setattr(runner.requests, "Session", Session)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli.DeepSeekClient, "from_config", lambda *args: client)
    monkeypatch.setattr(cli, "run_identity_probe", lambda *a, **k: pytest.fail("extra identity request"))
    monkeypatch.setattr(cli, "run_param_tests", lambda *a, **k: pytest.fail("generic profile dispatch"))
    monkeypatch.setattr(client, "count_tokens", lambda *a, **k: pytest.fail("extra count request"))
    for key in ("LOADTEST_REFERENCE_SOURCE", "LOADTEST_JOB_SPEC", "LOADTEST_PARAM_TEST_RUNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOADTEST_PROVIDER", "deepseek_official")
    monkeypatch.setenv("LOADTEST_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LOADTEST_API_FORM", beta.API_FORM)
    monkeypatch.setenv("LOADTEST_ROUTE_PROFILE", "vendor_direct")
    monkeypatch.setenv("LOADTEST_REPORT_DIR", str(tmp_path))
    # An explicit historical preset keeps the original five-observation path;
    # the default CLI now selects the broader registered v6 beta workflow.
    monkeypatch.setenv("LOADTEST_PARAM_PROFILES", ",".join(beta.CASE_IDS))
    assert cli.main() == 1
    assert len(calls) == 5
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    assert verdict["total"] == verdict["planned_requests"] == 5
    assert verdict["identity_probe"] is None and verdict["identity_probe_requests"] == 0
    assert verdict["bounded_beta_observations"]["bounded_observations_pass"]
    assert not verdict["pass"] and not verdict["full_parameter_matrix_verified"]
    assert verdict["param_specs"]["test_profiles"] == list(beta.CASE_IDS)


def test_web_job_freezes_registered_beta_cases_and_rejects_pressure(monkeypatch, config, tmp_path):
    from scripts import web_console as web
    monkeypatch.setattr(web, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(web, "provider_has_api_key", lambda *a: True)
    monkeypatch.setattr(web.JobManager, "_load_finished_jobs", lambda *a: None)
    monkeypatch.setattr(web.JobManager, "_start_locked", lambda *a: None)
    payload = {"type": "param_test", "provider": "deepseek_official", "model": "deepseek-v4-pro",
               "api_form": beta.API_FORM, "route_profile": "vendor_direct", "reference_contract_id": beta.CONTRACT_ID}
    manager = web.JobManager()
    job = manager.create(payload)
    assert job.param_test_runs == 1
    plan = job.job_spec["execution_plan"]
    assert job.job_spec["schema_version"] == 6 and job.job_spec["test_workflow_snapshot"]
    assert set(beta.CASE_IDS) <= set(plan["selected_cases"])
    assert plan["limits"]["max_requests"] >= len(beta.CASE_IDS)
    assert web._param_progress_counts(job, [1, 2])["total_cells"] == len(plan["selected_cases"])
    for runs in (True, 0, 1.0):
        manager._jobs.clear()
        with pytest.raises(ValueError):
            manager.create({**payload, "param_test_runs": runs})
    for job_type in ("quick_load", "cache_suite", "staircase", "soak"):
        manager._jobs.clear()
        with pytest.raises(ValueError):
            manager.create({**payload, "type": job_type})
