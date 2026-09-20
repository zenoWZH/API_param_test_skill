from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest

from lib.config import get_model_api_form, get_model_route_profile, load_config
from lib.deepseek_params import build_request, load_pressure_capability, weighted_workload_profiles
from lib.model_profile_catalog import resolve_runtime_parameter_config
from lib.reference_specs import load_model_capability_profile
from scripts.run_staircase import run_locust


@pytest.fixture
def frozen_config():
    config = load_config()
    config["active_provider"] = "yibu"
    config["providers"]["yibu"]["models"]["default"] = "deepseek-v4-flash"
    route = get_model_route_profile(config, "deepseek-v4-flash", "yibu")
    form = get_model_api_form(config, "deepseek-v4-flash", "yibu", route_profile=route)
    snapshot = resolve_runtime_parameter_config(
        config, "yibu", "deepseek-v4-flash", "deepseek", route, form,
    )["model_profile_database"]
    config["_model_profile_database"] = snapshot
    config["_model_capability_profile"] = load_pressure_capability(
        config, "yibu", "deepseek", "deepseek-v4-flash", form, route,
        snapshot["reference_contract_id"],
    )
    return config


def _rehash(snapshot):
    snapshot.pop("snapshot_digest", None)
    snapshot["snapshot_digest"] = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def test_provider_override_cannot_enable_disabled_mpdb_pressure():
    capability = load_model_capability_profile(
        "text", "deepseek", "deepseek-v4-pro", api_form="openai_chat_completions",
        route_profile="dynamic_aggregator", provider_override={"pressure_test_enabled": True},
    )
    assert capability["pressure_test_enabled"] is False


@pytest.mark.parametrize("field,value", [
    ("model", "deepseek-v4-pro"),
    ("api_form", "anthropic_messages"),
    ("route_profile", "vendor_direct"),
    ("reference_contract_id", "deepseek_anthropic"),
])
def test_cached_capability_cannot_cross_selection(frozen_config, field, value):
    frozen_config["_model_capability_profile"][field] = value
    with pytest.raises(ValueError, match="snapshot conflicts"):
        weighted_workload_profiles(frozen_config, "throughput_rpm")


@pytest.mark.parametrize("field,value", [
    ("pressure_test_enabled", False), ("enabled", False), ("executable", False),
    ("runner_enabled", False), ("disabled_reason", "paused"),
])
def test_disabled_cached_capability_never_reloads_enabled_catalog(frozen_config, field, value):
    frozen_config["_model_capability_profile"][field] = value
    with patch("lib.reference_specs.load_model_capability_profile", side_effect=AssertionError("live reload")):
        with pytest.raises(ValueError, match="Pressure testing is disabled"):
            weighted_workload_profiles(frozen_config, "throughput_rpm")
        with pytest.raises(ValueError, match="Pressure testing is disabled"):
            build_request(frozen_config, "throughput_profiles", "standard_short")


def test_enabled_cache_cannot_override_disabled_frozen_policy(frozen_config):
    snapshot = frozen_config["_model_profile_database"]
    snapshot["test_binding"]["pressure_test_enabled"] = False
    _rehash(snapshot)
    frozen_config["_model_capability_profile"]["model_profile_database"] = copy.deepcopy(snapshot)
    with pytest.raises(ValueError, match="disabled by the frozen MPDB policy"):
        weighted_workload_profiles(frozen_config, "throughput_rpm")


def test_frozen_policy_and_profiles_are_used_without_catalog_reload(frozen_config):
    snapshot = frozen_config["_model_profile_database"]
    snapshot["test_binding"]["pressure_overrides"] = {"temperature": 0.123}
    snapshot["test_binding"]["pressure_profiles"] = {snapshot["reference_contract_id"]: ["basic_stream"]}
    _rehash(snapshot)
    frozen_config["_model_capability_profile"]["model_profile_database"] = copy.deepcopy(snapshot)
    # The loose capability projection must not replace the immutable policy.
    frozen_config["_model_capability_profile"]["pressure_overrides"] = {"temperature": 0.9}
    frozen_config["profile_weights"]["mixed_compat"] = {"basic_stream": 3, "tool_calls": 2}
    with patch("lib.reference_specs.load_model_capability_profile", side_effect=AssertionError("live reload")):
        request = build_request(frozen_config, "throughput_profiles", "standard_short")
        assert request.body["temperature"] == 0.123
        assert weighted_workload_profiles(frozen_config, "mixed_compat") == [
            ("compatibility_profiles", "basic_stream", 3),
        ]


