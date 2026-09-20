from __future__ import annotations

import copy
import json

import pytest
import requests

from lib.client import ChatResult, OpenAICompatibleClient, _normalized_interfaces
from lib.config import get_active_provider_name, get_provider_config, get_selected_model, load_config
from lib.parameter_job_controls import freeze_workflow_job, make_workflow_execution, workflow_execution_result_state
from lib.job_spec import make_job_spec
from lib.test_runner import IntegrityError, PlanValidationError
from lib.test_runner.adapters.legacy_parameter import execute_legacy_parameter_plan, prepare_legacy_parameter_plan
from scripts import param_test


SOURCE = "deepseek_v4_pro_0813_responses"
PROFILE = "deepseek0813_responses_basic"
ARGS = ("deepseek_official", "deepseek-v4-pro", "deepseek", SOURCE, "deepseek")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/workflow-missing-private-overlay")
    for key in ("LOADTEST_PROVIDER", "LOADTEST_MODEL", "LOADTEST_ROUTE_PROFILE", "LOADTEST_API_FORM", "LOADTEST_PARAM_PROFILES", "LOADTEST_TOOL_VALIDATION_MODE"):
        monkeypatch.delenv(key, raising=False)
    return load_config()


def prepare(config, **kwargs):
    return prepare_legacy_parameter_plan(config, *ARGS, profiles=[PROFILE], capability_profile={"parameter_test_enabled": True}, **kwargs)[0]


def profile_step(plan):
    return next(step for step in plan["ordered_steps"] if step["id"] == PROFILE)


