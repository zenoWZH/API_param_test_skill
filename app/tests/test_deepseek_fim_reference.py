"""Exact shared FIM inputs, native envelopes and controlled semantics, offline."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from lib import deepseek_fim_reference as fim
from lib.model_profile_catalog import catalog_metadata, database_snapshot, get_model_profile_catalog


@pytest.fixture(scope="module")
def snapshot():
    catalog = get_model_profile_catalog()
    return database_snapshot({**catalog_metadata(), "source_id": "deepseek", "suite_family_id": "deepseek",
        "canonical_family_id": "deepseek", "profile_id": fim.PROFILE_ID, "interface_id": fim.INTERFACE_ID,
        "catalog_resolved": True, "profile": catalog.get_profile(fim.PROFILE_ID),
        "interface": catalog.get_interface(fim.INTERFACE_ID), "binding_source": "catalog_official_reference",
        "execution_target": {"provider_id": "deepseek_official", "request_model_id": "deepseek-v4-pro",
                             "route_profile": "vendor_direct", "api_form": fim.API_FORM}})


@pytest.fixture
def plan(snapshot):
    return fim.build_fim_plan(snapshot, suite_id=fim.SUITE_ID)


def _native(text):
    return {"id": "offline-fim-response", "created": 1, "object": "text_completion", "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "finish_reason": "stop", "text": text}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39,
                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 30}}


def _record(request, payload, status=200):
    return {"case_id": request.case_id, "request_body_sha256": request.body_sha256,
            "status_code": status, "response_raw": json.dumps(payload)}


@pytest.fixture
def records(plan):
    requests = plan.requests
    return [_record(requests[0], _native("42")), _record(requests[1], _native("73")),
        _record(requests[2], {"error": {"type": "invalid_request_error", "param": None,
            "message": "echo should not be used with suffix"}}, 400),
        _record(requests[3], {"error": {"type": "invalid_request_error", "param": None,
            "message": "Failed to deserialize suffix: invalid type map, expected a string"}}, 400),
        _record(requests[4], _native("LEAD_CUT_HERE_OMEGA")), _record(requests[5], _native("LEAD_")),
        _record(requests[6], _native("42")), _record(requests[7], _native(requests[7].body["prompt"] + "42"))]


def _redigest(snapshot):
    snapshot["snapshot_digest"] = fim.digest({k: v for k, v in snapshot.items() if k != "snapshot_digest"})


def _change(records, index, change):
    payload = json.loads(records[index]["response_raw"])
    change(payload)
    records[index]["response_raw"] = json.dumps(payload)


def test_current_shared_binding_supplies_eight_exact_fixed_bodies(snapshot, plan):
    parameter = snapshot["parameter_test_binding"]
    suite = parameter["fixed_case_suites"][fim.SUITE_ID]
    assert fim.digest(suite) == fim.SUITE_SHA256
    assert len(parameter["test_cases"]) == 15 and not set(fim.CASE_IDS) & set(parameter["test_cases"])
    assert [r.case_id for r in plan.requests] == list(fim.CASE_IDS)
    for request, case in zip(plan.requests, suite["case_definitions"]):
        assert request.body_bytes == fim.canonical_bytes(case["body"])
        assert request.body_sha256 == case["request_body_sha256"]
        assert request.expectation == case["expectation"]
        assert request.body["max_tokens"] == 512 and request.body["stream"] is False
        assert request.body["model"] == "deepseek-v4-pro"
    assert plan.request_cap == 8


def test_success_requires_all_five_distinct_causal_findings(plan, records):
    result = fim.evaluate_fim_plan(plan, records)
    assert result["pass"] and result["complete"]
    assert result["suffix_ab_effect_observed"] and result["stop_effect_observed"] and result["echo_effect_observed"]
    assert result["case_results"][2]["field_rejection_attributed"]
    assert result["case_results"][3]["field_rejection_attributed"]
    assert result["case_results"][4]["ast_literal_verified"]
    assert not result["code_executed"] and not result["token_exact_proof"] and not result["full_parameter_matrix_verified"]


def test_eight_verified_original_native_responses_replay_without_new_requests(plan):
    root = Path(__file__).resolve().parents[2]
    fact_path = root / "references/deepseek_fim_followup_facts_20260907.json"
    fact_raw = fact_path.read_bytes()
    assert hashlib.sha256(fact_raw).hexdigest() == fim.FACT_SHA256
    facts = json.loads(fact_raw)
    if datetime.now(timezone.utc) >= min(datetime.fromisoformat(b["expires_at"].replace("Z", "+00:00")) for b in facts["batches"]):
        pytest.skip("Original raw evidence expired under its approved P1M retention")
    records = []
    for request, fact in zip(plan.requests, facts["records"]):
        ref = fact["evidence"]["observation"]
        path = root / ref["path"]
        if not path.is_file():
            pytest.skip("Private P1M raw evidence is not distributed with this skill")
        assert not path.is_symlink()
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        observed = json.loads(raw)
        metadata = json.loads((path.parent / ".report-retention.json").read_bytes())
        assert metadata["period"] == "P1M" and metadata["expires_at"] == fact["evidence"]["raw_evidence_expires_at"]
        assert next(r for r in metadata["owned_files"] if r["path"] == path.name)["sha256"] == ref["sha256"]
        assert fim.canonical_bytes(observed["request"]) == request.body_bytes
        assert observed["request_sha256"] == request.body_sha256 == fact["request_body_sha256"]
        assert hashlib.sha256(observed["response_raw"].encode()).hexdigest() == fact["raw_response_sha256"]
        records.append({"case_id": request.case_id, "request_body_sha256": request.body_sha256,
                        "status_code": observed["status_code"], "response_raw": observed["response_raw"],
                        "response_sha256_before_redaction": fact["raw_response_sha256"]})
    result = fim.evaluate_fim_plan(plan, records)
    assert len(records) == 8 and result["pass"] and all(r["pass"] for r in result["case_results"])
    assert facts["batches"][0]["original_summary"]["echo_effect_observed"] is False
    assert result["case_results"][2]["status_code"] == result["case_results"][3]["status_code"] == 400


def test_plan_views_cannot_mutate_frozen_requests_or_expectations(plan, snapshot):
    before = fim.canonical_bytes(snapshot)
    request = plan.requests[0]
    request.body["max_tokens"] = 1
    request.expectation["value"] = False
    plan.snapshot["parameter_test_binding"]["fixed_case_suites"][fim.SUITE_ID]["case_definitions"][0]["body"]["max_tokens"] = 1
    assert plan.requests[0].body["max_tokens"] == 512 and plan.requests[0].expectation["value"] == 42
    assert fim.canonical_bytes(snapshot) == before
    with pytest.raises(FrozenInstanceError): plan.endpoint = "https://other.invalid"


@pytest.mark.parametrize("kwargs", [
    {"suite_id": None}, {"suite_id": "generic"}, {"suite_id": "deepseek_beta_prefix"},
    {"runs": True}, {"runs": 1.0}, {"runs": 2}, {"runs": 0}, {"job_type": "cache_suite"}, {"job_type": "quick_load"},
    {"endpoint": "https://api.deepseek.com/v1/completions"}, {"endpoint": "https://api.deepseek.com/beta/chat/completions"},
    {"endpoint": fim.ENDPOINT + "?test=1"}, {"endpoint": "http://api.deepseek.com/beta/completions"},
    {"endpoint": "https://api.deepseek.com.evil.invalid/beta/completions"},
])
def test_plan_accepts_only_explicit_fixed_suite_selection(snapshot, kwargs):
    with pytest.raises(ValueError): fim.build_fim_plan(snapshot, **{"suite_id": fim.SUITE_ID, **kwargs})


@pytest.mark.parametrize("mutation", [
    "digest", "schema_bool", "source", "model", "provider", "slug", "form", "route", "transport", "execution_transport",
    "path", "version", "profile_owner", "canonical", "contract", "parameter_source", "policy_source", "embedded_policy",
    "disabled", "gate_type", "pressure", "capability", "malformed_caps", "malformed_conflicts", "malformed_suites",
    "body", "body_float", "expectation", "case_order", "case_sha", "manifest_sha", "manifest_fact", "missing_suite",
])
def test_rehashed_snapshot_cannot_cross_exact_source_binding_or_suite(snapshot, mutation):
    s = copy.deepcopy(snapshot)
    i, p, b = s["interface"], s["profile"], s["parameter_test_binding"]
    suite = b["fixed_case_suites"][fim.SUITE_ID]
    if mutation == "digest": s["snapshot_digest"] = "0" * 64
    elif mutation == "schema_bool": s["snapshot_schema_version"] = True
    elif mutation == "source": s["source_id"] = "moonshot"
    elif mutation == "model": s["execution_target"]["request_model_id"] = "deepseek-v4-flash"
    elif mutation == "provider": s["execution_target"]["provider_id"] = "third_party"
    elif mutation == "slug": p["model_slug"] = "deepseek-v4-flash-0731"
    elif mutation == "form": s["execution_target"]["api_form"] = "openai_chat"
    elif mutation == "route": s["execution_target"]["route_profile"] = "dynamic_aggregator"
    elif mutation == "transport": i["transport_adapter_id"] = "chat_completions"
    elif mutation == "execution_transport": s["execution_target"]["transport"] = "chat_completions"
    elif mutation == "path": i["api_versions"] = {"beta": {"stability": "beta", "path_template": "/v1/completions"}}
    elif mutation == "version": i["default_api_version"] = "v1"
    elif mutation == "profile_owner": p["interface_ids"].remove(fim.INTERFACE_ID)
    elif mutation == "canonical": p["canonical_model_id"] = "deepseek/deepseek-v4-flash-0731"
    elif mutation == "contract": s["reference_contract"]["source_ids"] = ["moonshot"]
    elif mutation == "parameter_source": b["source_id"] = "moonshot"
    elif mutation == "policy_source": s["test_binding"]["source_id"] = "moonshot"
    elif mutation == "embedded_policy": i["test_bindings"][0]["suite_family_id"] = "other"
    elif mutation == "disabled": b["disabled_reason"] = "revoked"
    elif mutation == "gate_type": b["enabled"] = 1
    elif mutation == "pressure": b["pressure_test_enabled"] = True
    elif mutation == "capability": i.setdefault("parameter_capabilities", {})["suffix"] = {"state": "unsupported"}
    elif mutation == "malformed_caps": i["parameter_capabilities"] = []
    elif mutation == "malformed_conflicts": i["source_conflicts"] = []
    elif mutation == "malformed_suites": b["fixed_case_suites"] = []
    elif mutation == "body": suite["case_definitions"][0]["body"]["model"] = "deepseek-v4-flash"
    elif mutation == "body_float": suite["case_definitions"][0]["body"]["max_tokens"] = 512.0
    elif mutation == "expectation": suite["case_definitions"][0]["expectation"]["value"] = True
    elif mutation == "case_order": suite["case_definitions"].reverse()
    elif mutation == "case_sha": suite["case_definitions"][0]["case_definition_sha256"] = "0" * 64
    elif mutation == "manifest_sha": i["source_conflicts"]["fixed_fim_suite_manifest_20260908"]["suite_definition_sha256"] = "0" * 64
    elif mutation == "manifest_fact": i["source_conflicts"]["fixed_fim_suite_manifest_20260908"]["facts_artifact"]["sha256"] = "0" * 64
    else: del b["fixed_case_suites"][fim.SUITE_ID]
    if mutation != "digest": _redigest(s)
    with pytest.raises(ValueError): fim.build_fim_plan(s, suite_id=fim.SUITE_ID)


@pytest.mark.parametrize("owner", ["test_binding", "parameter_test_binding"])
@pytest.mark.parametrize("mutation", ["exclude", "malformed_exclude", "case_override", "nested_override", "parameter_override", "malformed_override"])
def test_fixed_case_exclusions_and_overrides_cannot_be_ignored(snapshot, owner, mutation):
    s = copy.deepcopy(snapshot)
    binding = s[owner]
    if mutation == "exclude": binding["excluded_test_profiles"] = [fim.CASE_IDS[0]]
    elif mutation == "malformed_exclude": binding["excluded_test_profiles"] = "bad"
    elif mutation == "case_override": binding["expectations"] = {fim.CASE_IDS[0]: "unsupported"}
    elif mutation == "nested_override": binding["model_expectations"] = {"deepseek-v4-pro": {fim.CASE_IDS[0]: "unsupported"}}
    elif mutation == "parameter_override": binding["parameter_expectations"] = {"suffix": "unsupported"}
    else: binding["default_expectations"] = []
    if owner == "test_binding":
        s["interface"]["test_bindings"] = [copy.deepcopy(binding) if r.get("test_binding_id") == fim.POLICY_BINDING_ID else r
                                           for r in s["interface"]["test_bindings"]]
    _redigest(s)
    with pytest.raises(ValueError): fim.build_fim_plan(s, suite_id=fim.SUITE_ID)


@pytest.mark.parametrize("mutation", ["model", "object", "id", "id_space", "created_bool", "error", "choice_count", "index_bool", "finish",
    "text_type", "empty_text", "usage_missing", "usage_bool", "usage_negative", "arithmetic", "cache_arithmetic", "output_cap"])
def test_http_200_requires_complete_strict_native_generation(plan, records, mutation):
    def change(p):
        if mutation == "model": p["model"] = "deepseek-v4-flash"
        elif mutation == "object": p["object"] = "chat.completion"
        elif mutation == "id": p.pop("id")
        elif mutation == "id_space": p["id"] = "  id "
        elif mutation == "created_bool": p["created"] = True
        elif mutation == "error": p["error"] = None
        elif mutation == "choice_count": p["choices"].append(copy.deepcopy(p["choices"][0]))
        elif mutation == "index_bool": p["choices"][0]["index"] = False
        elif mutation == "finish": p["choices"][0]["finish_reason"] = "length"
        elif mutation == "text_type": p["choices"][0]["text"] = 42
        elif mutation == "empty_text": p["choices"][0]["text"] = ""
        elif mutation == "usage_missing": p["usage"].pop("prompt_cache_miss_tokens")
        elif mutation == "usage_bool": p["usage"]["prompt_cache_hit_tokens"] = False
        elif mutation == "usage_negative": p["usage"]["prompt_cache_hit_tokens"] = -1
        elif mutation == "arithmetic": p["usage"]["total_tokens"] += 1
        elif mutation == "cache_arithmetic": p["usage"]["prompt_cache_miss_tokens"] += 1
        else: p["usage"].update(completion_tokens=513, total_tokens=543)
    _change(records, 0, change)
    result = fim.evaluate_fim_plan(plan, records)
    assert not result["pass"] and not result["suffix_ab_effect_observed"]
    assert not result["case_results"][0]["response_valid"]


@pytest.mark.parametrize("index,text", [(0, "73"), (0, "True"), (0, "42.0"), (0, "42\nprint('unexpected')"),
    (1, "42"), (4, "'LEAD_CUT_HERE_OMEGA'"), (4, "WRONG"), (5, "LEAD_CUT_HERE_OMEGA"), (5, "LEAD_CUT_HERE"), (7, "42")])
def test_plausible_text_does_not_replace_ast_or_pair_semantics(plan, records, index, text):
    _change(records, index, lambda p: p["choices"][0].update(text=text))
    result = fim.evaluate_fim_plan(plan, records)
    assert not result["pass"] and not result["case_results"][index]["pass"]
    if index == 4: assert not result["stop_effect_observed"]


def test_generated_code_is_parsed_but_never_executed(plan, records, tmp_path):
    target = tmp_path / "must-not-exist"
    text = f"42\n__import__('pathlib').Path({str(target)!r}).write_text('executed')\n"
    _change(records, 0, lambda p: p["choices"][0].update(text=text))
    result = fim.evaluate_fim_plan(plan, records)
    assert not target.exists() and not result["case_results"][0]["pass"] and result["code_executed"] is False


@pytest.mark.parametrize("index,error", [
    (2, {"type": "invalid_request_error", "param": "model", "message": "echo should not be used with suffix"}),
    (2, {"type": "invalid_request_error", "param": "echo", "message": "echo is unsupported"}),
    (2, {"type": "authentication_error", "param": "echo", "message": "echo should not be used with suffix"}),
    (3, {"type": "invalid_request_error", "param": "model", "message": "suffix invalid type"}),
    (3, {"type": "invalid_request_error", "param": None, "message": "expected a string"}),
    (3, {"type": "invalid_request_error", "param": "suffix", "message": "unknown model suffix type"}),
    (3, {"type": "invalid_request_error", "param": "suffix", "message": "suffix failed"}),
])
def test_rejections_require_field_attribution_separate_from_echo_support(plan, records, index, error):
    records[index]["response_raw"] = json.dumps({"error": error})
    result = fim.evaluate_fim_plan(plan, records)
    assert not result["case_results"][index]["pass"] and not result["pass"]
    assert result["echo_effect_observed"]


def test_suffix_error_param_can_supply_field_attribution_without_repeating_name(plan, records):
    records[3]["response_raw"] = json.dumps({"error": {"type": "validation_error", "param": "suffix", "message": "expected a string"}})
    records[3]["status_code"] = 422
    assert fim.evaluate_fim_plan(plan, records)["pass"]


@pytest.mark.parametrize("extra", [{"model": "deepseek-v4-flash"}, {"choices": {}}])
def test_error_envelope_cannot_claim_other_model_or_malformed_choices(plan, records, extra):
    _change(records, 2, lambda payload: payload.update(extra))
    result = fim.evaluate_fim_plan(plan, records)
    assert not result["case_results"][2]["pass"] and not result["pass"]


@pytest.mark.parametrize("raw", [b"", b"\xff", b"{}{}", b'{"error":null,"error":{}}', b'{"value":NaN}', b"[]", b"null", b"[DONE]"])
def test_invalid_duplicate_or_nonobject_response_never_passes(plan, records, raw):
    records[0]["response_raw"] = raw
    assert not fim.evaluate_fim_plan(plan, records)["case_results"][0]["pass"]


@pytest.mark.parametrize("flags", [{"response_complete": False}, {"response_complete": 0}, {"response_complete": 1},
    {"client_entered": False}, {"client_entered": 1}, {"interrupted": True}, {"interrupted": 0},
    {"failure_type": "ReadTimeout"}, {"response_sha256_before_redaction": "0" * 64}])
def test_transport_or_original_hash_failures_do_not_become_semantic_passes(plan, records, flags):
    records[0].update(flags)
    result = fim.evaluate_fim_plan(plan, records)
    assert not result["case_results"][0]["pass"] and not result["suffix_ab_effect_observed"]


@pytest.mark.parametrize("size", [0, 1, 2, 5, 7])
def test_partial_collection_is_reported_without_full_suite_claim(plan, records, size):
    selected = records[:size]
    result = fim.evaluate_fim_plan(plan, selected)
    assert result["requests_recorded"] == size and not result["complete"] and not result["pass"]
    assert fim.next_fim_request(plan, selected).case_id == fim.CASE_IDS[size]


@pytest.mark.parametrize("mutation", ["too_many", "duplicate", "out_of_order", "hash", "bool_status", "float_status", "missing_status", "missing_raw"])
def test_dispatch_ledger_must_match_unique_ordered_fixed_requests(plan, records, mutation):
    if mutation == "too_many": records.append(copy.deepcopy(records[-1]))
    elif mutation == "duplicate": records[1] = copy.deepcopy(records[0])
    elif mutation == "out_of_order": records.reverse()
    elif mutation == "hash": records[0]["request_body_sha256"] = "0" * 64
    elif mutation == "bool_status": records[0]["status_code"] = True
    elif mutation == "float_status": records[0]["status_code"] = 200.0
    elif mutation == "missing_status": records[0].pop("status_code")
    else: records[0].pop("response_raw")
    with pytest.raises(ValueError): fim.evaluate_fim_plan(plan, records)
    with pytest.raises(ValueError): fim.next_fim_request(plan, records)


def test_complete_collection_has_no_ninth_request(plan, records):
    assert fim.next_fim_request(plan, records) is None


def test_mock_or_subclass_plans_are_rejected(plan):
    class MockPlan:
        requests = plan.requests
    class OverridePlan(fim.FimPlan):
        @property
        def requests(self): return ()
    for wrong in (MockPlan(), OverridePlan(plan._snapshot_bytes)):
        with pytest.raises(ValueError): fim.next_fim_request(wrong, [])
        with pytest.raises(ValueError): fim.evaluate_fim_plan(wrong, [])


def test_execution_uses_snapshot_without_reading_catalog_or_raw_files(plan, records, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Runtime FIM reread filesystem or live catalog")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert fim.evaluate_fim_plan(plan, records)["pass"]
