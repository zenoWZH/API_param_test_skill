"""Web fixed-cache preview and create freeze one nonce/body experiment."""
import json
from unittest.mock import Mock

import pytest

from lib.job_spec import load_job_spec
from lib.test_runner.adapters import fixed_parameter as fixed
from scripts import web_console as web
from test_workflow_parameter_web import context


def test_fixed_cache_web_preview_and_create_keep_the_same_nonce_before_credentials(context, monkeypatch):
    _, manager = context
    model = fixed.cache.MODELS[0]
    payload = {"type": "param_test", "provider": "anthropic_official", "model": model,
        "api_form": "anthropic_messages", "parameter_suite": fixed.cache.suite_id(model)}
    nonce = Mock(side_effect=["a" * 32, "b" * 32])
    monkeypatch.setattr(fixed.cache.secrets, "token_hex", nonce)
    keys = Mock(side_effect=AssertionError("preview read provider credentials"))
    monkeypatch.setattr(web, "provider_has_api_key", keys)
    response = web.app.test_client().post("/api/test-plan/preview", json=payload)
    assert response.status_code == 200, response.json
    preview = response.json
    assert nonce.call_count == 2 and preview["request_cap"] == 3
    assert preview["job_spec"]["fixed_parameter_plan"] == preview["plan_seed"]
    with pytest.raises(ValueError, match="seed"):
        manager.create({**payload, "plan_digest": preview["plan_digest"]})
    assert nonce.call_count == 2
    keys.assert_not_called()
    monkeypatch.setattr(web, "provider_has_api_key", Mock(return_value=True))
    job = manager.create({**payload, "plan_seed": preview["plan_seed"], "plan_digest": preview["plan_digest"]})
    persisted = load_job_spec(job.report_dir / "job_spec.json")
    assert persisted["schema_version"] == 6
    assert persisted["execution_plan"] == preview["plan"]
    assert persisted["fixed_parameter_plan"] == preview["plan_seed"]
    assert persisted["parameter_suite"] == payload["parameter_suite"] and job.param_test_runs == 1
    assert nonce.call_count == 2
    manager._start_locked.assert_called_once_with(job)


def test_closed_fixed_suite_refuses_repeats_and_subsets_before_credentials(context, monkeypatch):
    _, manager = context
    monkeypatch.setattr(web, "provider_has_api_key", Mock(side_effect=AssertionError("invalid scope read credentials")))
    payload = {"type": "param_test", "provider": "deepseek_official", "model": "deepseek-v4-pro",
               "api_form": "openai_fim_completions_beta", "parameter_suite": fixed.fim.SUITE_ID}
    for extra in ({"param_test_runs": 2}, {"cases": [fixed.fim.CASE_IDS[0]]}):
        with pytest.raises(ValueError):
            manager.create({**payload, **extra})
    web.provider_has_api_key.assert_not_called()