def chat_result():
    usage = {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}
    payload = {"model": "deepseek-v4-pro", "status": "completed", "store": False, "parallel_tool_calls": True,
               "previous_response_id": None, "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}], "usage": usage}
    return ChatResult(success=True, status_code=200, latency_ms=10, timestamp=0, response_json=payload, text="OK", usage=usage, finish_reason="completed")


class FixtureClient:
    def __init__(self):
        self.calls = []

    def openai_responses(self, body):
        self.calls.append(copy.deepcopy(body))
        return chat_result()

    def count_tokens(self, *args):
        pytest.fail("An unconfigured count-token helper must not become a request")


def test_compilation_freezes_final_bodies_inputs_metadata_and_floor_without_credentials(config):
    config["providers"]["deepseek_official"]["api_key"] = "private-fixture-credential"
    plan = prepare(config, runs=2)
    inputs = profile_step(plan)["inputs"]
    assert len(inputs["variants"]) == 2
    assert all(variant["prepared_request"]["body"]["max_output_tokens"] >= 256 for variant in inputs["variants"])
    assert inputs["variants"][0]["prepared_request"]["metadata"]["transport"] == "openai_responses"
    assert inputs["variants"][0]["input_sample"]["id"]
    assert "private-fixture-credential" not in json.dumps(plan)
    assert plan["limits"]["cleanup_max_requests"] == 0


def test_frozen_execution_never_rebuilds_and_preserves_existing_semantic_verdicts(config, monkeypatch, tmp_path):
    plan = prepare(config)
    monkeypatch.setattr(param_test, "build_request", lambda *args, **kwargs: pytest.fail("Frozen execution must not rebuild"))
    client = FixtureClient()
    results, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    frozen = profile_step(plan)["inputs"]["variants"][0]["prepared_request"]
    identity = plan["ordered_steps"][0]["inputs"]["variants"][0]["prepared_request"]
    assert client.calls == [identity["body"], frozen["body"]]
    assert results[0]["request_body"] == frozen["body"]
    assert results[0]["token_audit"]
    assert results[0]["model_identity_audit"]
    assert report["runs"][0]["request_count"] == 2
    assert report["identity_probe"]["request_body"] == identity["body"]
    assert report["runs"][0]["steps"][PROFILE]["legacy_result"] == results[0]
    assert json.loads((tmp_path / "test_workflow_plan.json").read_text())["plan_digest"] == plan["plan_digest"]
    assert (tmp_path / "test_workflow_report.json").exists()


def test_enabled_token_count_is_a_separate_counted_attempt(config, tmp_path):
    config["providers"]["deepseek_official"]["api_interfaces"]["token_count"] = {
        "path": "/count", "auth": "bearer", "transports": ["openai_responses"]}
    plan = prepare(config)
    class Counter(FixtureClient):
        api_interfaces = {"token_count": {"transports": ["openai_responses"]}}
        def count_tokens(self, transport, model, body):
            self.counted = copy.deepcopy(body)
            return {"tokens": 8, "evidence_level": "official_count", "kind": "provider_count", "covers_full_input": True}
    client = Counter()
    _, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    assert client.counted == client.calls[-1]
    assert report["runs"][0]["request_count"] == 4
    assert [attempt["kind"] for attempt in report["runs"][0]["attempts"]] == ["api", "count_tokens", "api", "count_tokens"]


def test_token_count_config_for_different_transport_does_not_dispatch(config, tmp_path):
    config["providers"]["deepseek_official"]["api_interfaces"]["token_count"] = {"path": "/count", "transports": ["claude_messages"]}
    plan = prepare(config)
    _, report = execute_legacy_parameter_plan(plan, config, FixtureClient(), output_dir=tmp_path)
    assert report["runs"][0]["request_count"] == 2


def test_mutating_client_remains_visible_to_existing_input_audit(config, tmp_path):
    class Mutator(FixtureClient):
        def openai_responses(self, body):
            original = super().openai_responses(body)
            body["input"] = "unauthorized altered input"
            return original
    plan = prepare(config)
    results, report = execute_legacy_parameter_plan(plan, config, Mutator(), output_dir=tmp_path)
    assert results[0]["token_validation_pass"] is False
    assert "request" in json.dumps(results[0]["token_audit"]).lower()
    assert results[0]["request_body"] == profile_step(plan)["inputs"]["variants"][0]["prepared_request"]["body"]
    assert report["status"] == "failed"


def test_frozen_job_plan_is_reused_and_missing_plan_is_rejected(config, monkeypatch, tmp_path):
    plan = prepare(config, runs=2)
    config["_parameter_execution_controls"] = make_workflow_execution(plan)
    monkeypatch.setattr(param_test, "_sample_inputs_for_profile", lambda *args, **kwargs: pytest.fail("Do not resample frozen job"))
    with pytest.raises(PlanValidationError, match="original execution plan"):
        param_test.run_param_tests(config, FixtureClient(), *ARGS, 2)
    results = param_test.run_param_tests(config, FixtureClient(), *ARGS, 2, tmp_path, execution_plan=plan)
    assert len(results) == 2
    assert [result["run_index"] for result in results] == [1, 2]
    wrong = copy.deepcopy(config)
    wrong["api"]["timeout_sec"] += 1
    with pytest.raises(IntegrityError, match="configuration"):
        # Entry preflight should fail before credentials or business execution.
        execute_legacy_parameter_plan(plan, wrong, FixtureClient(), output_dir=tmp_path / "wrong")


class FixtureSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []
    def request(self, method, url, **kwargs):
        assert self.trust_env is False
        assert all(adapter.max_retries.total == 0 for adapter in self.adapters.values())
        assert kwargs["allow_redirects"] is False
        assert 0 < kwargs["timeout"] <= 60
        self.calls.append((method, url, copy.deepcopy(kwargs.get("json"))))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(chat_result().response_json).encode()
        response.headers["Content-Type"] = "application/json"
        return response


def real_fixture_client(config, client_type=OpenAICompatibleClient):
    provider = get_provider_config(config, "deepseek_official")
    client = client_type(provider["base_url"], "offline-fixture-key", provider="deepseek_official", api_interfaces=_normalized_interfaces(provider))
    client.session = FixtureSession()
    return client


def test_production_client_session_is_bounded_and_restored(config, tmp_path):
    config["api"]["timeout_sec"] = 30
    plan = prepare(config)
    client = real_fixture_client(config)
    session, timeout = client.session, client.timeout_sec
    _, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    assert len(session.calls) == 2
    assert client.session is session and client.timeout_sec == timeout
    assert session.trust_env is True
    assert report["runs"][0]["request_count"] == 2


def test_runtime_client_route_drift_is_rejected_before_actual_send(config, tmp_path):
    plan = prepare(config)
    client = real_fixture_client(config)
    client.base_url = "https://unexpected.invalid"
    _, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    assert client.session.calls == []
    assert report["status"] == "failed"
    assert "routes differ" in json.dumps(report)


def test_hidden_extra_send_is_prevented_inside_one_legacy_operation(config, tmp_path):
    class DoubleSend(OpenAICompatibleClient):
        def openai_responses(self, body):
            self.session.post(self._transport_url("openai_responses"), json=body)
            self.session.post(self._transport_url("openai_responses"), json=body)
            return chat_result()
    config["api"]["timeout_sec"] = 30
    plan = prepare(config)
    client = real_fixture_client(config, DoubleSend)
    _, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    assert len(client.session.calls) == 1
    assert report["status"] == "failed"
    assert "uncounted additional" in json.dumps(report)


def test_actual_tool_followup_preserves_tool_id_and_uses_two_counted_sends(config, monkeypatch, tmp_path):
    sample = {"id": "weather_shanghai", "prompt": "请查询上海天气。"}
    monkeypatch.setattr(param_test, "_sample_inputs_for_profile", lambda *args, **kwargs: [copy.deepcopy(sample)])
    model = "deepseek-v4-flash"
    class ToolClient:
        def __init__(self):
            self.bodies = []
        def chat_completion(self, body):
            self.bodies.append(copy.deepcopy(body))
            if len(self.bodies) == 1:
                response = {"model": model, "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
                return ChatResult(success=True, status_code=200, latency_ms=10, timestamp=0, response_json=response,
                                  usage={"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}, raw_text=json.dumps(response))
            first = len(self.bodies) == 2
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_weather", "type": "function", "function": {"name": "get_weather", "arguments": json.dumps({"city": "Shanghai"})}}]} if first else {"role": "assistant", "content": "上海当前天气晴朗。"}
            response = {"model": model, "choices": [{"message": message, "finish_reason": "tool_calls" if first else "stop"}]}
            return ChatResult(success=True, status_code=200, latency_ms=100, timestamp=0, response_json=response,
                              usage={"prompt_tokens": 100 if first else 140, "completion_tokens": 24 if first else 10, "total_tokens": 124 if first else 150}, raw_text=json.dumps(response, ensure_ascii=False))
    plan, _ = prepare_legacy_parameter_plan(config, "yibu", model, "deepseek", "deepseek_chat", "deepseek", profiles=["tool_calls"], capability_profile={"parameter_test_enabled": True})
    client = ToolClient()
    results, report = execute_legacy_parameter_plan(plan, config, client, output_dir=tmp_path)
    assert len(client.bodies) == 3
    assert report["runs"][0]["request_count"] == 3
    assert client.bodies[0]["max_tokens"] >= 256
    assert all(body["max_tokens"] >= 1024 for body in client.bodies[1:])
    assert client.bodies[2]["messages"][-1]["tool_call_id"] == "call_weather"
    assert client.bodies[2]["messages"][0] == client.bodies[1]["messages"][0]
    assert results[0]["overall_pass"] is True
    assert len(results[0]["token_audit"]["exchanges"]) == 2


@pytest.mark.parametrize("hard_failure", [None, "http", "token", "identity", "semantic"])
def test_kimi_any_uses_existing_reviewed_rule_and_keeps_raw_failed_experiment(config, monkeypatch, tmp_path, hard_failure):
    from tests.test_run_success_mode import _run
    profile = "kimi_k3_preserved_thinking"
    config.setdefault("compatibility_profiles", {}).setdefault(profile, {})["run_success_mode"] = "any"
    class KimiIdentity:
        def chat_completion(self, body):
            response = {"model": "kimi-k3", "choices": [{"message": {"content": "OK", "reasoning_content": "I will reply OK."}, "finish_reason": "stop"}]}
            return ChatResult(success=True, status_code=200, latency_ms=10, timestamp=0, response_json=response,
                              usage={"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16})
    def profile_result(*args, **kwargs):
        index = args[8]
        row = _run(profile, "incompatible" if index == 1 else "pass", run_index=index)
        if index == 1:
            if hard_failure == "http": row["status_code"] = 503
            elif hard_failure == "token": row["token_validation_pass"] = False
            elif hard_failure == "identity": row["model_identity_audit"] = {"status": "mismatch"}
            elif hard_failure == "semantic": row["failure_classification"] = "json_parse"
        return row
    monkeypatch.setattr(param_test, "run_one_profile", profile_result)
    plan, _ = prepare_legacy_parameter_plan(config, "kimi_official", "kimi-k3", "kimi", "kimi_k3_openai_compat", "kimi",
        profiles=[profile], runs=2, capability_profile={"parameter_test_enabled": True})
    results, report = execute_legacy_parameter_plan(plan, config, KimiIdentity(), output_dir=tmp_path)
    assert report["runs"][0]["steps"][profile]["legacy_result"]["status"] == "incompatible"
    assert report["runs"][0]["status"] == "failed"
    assert len(report["identity_probes"]) == 2
    if hard_failure is None:
        assert report["status"] == "passed"
        assert report["case_outcomes"][profile]["covered_failed_runs"] == [1]
        assert results[0]["satisfied_by_sibling_run"] is True
    else:
        assert report["status"] == "failed"
        assert report["case_outcomes"][profile]["covered_failed_runs"] == []
    job = freeze_workflow_job({"type": "param_test", **plan["target"], "reference_contract_id": plan["target"]["contract_id"]}, plan)
    assert workflow_execution_result_state(job, report) == ("frozen", [])


def test_v6_legacy_main_uses_frozen_identity_and_samples_without_unaccounted_probe(config, monkeypatch, tmp_path):
    from lib.test_runner.service import preview_test_plan
    monkeypatch.setattr(param_test, "print", lambda *a, **kw: None, raising=False)
    monkeypatch.setenv("LOADTEST_PROVIDER", "yibu")
    monkeypatch.setenv("LOADTEST_MODEL", "deepseek-v4-flash")
    config = load_config()
    monkeypatch.setattr(param_test, "_sample_inputs_for_profile", lambda config, profile, runs, rng: [{"id": f"sample-{i}", "prompt": "Reply with OK."} for i in range(runs)])
    preview = preview_test_plan(config, {
        "type": "param_test", "provider": ARGS[0], "model": ARGS[1],
        "api_form": "openai_responses", "route_profile": "vendor_direct",
        "reference_contract_id": SOURCE, "cases": [PROFILE], "runs": 2,
        "plan_seed": "fixed-cli-regression-seed",
    })
    plan = preview["plan"]
    assert profile_step(plan)["inputs"]["variants"][0]["prepared_request"]["metadata"]["provider"] == ARGS[0]
    target = plan["target"]
    job = preview["job_spec"]
    monkeypatch.setattr(param_test, "load_config", load_config)
    monkeypatch.setattr(param_test, "load_job_spec", lambda _path: copy.deepcopy(job))
    # Source preflight replays the same seed to detect drift. Once it reaches
    # the client boundary, execution must use the already frozen variants.
    client = FixtureClient()
    def client_after_validation(*args, **kwargs):
        monkeypatch.setattr(param_test, "_sample_inputs_for_profile", lambda *a, **kw: pytest.fail("Execution resampled v6 inputs"))
        monkeypatch.setattr(param_test, "build_request", lambda *a, **kw: pytest.fail("Execution rebuilt v6 bodies"))
        return client
    monkeypatch.setattr(param_test.DeepSeekClient, "from_config", client_after_validation)
    for key, value in {"LOADTEST_PROVIDER": target["provider"], "LOADTEST_MODEL": target["model"], "LOADTEST_ROUTE_PROFILE": target["route_profile"],
                       "LOADTEST_API_FORM": target["api_form"], "LOADTEST_REFERENCE_CONTRACT_ID": SOURCE,
                       "LOADTEST_PARAM_TEST_RUNS": "2", "LOADTEST_REPORT_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    assert param_test.main() == 0
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    assert len(client.calls) == 4
    assert client.calls == [
        step["inputs"]["variants"][index]["prepared_request"]["body"]
        for index in range(2) for step in plan["ordered_steps"]
    ]
    assert [run["request_count"] for run in verdict["workflow_result"]["runs"]] == [2, 2]
    assert len(verdict["identity_probes"]) == 2
    assert verdict["workflow_result"]["plan_digest"] == plan["plan_digest"]
    assert workflow_execution_result_state(job, verdict) == ("frozen", [])


def test_functional_provider_selection_is_local_and_cannot_change_pressure_defaults(config, monkeypatch):
    from lib.deepseek_params import build_request
    monkeypatch.setenv("LOADTEST_PROVIDER", "yibu")
    monkeypatch.setenv("LOADTEST_MODEL", "deepseek-v4-flash")
    original = copy.deepcopy(config)
    plan = prepare(config)
    metadata = profile_step(plan)["inputs"]["variants"][0]["prepared_request"]["metadata"]
    assert metadata["provider"] == "deepseek_official"
    assert config == original
    assert get_active_provider_name(config) == "yibu"
    assert get_selected_model(config) == "deepseek-v4-flash"
    with pytest.raises(ValueError, match="restricted to functional"):
        build_request(config, "throughput_profiles", "throughput_balanced", provider_override="deepseek_official")


def test_yaml_cannot_reintroduce_a_stale_functional_target(config, monkeypatch, tmp_path):
    import yaml
    copied = copy.deepcopy(config)
    copied["_parameter_test_exact_input"] = True
    copied["_functional_parameter_target"] = {"provider": "deepseek_official", "model": "deepseek-v4-pro"}
    file = tmp_path / "config.yaml"
    file.write_text(yaml.safe_dump(copied))
    monkeypatch.setenv("LOADTEST_PROVIDER", "yibu")
    monkeypatch.setenv("LOADTEST_MODEL", "deepseek-v4-flash")
    loaded = load_config(file)
    assert "_functional_parameter_target" not in loaded
    assert "_parameter_test_exact_input" not in loaded
    assert get_active_provider_name(loaded) == "yibu"
    assert get_selected_model(loaded) == "deepseek-v4-flash"


def test_target_authentication_configuration_is_not_ignored_by_digest(config, tmp_path):
    plan = prepare(config)
    changed = copy.deepcopy(config)
    changed["providers"]["deepseek_official"]["api_interfaces"]["openai_responses"]["auth"] = "none"
    with pytest.raises(IntegrityError, match="configuration"):
        execute_legacy_parameter_plan(plan, changed, FixtureClient(), output_dir=tmp_path)


def test_explicit_profile_selection_overrides_ambient_cli_subset_without_mutating_environment(config, monkeypatch):
    monkeypatch.setenv("LOADTEST_PARAM_PROFILES", "deepseek0813_responses_instructions")
    plan = prepare(config)
    assert plan["requested_cases"] == ["identity_probe", PROFILE]
    import os
    assert os.environ["LOADTEST_PARAM_PROFILES"] == "deepseek0813_responses_instructions"


def test_reviewed_sampling_seed_reproduces_the_exact_plan(config):
    first = prepare(config, runs=3)
    seed = first["definition"]["factory_arguments"]["sampling_seed"]
    assert prepare(config, runs=3, sampling_seed=seed) == first
    for invalid in (True, "", "x" * 129, "line\nbreak"):
        with pytest.raises(PlanValidationError, match="sampling_seed"):
            prepare(config, sampling_seed=invalid)


def test_explicit_route_is_frozen_and_overrides_unrelated_ambient_route(config, monkeypatch, tmp_path):
    monkeypatch.setenv("LOADTEST_ROUTE_PROFILE", "google_ai_studio")
    before = copy.deepcopy(config)
    plan = prepare(config, route_profile="vendor_direct")
    assert plan["target"]["route_profile"] == "vendor_direct"
    assert plan["definition"]["factory_arguments"]["route_profile"] == "vendor_direct"
    rows, report = execute_legacy_parameter_plan(plan, config, FixtureClient(), output_dir=tmp_path)
    assert rows[0]["route_profile"] == "vendor_direct"
    assert report["identity_probe"]["route_profile"] == "vendor_direct"
    assert config == before
    assert "/workflow_runs/" in report["runs"][0]["ledger_path"]
