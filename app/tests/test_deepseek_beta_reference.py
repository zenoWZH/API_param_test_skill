"""Offline exact-beta snapshot, wire and paired-semantic regression."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from lib import deepseek_beta_reference as beta
from lib.model_profile_catalog import catalog_metadata, database_snapshot, get_model_profile_catalog


@pytest.fixture(scope="module")
def snapshot():
    catalog = get_model_profile_catalog()
    return database_snapshot({**catalog_metadata(), "source_id": beta.SOURCE_ID,
        "suite_family_id": beta.FAMILY_ID, "canonical_family_id": beta.FAMILY_ID,
        "profile_id": beta.PROFILE_ID, "interface_id": beta.INTERFACE_ID, "catalog_resolved": True,
        "profile": catalog.get_profile(beta.PROFILE_ID), "interface": catalog.get_interface(beta.INTERFACE_ID),
        "binding_source": "catalog_official_reference", "execution_target": {
            "provider_id": "deepseek_official", "request_model_id": beta.REQUEST_MODEL_ID,
            "route_profile": "vendor_direct", "api_form": beta.API_FORM}})


@pytest.fixture
def plan(snapshot):
    return beta.build_deepseek_beta_reference_plan(snapshot, endpoint=beta.ENDPOINT)


def _redigest(snapshot):
    snapshot["snapshot_digest"] = hashlib.sha256(beta.canonical_bytes(
        {k: v for k, v in snapshot.items() if k != "snapshot_digest"})).hexdigest()


def _native(content):
    return {"id": "offline-beta-response", "object": "chat.completion", "created": 1,
        "model": beta.REQUEST_MODEL_ID, "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26,
                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 20}}


def _record(request, payload, status=200):
    return {"case_id": request.case_id, "request_body_sha256": request.body_sha256,
            "status_code": status, "response_raw": json.dumps(payload)}


@pytest.fixture
def records(plan):
    requests = plan.requests
    return [_record(requests[0], _native('{"answer":2,"tail":"LEAD_CUT_HERE_OMEGA"}')),
        _record(requests[1], _native('OMEGA"}')),
        _record(requests[2], {"error": {"type": "invalid_request_error", "param": None,
            "message": 'Failed to deserialize the JSON body into the target type: messages[1]: invalid type: string "not-a-boolean", expected a boolean at line 1 column 351'}}, 400),
        _record(requests[3], _native('LEAD_CUT_HERE_OMEGA"}')),
        _record(requests[4], _native('LEAD_'))]


def _change_payload(records, index, change):
    payload = json.loads(records[index]["response_raw"])
    change(payload)
    records[index]["response_raw"] = json.dumps(payload)


def test_real_installed_snapshot_supplies_exact_five_bodies_and_expectations(plan, snapshot):
    requests = plan.requests
    assert [r.case_id for r in requests] == list(beta.CASE_IDS)
    assert plan.request_cap == 5 and plan.retries == 0 and plan.identity_probe_requests == 0
    definitions = {c["case_id"]: c for c in snapshot["parameter_test_binding"]["case_definitions"]}
    assert len(definitions) == 8
    for request in requests:
        definition = definitions[request.case_id]
        assert request.body == definition["body"] and request.expectation == definition["expectation"]
        assert request.body_bytes == beta.canonical_bytes(definition["body"])
        assert request.method == "POST" and request.endpoint == beta.ENDPOINT
        assert request.body["max_tokens"] == 512 and request.body["thinking"] == {"type": "disabled"}
        assert request.body["stream"] is False
    assert requests[2].expectation == "unsupported"
    assert snapshot["interface"]["source_conflicts"][beta.OBSERVATION_KEY]["pro_app_fixed_case_candidate"]["enabled_by_this_metadata"] is False


def test_request_and_snapshot_copies_cannot_mutate_frozen_plan(plan, snapshot):
    original = copy.deepcopy(snapshot)
    request = plan.requests[0]
    body = request.body
    body["max_tokens"] = 1
    view = plan.snapshot
    view["parameter_test_binding"]["case_definitions"][-1]["body"]["max_tokens"] = 1
    assert request.body["max_tokens"] == 512 and plan.requests[0].body["max_tokens"] == 512
    assert snapshot == original
    with pytest.raises(FrozenInstanceError):
        plan.endpoint = "https://example.invalid"


def test_only_exact_official_endpoint_one_run_and_parameter_job_are_available(snapshot):
    for endpoint in ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com/beta/completions",
                     "https://api.deepseek.com/beta/chat/completions?x=1", "https://gateway.example/beta/chat/completions",
                     "https://api.deepseek.com.evil.example/beta/chat/completions", "http://api.deepseek.com/beta/chat/completions"):
        with pytest.raises(ValueError):
            beta.build_deepseek_beta_reference_plan(snapshot, endpoint=endpoint)
    for runs in (True, 1.0, 0, 2):
        with pytest.raises(ValueError):
            beta.build_deepseek_beta_reference_plan(snapshot, endpoint=beta.ENDPOINT, runs=runs)
    for job in ("cache_suite", "quick_load", "staircase", "soak"):
        with pytest.raises(ValueError):
            beta.build_deepseek_beta_reference_plan(snapshot, endpoint=beta.ENDPOINT, job_type=job)


@pytest.mark.parametrize("mutation", ["snapshot_digest", "source", "family", "model", "slug", "canonical", "form",
    "route", "transport", "path", "version", "profile_owner", "contract", "policy", "parameter", "pressure",
    "disabled", "scope", "unsupported", "duplicate_case", "missing_case", "body", "body_float", "expectation",
    "candidate_list", "summary_sha", "fact_sha", "payload_pin", "embedded_policy", "snapshot_schema_bool"])
def test_rehashed_snapshot_cannot_cross_identity_manifest_or_evidence_scope(snapshot, mutation):
    s = copy.deepcopy(snapshot)
    p, i, b, policy = s["profile"], s["interface"], s["parameter_test_binding"], s["test_binding"]
    metadata = i["source_conflicts"][beta.OBSERVATION_KEY]
    if mutation == "snapshot_digest": s["snapshot_digest"] = "0" * 64
    elif mutation == "source": p["source_id"] = "moonshot"
    elif mutation == "family": i["family_id"] = "kimi"
    elif mutation == "model": s["execution_target"]["request_model_id"] = "deepseek-v4-flash"
    elif mutation == "slug": p["model_slug"] = "deepseek-v4-flash-0731"
    elif mutation == "canonical": p["canonical_model_id"] = "deepseek/deepseek-v4-flash-0731"
    elif mutation == "form": s["execution_target"]["api_form"] = "openai_chat_completions"
    elif mutation == "route": s["execution_target"]["route_profile"] = "dynamic_aggregator"
    elif mutation == "transport": i["transport_adapter_id"] = "chat_completions"
    elif mutation == "path": i["api_versions"]["beta"]["path_template"] = "/chat/completions"
    elif mutation == "version": i["default_api_version"] = "v1"
    elif mutation == "profile_owner": p["interface_ids"].remove(beta.INTERFACE_ID)
    elif mutation == "contract": s["reference_contract"]["source_ids"] = ["moonshot"]
    elif mutation == "policy": policy["interface_id"] = "wrong"
    elif mutation == "parameter": b["source_id"] = "moonshot"
    elif mutation == "pressure": i["pressure_test_enabled"] = True
    elif mutation == "disabled": b["disabled_reason"] = "revoked"
    elif mutation == "scope": policy["certification_scope"] = "generic"
    elif mutation == "unsupported": i.setdefault("parameter_capabilities", {})["stop"] = {"state": "unsupported"}
    elif mutation == "duplicate_case": b["case_definitions"].append(copy.deepcopy(b["case_definitions"][-1]))
    elif mutation == "missing_case": b["test_cases"].pop()
    elif mutation == "body": b["case_definitions"][-1]["body"]["messages"][0]["content"] = "replaced"
    elif mutation == "body_float": b["case_definitions"][-1]["body"]["max_tokens"] = 512.0
    elif mutation == "expectation": b["case_definitions"][-1]["expectation"] = "unsupported"
    elif mutation == "candidate_list": metadata["pro_app_fixed_case_candidate"]["supported_case_ids"].append("deepseek_beta_prefix_json")
    elif mutation == "summary_sha": metadata["summary_artifact"]["sha256"] = "0" * 64
    elif mutation == "fact_sha": metadata["facts_artifact"]["sha256"] = "0" * 64
    elif mutation == "payload_pin":
        b["case_definitions"][-1]["body"]["max_tokens"] = 1024
        manifest = metadata["binding_case_manifest"][-1]
        manifest["case_definition_sha256"] = hashlib.sha256(beta.canonical_bytes(b["case_definitions"][-1])).hexdigest()
        manifest["request_body_sha256"] = hashlib.sha256(beta.canonical_bytes(b["case_definitions"][-1]["body"])).hexdigest()
    elif mutation == "embedded_policy": i["test_bindings"][0]["label"] = "conflict"
    elif mutation == "snapshot_schema_bool": s["snapshot_schema_version"] = True
    if mutation != "snapshot_digest": _redigest(s)
    with pytest.raises(ValueError):
        beta.build_deepseek_beta_reference_plan(s, endpoint=beta.ENDPOINT)


def test_framing_success_keeps_strict_tail_failure_and_original_stop_gap(plan, records):
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert result["complete"] and result["bounded_observations_pass"]
    assert result["pass"] is False and result["historical_strict_tail_target_met"] is False
    assert result["original_stop_status"] == "insufficient_evidence"
    assert not result["full_parameter_matrix_verified"] and not result["token_exact_proof"]
    true_case = result["case_results"][1]
    assert true_case["response_valid"] and true_case["behavior_pass"]
    assert not true_case["strict_target_pass"] and not true_case["pass"]
    assert true_case["returned_content_mode"] == "continuation_only"
    assert result["case_results"][2]["type_attribution"]["attributed"]
    assert result["case_results"][-1]["stop_status"] == "observed"


@pytest.mark.parametrize("owner", ["test_binding", "parameter_test_binding"])
@pytest.mark.parametrize("mutation", ["excluded", "bad_excluded", "expectations", "default_expectations", "bad_expectations", "model_expectations"])
def test_binding_exclusions_and_expectation_overrides_are_enforced(snapshot, owner, mutation):
    s = copy.deepcopy(snapshot)
    binding = s[owner]
    if mutation == "excluded": binding["excluded_test_profiles"] = [beta.CASE_IDS[3]]
    elif mutation == "bad_excluded": binding["excluded_test_profiles"] = "bad"
    elif mutation == "bad_expectations": binding["expectations"] = []
    elif mutation == "model_expectations": binding["model_expectations"] = {beta.MODEL_SLUG: {beta.CASE_IDS[3]: "unsupported"}}
    else: binding[mutation] = {beta.CASE_IDS[3]: "unsupported"}
    if owner == "test_binding":
        s["interface"]["test_bindings"] = [copy.deepcopy(binding) if row.get("test_binding_id") == beta.POLICY_BINDING_ID else row
                                           for row in s["interface"]["test_bindings"]]
    _redigest(s)
    with pytest.raises(ValueError):
        beta.build_deepseek_beta_reference_plan(s, endpoint=beta.ENDPOINT)


@pytest.mark.parametrize("owner", ["profile", "reference_contract"])
@pytest.mark.parametrize("field,value", [("enabled", False), ("executable", False), ("runner_enabled", False),
                                        ("parameter_test_enabled", False), ("disabled_reason", "revoked")])
def test_profile_and_contract_explicit_revocations_are_not_ignored(snapshot, owner, field, value):
    s = copy.deepcopy(snapshot)
    s[owner][field] = value
    _redigest(s)
    with pytest.raises(ValueError):
        beta.build_deepseek_beta_reference_plan(s, endpoint=beta.ENDPOINT)


@pytest.mark.parametrize("full_prefix", [False, True])
def test_new_strict_target_success_does_not_rewrite_historical_failure(plan, records, full_prefix):
    text = 'LEAD_CUT_HERE_OMEGA"}'
    if full_prefix:
        text = plan.requests[1].body["messages"][-1]["content"] + text
    _change_payload(records, 1, lambda p: p["choices"][0]["message"].update(content=text))
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert result["pass"] and result["bounded_observations_pass"]
    assert result["case_results"][1]["strict_target_pass"]
    assert result["historical_strict_tail_target_met"] is False


def test_full_prefix_wrong_tail_cannot_inherit_continuation_only_framing_relaxation(plan, records):
    text = plan.requests[1].body["messages"][-1]["content"] + 'OMEGA"}'
    _change_payload(records, 1, lambda p: p["choices"][0]["message"].update(content=text))
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert not result["case_results"][1]["strict_target_pass"]
    assert not result["case_results"][1]["behavior_pass"]
    assert not result["bounded_observations_pass"]


@pytest.mark.parametrize("change", [
    lambda p: p.update(model="deepseek-v4-flash"),
    lambda p: p.update(id=" bad "),
    lambda p: p.update(object="text_completion"),
    lambda p: p.update(created=True),
    lambda p: p.update(error={"message": "mixed"}),
    lambda p: p.update(choices=[]),
    lambda p: p["choices"].append(copy.deepcopy(p["choices"][0])),
    lambda p: p.update(choices=[None]),
    lambda p: p["choices"][0].update(index=False),
    lambda p: p["choices"][0].update(finish_reason="length"),
    lambda p: p["choices"][0].update(message=[]),
    lambda p: p["choices"][0]["message"].update(role="user"),
    lambda p: p["choices"][0]["message"].update(content=[]),
    lambda p: p["choices"][0]["message"].update(reasoning_content="unexpected"),
    lambda p: p["choices"][0]["message"].update(tool_calls=[{"function": {}}]),
    lambda p: p["choices"][0]["message"].update(tool_calls={}),
    lambda p: p["choices"][0]["message"].update(reasoning_content=False),
    lambda p: p["choices"][0]["message"].update(refusal=False),
    lambda p: p["choices"][0]["message"].update(refusal="refused"),
    lambda p: p.update(usage=[]),
    lambda p: p["usage"].pop("total_tokens"),
    lambda p: p["usage"].update(completion_tokens=True),
    lambda p: p["usage"].update(completion_tokens=513, total_tokens=533),
    lambda p: p["usage"].update(total_tokens=100),
    lambda p: p["usage"].update(prompt_cache_hit_tokens=-1),
    lambda p: p["usage"].update(prompt_cache_miss_tokens=19),
])
def test_native_http_200_requires_exact_envelope_and_usage(plan, records, change):
    _change_payload(records, 1, change)
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert not result["case_results"][1]["response_valid"]
    assert not result["case_results"][1]["behavior_pass"]
    assert not result["case_results"][2]["pass"]
    assert not result["bounded_observations_pass"]


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '[]', 'null', b'\xff'])
def test_strict_original_response_decoder_rejects_ambiguous_json(raw):
    with pytest.raises(ValueError):
        beta.decode_beta_response(raw)


def test_duplicate_envelope_key_cannot_hide_in_already_decoded_payload(plan, records):
    records[0]["response_raw"] = records[0]["response_raw"].replace('"model":', '"model":"wrong", "model":', 1)
    records[0]["response_json"] = json.loads(records[0]["response_raw"])
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert not result["case_results"][0]["response_valid"]
    assert not result["bounded_observations_pass"]


@pytest.mark.parametrize("text", ['{"answer":true,"tail":"LEAD_CUT_HERE_OMEGA"}',
    '{"answer":2,"tail":"LEAD_CUT_HERE_OMEGA","extra":1}',
    '{"answer":2,"answer":2,"tail":"LEAD_CUT_HERE_OMEGA"}'])
def test_control_json_semantics_reject_bool_extra_or_duplicate_members(plan, records, text):
    _change_payload(records, 0, lambda p: p["choices"][0]["message"].update(content=text))
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert not result["case_results"][0]["pass"] and not result["case_results"][1]["behavior_pass"]


@pytest.mark.parametrize("kind", ["accepted", "auth", "wrong_field", "generic", "misleading_param", "mixed", "positive_invalid"])
def test_type_rejection_requires_its_valid_positive_and_precise_attribution(plan, records, kind):
    if kind == "accepted": records[2]["status_code"] = 200
    elif kind == "auth": records[2]["status_code"] = 401
    elif kind == "wrong_field": _change_payload(records, 2, lambda p: p["error"].update(param="model", message="model invalid"))
    elif kind == "generic": _change_payload(records, 2, lambda p: p["error"].update(message="prefix has an error"))
    elif kind == "misleading_param": _change_payload(records, 2, lambda p: p["error"].update(param="model"))
    elif kind == "mixed": _change_payload(records, 2, lambda p: p.update(usage={}))
    elif kind == "positive_invalid": _change_payload(records, 1, lambda p: p.update(model="other"))
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert not result["case_results"][2]["pass"]


def test_structured_prefix_error_is_accepted_for_same_fixed_mutation(plan, records):
    _change_payload(records, 2, lambda p: p["error"].update(param="messages[1].prefix", message="boolean required"))
    assert beta.evaluate_deepseek_beta_reference(plan, records)["case_results"][2]["pass"]


@pytest.mark.parametrize("base,stop,status", [
    ('OMEGA"}', 'LEAD_', "insufficient_evidence"),
    ('WRONG_CUT_HERE_OMEGA"}', 'WRONG_', "insufficient_evidence"),
    ('LEAD_CUT_HERE_OMEGA"}', ' LEAD_', "not_observed"),
    ('LEAD_CUT_HERE_OMEGA"}', 'LEAD_\n', "not_observed"),
    ('LEAD_CUT_HERE_OMEGA"}', 'LEAD_CUT_HERE', "not_observed"),
])
def test_stop_keeps_actual_literal_positive_control_and_exact_boundary(plan, records, base, stop, status):
    _change_payload(records, 3, lambda p: p["choices"][0]["message"].update(content=base))
    _change_payload(records, 4, lambda p: p["choices"][0]["message"].update(content=stop))
    result = beta.evaluate_deepseek_beta_reference(plan, records)
    assert result["case_results"][-1]["stop_status"] == status and not result["case_results"][-1]["pass"]


@pytest.mark.parametrize("full_baseline", [False, True])
@pytest.mark.parametrize("full_stopped", [False, True])
def test_stop_segment_compares_exact_prefill_boundary_without_claiming_complete_json(
    plan, records, full_baseline, full_stopped
):
    prefix = plan.requests[3].body["messages"][-1]["content"]
    baseline = (prefix if full_baseline else "") + 'LEAD_CUT_HERE_OMEGA"}'
    stopped = (prefix if full_stopped else "") + "LEAD_"
    _change_payload(records, 3, lambda p: p["choices"][0]["message"].update(content=baseline))
    _change_payload(records, 4, lambda p: p["choices"][0]["message"].update(content=stopped))
    row = beta.evaluate_deepseek_beta_reference(plan, records)["case_results"][-1]
    assert row["pass"] and row["token_validation_pass"]
    assert row["output_semantics"] == "intentional_stop_segment"
    assert row["complete_json_output"] is False
    assert row["token_cap_truncation_accepted"] is False
    assert row["stop_boundary"]["expected_visible_segment"] == "LEAD_"
    assert row["stop_boundary"]["actual_prefill_and_segment"] == prefix + "LEAD_"
    assert row["stop_boundary"]["exact_boundary_match"] is True


@pytest.mark.parametrize("content", ["LEAD", "LEAD_X"])
def test_stop_segment_rejects_missing_or_extra_characters(plan, records, content):
    _change_payload(records, 4, lambda p: p["choices"][0]["message"].update(content=content))
    row = beta.evaluate_deepseek_beta_reference(plan, records)["case_results"][-1]
    assert row["pass"] is False
    assert row["stop_boundary"]["exact_boundary_match"] is False


def test_fixed_stop_does_not_accept_token_cap_truncation(plan, records):
    _change_payload(records, 4, lambda p: p["choices"][0].update(finish_reason="length"))
    row = beta.evaluate_deepseek_beta_reference(plan, records)["case_results"][-1]
    assert row["pass"] is False and row["token_validation_pass"] is False
    assert row["token_audit"]["exchanges"][0]["output_completion"]["status"] == "fail"


def test_fixed_suite_rejects_arithmetically_valid_unexplained_input_expansion(plan, records):
    _change_payload(records, 0, lambda p: p["usage"].update(
        prompt_tokens=2048, prompt_cache_miss_tokens=2048, total_tokens=2054
    ))
    row = beta.evaluate_deepseek_beta_reference(plan, records)["case_results"][0]
    assert row["response_valid"] is True
    assert row["token_validation_pass"] is False and row["pass"] is False
    assert row["token_audit"]["exchanges"][0]["gross_plausibility"]["input"]["status"] == "fail"


def test_partial_plan_has_no_extra_identity_or_retry_case(plan, records):
    for count in range(5):
        assert beta.next_deepseek_beta_request(plan, records[:count]).case_id == beta.CASE_IDS[count]
        result = beta.evaluate_deepseek_beta_reference(plan, records[:count])
        assert not result["complete"] and not result["pass"] and not result["bounded_observations_pass"]
    assert beta.next_deepseek_beta_request(plan, records) is None
    failed = copy.deepcopy(records[:1])
    failed[0].update(status_code=None, response_raw="")
    assert beta.next_deepseek_beta_request(plan, failed).case_id == beta.CASE_IDS[1]


@pytest.mark.parametrize("mutation", ["duplicate", "order", "too_many", "body", "missing_raw", "dict_raw", "status_bool", "missing_status", "identity_case"])
def test_record_sequence_rejects_unsupported_or_retried_dispatch(plan, records, mutation):
    if mutation == "duplicate": records[1] = copy.deepcopy(records[0])
    elif mutation == "order": records[0], records[1] = records[1], records[0]
    elif mutation == "too_many": records.append(copy.deepcopy(records[0]))
    elif mutation == "body": records[0]["request_body_sha256"] = "0" * 64
    elif mutation == "missing_raw": records[0].pop("response_raw")
    elif mutation == "dict_raw": records[0]["response_raw"] = {}
    elif mutation == "status_bool": records[0]["status_code"] = True
    elif mutation == "missing_status": records[0].pop("status_code")
    else: records[0]["case_id"] = "identity_probe"
    with pytest.raises(ValueError): beta.next_deepseek_beta_request(plan, records)
    with pytest.raises(ValueError): beta.evaluate_deepseek_beta_reference(plan, records)
