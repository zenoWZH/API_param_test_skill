from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lib.config import load_config
from lib.metrics import RequestRecord
from scripts import run_model_sweep as sweep


MODEL = "claude-opus-5"
PROVIDER = "test_anthropic"


def _config() -> dict[str, Any]:
    config = copy.deepcopy(load_config())
    config["active_provider"] = PROVIDER
    config["providers"][PROVIDER] = {
        "label": "Test Anthropic native Messages",
        "base_url": "https://example.test/v1",
        "backend": "anthropic",
        "default_transport": "claude_messages",
        "api_interfaces": {
            "claude_messages": {"path": "/messages", "auth": "anthropic"}
        },
        "models": {
            "default": MODEL,
            "candidates": [MODEL],
            "families": {MODEL: "claude"},
            "transports": {MODEL: "claude_messages"},
            "routes": {
                MODEL: {
                    "vendor_direct": {
                        "api_forms": {
                            "anthropic_messages": {
                                "reference_source": "claude_native_messages"
                            }
                        }
                    }
                }
            },
            "default_routes": {MODEL: "vendor_direct"},
            "default_api_forms": {
                MODEL: {"vendor_direct": "anthropic_messages"}
            },
            "identity_aliases": {},
        },
    }
    return config


def _aliyun_deepseek_config() -> dict[str, Any]:
    config = copy.deepcopy(load_config())
    config["active_provider"] = "yibu"
    provider = config["providers"]["yibu"]
    provider["reference_source_id"] = "aliyun_maas"
    provider["models"]["default"] = "deepseek-v4-pro"
    return config


def _result(
    *,
    returned_model: str = MODEL,
    finish_reason: str = "end_turn",
) -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        status_code=200,
        latency_ms=1.0,
        finish_reason=finish_reason,
        error_type=None,
        failure_classification=None,
        response_json={
            "model": returned_model,
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": finish_reason,
        },
        raw_text="{}",
        headers={},
    )


