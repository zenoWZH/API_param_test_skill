"""Exact xAI Chat cap semantics, including current-consumer retained replay."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lib.token_audit import audit_exchange

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = PROJECT.parent if PROJECT.name == "app" else PROJECT
BATCH = EVIDENCE_ROOT / "reports/approved_live_20260907/output_boundary_20260908T215455Z_3bb7b38e"
CONFIG = {"test_cases": {"token_accuracy": {"enabled": True}}}


def fixture(*, completion=256, reasoning=39):
    body = {"model": "grok-4.5", "messages": [{"role": "user", "content": "Continue."}],
            "max_completion_tokens": 256}
    response = {"id": "public_fixture", "model": "grok-4.5", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "amber " * 128}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 6, "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning}, "total_tokens": 6 + completion + reasoning}}
    return body, SimpleNamespace(usage=response["usage"], response_json=response, success=True, status_code=200)


def audit(body, result, *, source="xai", contract="grok_chat_completions", transport="chat_completions"):
    return audit_exchange(body, result, transport, CONFIG, "offline", accounting_source_id=source, accounting_contract_id=contract)


def test_exact_visible_budget_preserves_normalized_total_and_usage_arithmetic():
    body, result = fixture()
    check = audit(body, result)
    assert check["usage_arithmetic"]["status"] == "pass"
    assert check["usage_accounting"]["output_tokens"] == 295
    assert check["usage_accounting"]["answer_tokens"] == 256
    assert check["usage_accounting"]["thinking_tokens"] == 39
    output = check["gross_plausibility"]["output"]
    assert output["status"] == "pass"
    assert output["request_limit_compared_tokens"] == output["request_limit_tokens"] == 256
    assert output["request_limit_counter"] == "usage.completion_tokens"
    assert output["reported_total_tokens"] == 295


@pytest.mark.parametrize("source,contract", [(None, None), ("openai", "grok_chat_completions"),
    ("foreign", "grok_chat_completions"), ("xai", "openai_gpt56_chat"), ("xai", None)])
def test_unbound_or_wrong_source_contract_cannot_claim_visible_budget(source, contract):
    body, result = fixture()
    check = audit(body, result, source=source, contract=contract)
    assert "request_limit_scope" not in check["gross_plausibility"]["output"]
    assert check["usage_arithmetic"]["status"] == "fail" and check["validation_pass"] is False


@pytest.mark.parametrize("field", ["max_tokens", "max_output_tokens", "maxOutputTokens"])
def test_other_budget_fields_do_not_inherit_new_semantics(field):
    body, result = fixture()
    body[field] = body.pop("max_completion_tokens")
    check = audit(body, result)
    assert "request_limit_scope" not in check["gross_plausibility"]["output"]
    assert check["gross_plausibility"]["output"]["status"] == "fail"


@pytest.mark.parametrize("other", [{"max_tokens": 256}, {"max_output_tokens": 256},
    {"generationConfig": {"maxOutputTokens": 256}}, {"max_tokens": None}])
def test_ambiguous_budget_aliases_keep_existing_fail_closed_comparison(other):
    body, result = fixture()
    body.update(other)
    check = audit(body, result)
    assert "request_limit_scope" not in check["gross_plausibility"]["output"]
    assert check["gross_plausibility"]["output"]["status"] == "fail"


def test_visible_completion_itself_over_budget_still_fails():
    body, result = fixture(completion=257)
    check = audit(body, result)
    assert check["usage_arithmetic"]["status"] == "pass"
    assert check["gross_plausibility"]["output"]["request_limit_compared_tokens"] == 257
    assert check["gross_plausibility"]["output"]["status"] == "fail"
    assert check["validation_pass"] is False


@pytest.mark.parametrize("completion", ["256", True, None])
def test_noninteger_native_completion_cannot_select_visible_budget(completion):
    body, result = fixture()
    result.usage["completion_tokens"] = completion
    check = audit(body, result)
    assert "request_limit_scope" not in check["gross_plausibility"]["output"]
    assert check["validation_pass"] is False


def test_other_transport_cannot_select_visible_chat_budget():
    body, result = fixture()
    check = audit(body, result, transport="openai_responses")
    assert "request_limit_scope" not in check["gross_plausibility"]["output"]
    assert check["validation_pass"] is False


@pytest.fixture(scope="module")
def actual_context():
    if not (BATCH / "case_05_response.bin").is_file():
        pytest.skip("P1M raw xAI fixtures are not retained in this checkout")
    from lib.config import _normalize_provider_config
    from lib.model_profile_catalog import resolve_runtime_parameter_config
    config = yaml.safe_load((PROJECT / "config.yaml").read_text())
    _normalize_provider_config(config, prune_unknown_models=True)
    resolved = resolve_runtime_parameter_config(config, "xai_official", "grok-4.5", "grok", "vendor_direct", "openai_chat_completions")
    db = resolved["model_profile_database"]
    assert db["source_id"] == "xai" and db["reference_contract_id"] == "grok_chat_completions"
    assert db["interface_id"] == "text/xai/grok/grok-4.5#openai-chat-default"
    return config, db


@pytest.mark.parametrize("ordinal,completion,total,expected_sha", [
    (5, 256, 2225, "15a47b42f66627ed4cdeeb809248cafb871daa66a73d14f79cde63fdd77487ce"),
    (6, 512, 2481, "05d559438073bdf3cd697cddb5819634c8723c98c2c048583bb616645c85e0f3"),
])
def test_actual_param_audit_path_replays_raw_without_changing_original_verdict(
        actual_context, ordinal, completion, total, expected_sha, monkeypatch):
    from scripts.param_test import _audit_exchange_safely
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("Offline retained replay cannot send HTTP"))
    config, db = actual_context
    record_path = BATCH / f"case_{ordinal:02d}_observation.json"
    original = record_path.read_bytes()
    saved = json.loads(original)
    raw = (BATCH / f"case_{ordinal:02d}_response.bin").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == saved["response_sha256"] == expected_sha
    response = json.loads(raw)
    assert response == saved["response_json"]
    body = copy.deepcopy(saved["body"])
    result = SimpleNamespace(usage=response["usage"], response_json=response, success=True,
                             status_code=200, finish_reason="length", error_type=None)
    check = _audit_exchange_safely(config, body, result, "chat_completions", "current_consumer_offline_replay",
        provider="xai_official", model="grok-4.5", accounting_source_id=db["source_id"],
        accounting_contract_id=db["reference_contract_id"], observed_request_body=body)
    assert check["request_integrity"]["status"] == "pass"
    assert check["usage_arithmetic"]["status"] == "pass" and check["usage_arithmetic"]["calculated_total_tokens"] == total
    assert check["usage_accounting"]["output_tokens"] == completion + 39
    assert check["usage_accounting"]["thinking_tokens"] == 39
    output = check["gross_plausibility"]["output"]
    assert output["status"] == "pass" and output["request_limit_compared_tokens"] == completion
    assert output["request_limit_counter"] == "usage.completion_tokens"
    assert check["output_completion"]["status"] == "fail" and check["validation_pass"] is False
    assert check["validation_failures"] and all("truncated" in reason for reason in check["validation_failures"])
    assert record_path.read_bytes() == original and saved["verdict"]["budget_respected"] is True