def test_provider_request_alias_uses_reference_model_for_pressure(frozen_config):
    config = frozen_config
    config.pop("_model_profile_database")
    config.pop("_model_capability_profile")
    models = config["providers"]["yibu"]["models"]
    models["default"] = "pressure-flash-alias"
    models.setdefault("families", {})["pressure-flash-alias"] = "deepseek"
    models.setdefault("reference_model_ids", {})["pressure-flash-alias"] = "deepseek-v4-flash"
    assert weighted_workload_profiles(config, "throughput_rpm")
    request = build_request(config, "throughput_profiles", "standard_short")
    assert request.body["model"] == "pressure-flash-alias"
    assert request.metadata["capability_profile_id"].endswith("deepseek-v4-flash-0731")


@pytest.mark.parametrize("request_mode", ["fixed", "unique"])
def test_staircase_rpm_child_preserves_measurement_mode(frozen_config, tmp_path, request_mode):
    with patch.dict(os.environ, {"LOADTEST_REQUEST_MODE": request_mode, "YIBU_API_KEY": "pressure-test-dummy-key"}), patch(
        "scripts.run_staircase.subprocess.run",
    ) as subprocess_run:
        subprocess_run.return_value.returncode = 0
        run_locust(config=frozen_config, report_dir=tmp_path, users=1, spawn_rate=1,
                   duration="1s", workload="throughput_rpm", phase="measure", staircase_step=1)
    assert subprocess_run.call_args.kwargs["env"]["LOADTEST_REQUEST_MODE"] == request_mode


@pytest.mark.parametrize("job_has_capability", [False, True])
def test_locust_reuses_frozen_job_and_pins_request_api(frozen_config, tmp_path, job_has_capability):
    from test_locust_outcomes import _fake_locust_modules

    snapshot = frozen_config["_model_profile_database"]
    job = {"model_profile_database": snapshot, "reference_contract_id": snapshot["reference_contract_id"]}
    if job_has_capability:
        job["model_capability_profile"] = frozen_config["_model_capability_profile"]
    previous = sys.modules.pop("locustfile", None)
    try:
        with patch.dict(sys.modules, _fake_locust_modules()), patch.dict(os.environ, {
            "LOADTEST_WORKLOAD": "throughput_rpm", "LOADTEST_REQUEST_MODE": "fixed",
            "LOADTEST_REPORT_DIR": str(tmp_path), "LOADTEST_TARGET_RPM": "0",
            "LOADTEST_TARGET_TPM": "0", "LOADTEST_JOB_SPEC": "frozen-job.json",
        }), patch("lib.config.load_config", return_value=frozen_config), patch(
            "lib.job_spec.load_job_spec", return_value=job,
        ), patch("lib.reference_specs.load_model_capability_profile", side_effect=AssertionError("live reload")):
            module = importlib.import_module("locustfile")
            user = object.__new__(module.DeepSeekLoadUser)
            module.CONFIG["throughput_profiles"]["standard_short"]["transport"] = "claude_messages"
            module.TASK_CHOICES = [("throughput_profiles", "standard_short")]
            module.TASK_WEIGHTS = [1]
            with patch.object(module, "_admit_request"), patch.object(
                module, "ADAPTIVE_CONTROLLER", None,
            ), patch.object(module, "build_request", wraps=module.build_request) as build, patch.object(
                module.DeepSeekLoadUser, "_post_chat", return_value=None,
            ) as post:
                user.run_weighted_profile()
            assert post.call_args.kwargs["transport"] == "chat_completions"
            assert post.call_args.args[3]["model"] == "deepseek-v4-flash"
            assert build.call_args.kwargs["api_form_override"] == snapshot["api_form"]
            assert build.call_args.kwargs["route_profile_override"] == snapshot["execution_target"]["route_profile"]
            assert build.call_args.kwargs["reference_source"] == snapshot["reference_contract_id"]
            assert build.call_args.kwargs["pressure_policy_override"]["pressure_test_enabled"] is True
    finally:
        sys.modules.pop("locustfile", None)
        if previous is not None:
            sys.modules["locustfile"] = previous