class _ClaudeClient:
    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result
        self.bodies: list[dict[str, Any]] = []

    def claude_messages(self, body: dict[str, Any]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        return self.result

    def chat_completion(self, body: dict[str, Any]) -> SimpleNamespace:
        raise AssertionError(body)

    def gemini_interactions(self, body: dict[str, Any]) -> SimpleNamespace:
        raise AssertionError(body)

    def openai_responses(self, body: dict[str, Any]) -> SimpleNamespace:
        raise AssertionError(body)

    def fim_completion(self, body: dict[str, Any]) -> SimpleNamespace:
        raise AssertionError(body)


class _ChatClient:
    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result
        self.bodies: list[dict[str, Any]] = []

    def chat_completion(self, body: dict[str, Any]) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        return self.result


def test_preflight_ignores_ambient_route_selection_and_uses_safe_opus_body(
    monkeypatch: Any,
) -> None:
    client = _ClaudeClient(_result())
    monkeypatch.setattr(
        sweep.OpenAICompatibleClient,
        "from_config",
        staticmethod(lambda config, provider: client),
    )
    monkeypatch.setenv("LOADTEST_PROVIDER", "yibu")
    monkeypatch.setenv("LOADTEST_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("LOADTEST_ROUTE_PROFILE", "dynamic_aggregator")
    monkeypatch.setenv("LOADTEST_API_FORM", "openai_chat_completions")

    result = sweep._preflight(_config(), PROVIDER, MODEL)

    assert result["success"] is True, result
    assert result["model_identity_audit"]["status"] == "match"
    assert result["transport"] == "claude_messages"
    assert len(client.bodies) == 1
    body = client.bodies[0]
    assert body == {
        "model": MODEL,
        "max_tokens": 128,
        "messages": [
            {
                "role": "user",
                "content": _config()["prompts"]["pressure_standard_short"],
            }
        ],
        "system": "你是一名简洁的文字助手，请严格按照用户要求作答。",
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    assert os.environ["LOADTEST_MODEL"] == "claude-haiku-4-5"
    assert os.environ["LOADTEST_PROVIDER"] == "yibu"
    assert os.environ["LOADTEST_ROUTE_PROFILE"] == "dynamic_aggregator"
    assert os.environ["LOADTEST_API_FORM"] == "openai_chat_completions"


def test_preflight_rejects_refusal_and_identity_mismatch(monkeypatch: Any) -> None:
    refusal_client = _ClaudeClient(_result(finish_reason="refusal"))
    monkeypatch.setattr(
        sweep.OpenAICompatibleClient,
        "from_config",
        staticmethod(lambda config, provider: refusal_client),
    )
    refusal = sweep._preflight(_config(), PROVIDER, MODEL)
    assert refusal["success"] is False
    assert refusal["failure"] == "finish_reason:refusal"
    assert refusal["model_identity_audit"]["status"] == "match"

    mismatch_client = _ClaudeClient(
        _result(returned_model="claude-opus-4-8")
    )
    monkeypatch.setattr(
        sweep.OpenAICompatibleClient,
        "from_config",
        staticmethod(lambda config, provider: mismatch_client),
    )
    mismatch = sweep._preflight(_config(), PROVIDER, MODEL)
    assert mismatch["success"] is False
    assert mismatch["failure"] == "model_identity:mismatch"
    assert mismatch["model_identity_audit"]["status"] == "mismatch"


def test_preflight_uses_the_provider_source_contract(monkeypatch: Any) -> None:
    model = "deepseek-v4-flash"
    response = _result(returned_model=model, finish_reason="stop")
    response.response_json = {
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    client = _ChatClient(response)
    monkeypatch.setattr(
        sweep.OpenAICompatibleClient,
        "from_config",
        staticmethod(lambda config, provider: client),
    )

    result = sweep._preflight(_aliyun_deepseek_config(), "yibu", model)

    assert result["success"] is True, result
    assert result["transport"] == "chat_completions"
    assert len(client.bodies) == 1
    assert client.bodies[0]["model"] == model


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("LOADTEST_TARGET_RPM", "-100"),
        ("LOADTEST_TARGET_RPM", "nan"),
        ("LOADTEST_SWEEP_DURATION", "0s"),
        ("LOADTEST_SWEEP_USERS", "-1"),
        ("LOADTEST_SWEEP_SPAWN_RATE", "0"),
        ("LOADTEST_SWEEP_SPAWN_RATE", "1"),
        ("LOADTEST_SWEEP_WORKLOAD", "not_a_workload"),
    ],
)
def test_invalid_sweep_plan_is_rejected_before_model_preparation(
    monkeypatch: Any,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    report_dir = tmp_path / "must-not-exist"
    environment = {
        "LOADTEST_PROVIDER": "yibu",
        "LOADTEST_MODELS": "deepseek-v4-flash",
        "LOADTEST_TARGET_RPM": "100",
        "LOADTEST_SWEEP_DURATION": "60s",
        "LOADTEST_SWEEP_USERS": "10",
        "LOADTEST_SWEEP_SPAWN_RATE": "2",
        "LOADTEST_SWEEP_WORKLOAD": "throughput_rpm",
        "LOADTEST_REPORT_DIR": str(report_dir),
    }
    environment[field] = value
    for name, selected in environment.items():
        monkeypatch.setenv(name, selected)

    def unexpected_prepare(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid input reached model preparation")

    monkeypatch.setattr(sweep, "_prepare_model_execution", unexpected_prepare)

    assert sweep.main() == 2
    assert not report_dir.exists()


def test_existing_report_directory_is_rejected_before_preflight(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "existing"
    report_dir.mkdir()
    for name, value in {
        "LOADTEST_PROVIDER": "yibu",
        "LOADTEST_MODELS": "deepseek-v4-flash",
        "LOADTEST_TARGET_RPM": "60",
        "LOADTEST_SWEEP_DURATION": "60s",
        "LOADTEST_SWEEP_USERS": "2",
        "LOADTEST_SWEEP_SPAWN_RATE": "1",
        "LOADTEST_SWEEP_WORKLOAD": "throughput_rpm",
        "LOADTEST_REPORT_DIR": str(report_dir),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sweep,
        "_prepare_model_execution",
        lambda *args, **kwargs: {"request": object(), "config": {}},
    )
    monkeypatch.setattr(
        sweep,
        "_preflight",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("existing output reached preflight")
        ),
    )

    assert sweep.main() == 2


def test_nonzero_locust_exit_can_never_pass_old_summary() -> None:
    summary = {
        "business_record_count": 100,
        "business_rpm": 100,
        "success_rate": 1,
        "error_429_ratio": 0,
        "error_5xx_ratio": 0,
        "model_identity_audit": {"status": "match"},
        "profile_mix_audit": {"pass": True},
    }

    assert sweep._run_passed(0, summary, 100) is True
    assert sweep._run_passed(1, summary, 100) is False


def test_report_slug_collision_is_rejected_before_model_preparation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    for name, value in {
        "LOADTEST_PROVIDER": "yibu",
        "LOADTEST_MODELS": "model/a,model_a",
        "LOADTEST_TARGET_RPM": "60",
        "LOADTEST_SWEEP_DURATION": "60s",
        "LOADTEST_SWEEP_USERS": "2",
        "LOADTEST_SWEEP_SPAWN_RATE": "1",
        "LOADTEST_SWEEP_WORKLOAD": "throughput_rpm",
        "LOADTEST_REPORT_DIR": str(tmp_path / "must-not-exist"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sweep,
        "_prepare_model_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("slug collision reached model preparation")
        ),
    )

    assert sweep.main() == 2


def test_preflight_rejects_pressure_disabled_bedrock_before_sending(
    monkeypatch: Any,
) -> None:
    config = _config()
    provider = config["providers"][PROVIDER]
    provider["backend"] = "proxy_unknown"
    models = provider["models"]
    models["routes"][MODEL] = {
        "aws_bedrock": {
            "api_forms": {
                "anthropic_messages": {
                    "reference_source": "claude_aws_bedrock_messages"
                }
            }
        }
    }
    models["default_routes"][MODEL] = "aws_bedrock"
    models["default_api_forms"][MODEL] = {"aws_bedrock": "anthropic_messages"}
    client = _ClaudeClient(_result())
    monkeypatch.setattr(
        sweep.OpenAICompatibleClient,
        "from_config",
        staticmethod(lambda config, provider: client),
    )

    # The registered Bedrock Interface remains disabled for pressure traffic.
    with pytest.raises(ValueError, match=r"Pressure testing is disabled|No approved official MPDB binding"):
        sweep._preflight(config, PROVIDER, MODEL)
    assert client.bodies == []


def test_gemini_native_preflight_dispatch_passes_model_separately() -> None:
    expected = object()

    class GeminiClient:
        def gemini_generate_content(
            self, model: str, body: dict[str, Any]
        ) -> object:
            assert model == "gemini-test"
            assert body == {"contents": []}
            return expected

    assert (
        sweep._dispatch_preflight(
            GeminiClient(),  # type: ignore[arg-type]
            "gemini_generate_content",
            "gemini-test",
            {"contents": []},
        )
        is expected
    )


def test_preflight_dispatch_only_requires_the_selected_client_method() -> None:
    expected = object()

    class ClaudeOnlyClient:
        def claude_messages(self, body: dict[str, Any]) -> object:
            assert body == {"model": MODEL}
            return expected

    assert (
        sweep._dispatch_preflight(
            ClaudeOnlyClient(),  # type: ignore[arg-type]
            "claude_messages",
            MODEL,
            {"model": MODEL},
        )
        is expected
    )


def test_run_locust_pins_contract_and_clears_ambient_sweep_state(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    inherited = {
        "LOADTEST_JOB_SPEC": "/tmp/stale-job.json",
        "LOADTEST_HTTP_EXTRA_HEADERS": json.dumps({"x-stale": "1"}),
        "LOADTEST_TARGET_TPM": "999999",
        "LOADTEST_TARGET_TOKENS_PER_REQUEST": "9999",
        "LOADTEST_MODEL": "claude-haiku-4-5",
        "LOADTEST_ROUTE_PROFILE": "dynamic_aggregator",
        "LOADTEST_API_FORM": "openai_chat_completions",
    }
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> SimpleNamespace:
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        sweep,
        "build_provider_child_env",
        lambda config, provider: dict(inherited),
    )
    monkeypatch.setattr(sweep.subprocess, "run", fake_run)
    monkeypatch.setenv("LOADTEST_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("LOADTEST_ROUTE_PROFILE", "dynamic_aggregator")
    monkeypatch.setenv("LOADTEST_API_FORM", "openai_chat_completions")

    prepared = sweep._prepare_model_execution(
        _config(), PROVIDER, MODEL, "throughput_rpm"
    )
    job_spec_path = tmp_path / "job_spec.json"
    job_spec_path.write_text(
        json.dumps(
            sweep._sweep_job_spec(
                prepared,
                provider=PROVIDER,
                model=MODEL,
                workload="throughput_rpm",
                target_rpm=100,
            )
        ),
        encoding="utf-8",
    )
    persisted_job = json.loads(job_spec_path.read_text(encoding="utf-8"))
    expected_digest = prepared["model_profile_database"]["snapshot_digest"]
    assert persisted_job["model_profile_database"]["snapshot_digest"] == expected_digest
    assert (
        persisted_job["model_capability_profile"]["model_profile_database"][
            "snapshot_digest"
        ]
        == expected_digest
    )
    assert (
        persisted_job["reference_contract_id"]
        == prepared["reference_contract_id"]
    )

    return_code = sweep._run_locust(
        config=_config(),
        provider=PROVIDER,
        model=MODEL,
        report_dir=tmp_path,
        users=10,
        spawn_rate=10,
        duration="60s",
        workload="throughput_rpm",
        target_rpm=100,
        prepared=prepared,
        job_spec_path=job_spec_path,
    )

    assert return_code == 0
    env = captured["env"]
    assert env["LOADTEST_MODEL"] == MODEL
    assert env["LOADTEST_ROUTE_PROFILE"] == "vendor_direct"
    assert env["LOADTEST_API_FORM"] == "anthropic_messages"
    assert env["LOADTEST_TARGET_TPM"] == "0"
    assert env["LOADTEST_TARGET_TOKENS_PER_REQUEST"] == "0"
    assert env["LOADTEST_REQUEST_MODE"] == "fixed"
    assert env["LOADTEST_WARMUP_SEC"] == "6.0"
    assert env["LOADTEST_MEASURE_DURATION_SEC"] == "60.0"
    assert env["LOADTEST_JOB_SPEC"] == str(job_spec_path)
    assert env["LOADTEST_EXPECTED_MPDB_SNAPSHOT_DIGEST"] == expected_digest
    assert (
        env["LOADTEST_EXPECTED_REFERENCE_CONTRACT_ID"]
        == prepared["reference_contract_id"]
    )
    assert "LOADTEST_HTTP_EXTRA_HEADERS" not in env
    command = captured["command"]
    assert command[command.index("-t") + 1] == "66s"


def test_run_locust_rejects_a_self_consistent_job_spec_from_another_source(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    model = "deepseek-v4-flash"
    default_config = copy.deepcopy(load_config())
    default_config["active_provider"] = "yibu"
    default_config["providers"]["yibu"]["models"]["default"] = model
    prepared = sweep._prepare_model_execution(
        default_config, "yibu", model, "throughput_rpm"
    )
    other_prepared = sweep._prepare_model_execution(
        _aliyun_deepseek_config(), "yibu", model, "throughput_rpm"
    )
    self_consistent_other = sweep._sweep_job_spec(
        other_prepared,
        provider="yibu",
        model=model,
        workload="throughput_rpm",
        target_rpm=60,
    )
    job_spec_path = tmp_path / "job_spec.json"
    job_spec_path.write_text(
        json.dumps(self_consistent_other), encoding="utf-8"
    )
    default_target = prepared["model_profile_database"]["execution_target"]
    other_target = other_prepared["model_profile_database"]["execution_target"]
    assert default_target == other_target
    assert prepared["reference_contract_id"] != other_prepared[
        "reference_contract_id"
    ]
    monkeypatch.setattr(
        sweep.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting snapshot reached Locust")
        ),
    )

    with pytest.raises(
        ValueError,
        match="sweep job spec conflicts with the prepared MPDB snapshot",
    ):
        sweep._run_locust(
            config=default_config,
            provider="yibu",
            model=model,
            report_dir=tmp_path,
            users=2,
            spawn_rate=1,
            duration="60s",
            workload="throughput_rpm",
            target_rpm=60,
            prepared=prepared,
            job_spec_path=job_spec_path,
        )


def test_recorded_identity_is_a_formal_pass_gate(tmp_path: Path) -> None:
    path = tmp_path / "request_records.jsonl"
    rows = [
        RequestRecord(
            timestamp=100 + index,
            task_name="chat:throughput_profiles:standard_short",
            group="throughput_profiles",
            profile="standard_short",
            method="POST",
            path="/v1/messages",
            success=True,
            status_code=200,
            extra={
                "response_model": MODEL,
                "measure_started_at": 100,
                "measure_ended_at": 200,
            },
        )
        for index in range(2)
    ]
    path.write_text("\n".join(item.to_json() for item in rows) + "\n", "utf-8")

    audit = sweep._recorded_model_identity_audit(
        path,
        _config()["providers"][PROVIDER],
        MODEL,
    )
    summary = {
        "business_record_count": 100,
        "business_rpm": 100,
        "success_rate": 1,
        "error_429_ratio": 0,
        "error_5xx_ratio": 0,
        "model_identity_audit": audit,
        "profile_mix_audit": {"pass": True},
    }
    assert audit["status"] == "match"
    assert audit["returned_model_counts"] == {MODEL: 2}
    assert sweep._summary_pass(summary, 100) is True

    rows[1].extra["response_model"] = "claude-opus-4-8"
    path.write_text("\n".join(item.to_json() for item in rows) + "\n", "utf-8")
    mismatch = sweep._recorded_model_identity_audit(
        path,
        _config()["providers"][PROVIDER],
        MODEL,
    )
    summary["model_identity_audit"] = mismatch
    assert mismatch["status"] == "mismatch"
    assert sweep._summary_pass(summary, 100) is False


def test_recorded_profile_mix_reports_per_profile_results_and_gates_pass(
    tmp_path: Path,
) -> None:
    path = tmp_path / "request_records.jsonl"
    window = {"measure_started_at": 100, "measure_ended_at": 200}
    rows = [
        RequestRecord(
            timestamp=101,
            task_name="chat:throughput_profiles:standard_short",
            group="throughput_profiles",
            profile="standard_short",
            method="POST",
            path="/v1/messages",
            success=True,
            status_code=200,
            latency_ms=10,
            finish_reason="end_turn",
            extra=window,
        ),
        RequestRecord(
            timestamp=102,
            task_name="chat:throughput_profiles:standard_short",
            group="throughput_profiles",
            profile="standard_short",
            method="POST",
            path="/v1/messages",
            success=False,
            status_code=200,
            latency_ms=30,
            finish_reason="refusal",
            failure_classification="finish_reason:refusal",
            extra=window,
        ),
        RequestRecord(
            timestamp=103,
            task_name="chat:throughput_profiles:standard_medium",
            group="throughput_profiles",
            profile="standard_medium",
            method="POST",
            path="/v1/messages",
            success=False,
            status_code=502,
            latency_ms=50,
            error_type="upstream_error",
            extra=window,
        ),
        RequestRecord(
            timestamp=104,
            task_name="chat:throughput_profiles:standard_rewrite",
            group="throughput_profiles",
            profile="standard_rewrite",
            method="POST",
            path="/v1/messages",
            success=True,
            status_code=200,
            latency_ms=70,
            finish_reason="end_turn",
            extra=window,
        ),
        RequestRecord(
            timestamp=105,
            task_name="chat:throughput_profiles:standard_extract",
            group="throughput_profiles",
            profile="standard_extract",
            method="POST",
            path="/v1/messages",
            success=True,
            status_code=200,
            latency_ms=90,
            finish_reason="end_turn",
            is_warmup=True,
            extra=window,
        ),
    ]
    path.write_text("\n".join(item.to_json() for item in rows) + "\n", "utf-8")

    audit = sweep._recorded_profile_mix(
        path,
        ["standard_short", "standard_medium", "standard_extract"],
    )

    assert audit["pass"] is False
    assert audit["missing_profiles"] == ["standard_extract"]
    assert audit["unexpected_profiles"] == ["standard_rewrite"]
    assert audit["profile_counts"] == {
        "standard_medium": 1,
        "standard_rewrite": 1,
        "standard_short": 2,
    }
    assert audit["profiles"]["standard_short"] == {
        "request_count": 2,
        "http_request_count": 2,
        "http_2xx_count": 2,
        "http_2xx_rate": 1.0,
        "business_success_count": 1,
        "business_success_rate": 0.5,
        "refusal_count": 1,
        "http_5xx_count": 0,
        "http_502_count": 0,
        "p95_latency_ms": 29.0,
    }
    assert audit["profiles"]["standard_medium"]["http_502_count"] == 1
    assert audit["profiles"]["standard_medium"]["http_5xx_count"] == 1
    assert audit["profiles"]["standard_extract"]["request_count"] == 0

    sampled_audit = sweep._recorded_profile_mix(
        path,
        ["standard_short", "standard_medium", "standard_extract", "standard_rewrite"],
        require_full_coverage=False,
    )
    assert sampled_audit["coverage_required"] is False
    assert sampled_audit["missing_profiles"] == ["standard_extract"]
    assert sampled_audit["pass"] is True

    passing_audit = sweep._recorded_profile_mix(
        path,
        ["standard_short", "standard_medium", "standard_rewrite"],
    )
    assert passing_audit["pass"] is True
    summary = {
        "business_record_count": 100,
        "business_rpm": 100,
        "success_rate": 1,
        "error_429_ratio": 0,
        "error_5xx_ratio": 0,
        "model_identity_audit": {"status": "match"},
        "profile_mix_audit": audit,
    }
    assert sweep._summary_pass(summary, 100) is False
    summary["profile_mix_audit"] = passing_audit
    assert sweep._summary_pass(summary, 100) is True

    summary.pop("profile_mix_audit")
    assert sweep._summary_pass(summary, 100) is False
