"""Claude fixed-suite snapshot and native semantic validation, offline."""
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from lib import anthropic_prefill_reference as prefill
from lib import model_profile_catalog as mpdb

ROOT = Path(__file__).resolve().parents[2]


def inject_candidate(snapshot):
    """Exercise the reviewed candidate before or after its shared installation."""
    if snapshot.get("source_id") != "anthropic" or snapshot.get("api_form") != prefill.API_FORM or snapshot.get("model_slug") not in prefill.MODELS:
        return snapshot
    model = snapshot["model_slug"]
    raw = (ROOT / "references/anthropic_prefill_fixed_suites_20260908.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == prefill.REFERENCE_SHA256
    suite = json.loads(raw)["suites"][prefill.suite_id(model)]
    manifest = {**prefill.identity(model), "api_version": prefill.API_VERSION, "suite_id": prefill.suite_id(model),
        "approval_leaf": suite["approval_leaf"], "reference_approval_leaf": suite["reference_approval_leaf"],
        "request_cap": 3, "selection_mode": "explicit_only", "generic_runner_dispatch": False,
        "suite_artifact": {"path": "references/anthropic_prefill_fixed_suites_20260908.json", "sha256": prefill.REFERENCE_SHA256},
        "suite_definition_sha256": prefill.digest(suite), "facts_artifact": copy.deepcopy(suite["facts_artifact"]),
        "case_manifest": [{key: row[key] for key in ("case_id", "historical_case_id", "request_body_sha256", "case_definition_sha256")}
                          for row in suite["case_definitions"]],
        "full_parameter_matrix_verified": False, "token_exact_proof": False, "generic_test_cases_changed": False,
        "execution_gates_changed": False}
    assert prefill.digest(manifest) == prefill.PINS[model][1]
    snapshot["parameter_test_binding"].setdefault("fixed_case_suites", {})[prefill.suite_id(model)] = suite
    snapshot["interface"].setdefault("source_conflicts", {})["fixed_prefill_suite_manifest_20260908"] = manifest
    redigest(snapshot)
    return snapshot


def redigest(snapshot):
    snapshot["snapshot_digest"] = prefill.digest({k: v for k, v in snapshot.items() if k != "snapshot_digest"})


@pytest.fixture(scope="module", params=prefill.MODELS)
def model(request):
    return request.param


@pytest.fixture(scope="module")
def snapshot(model):
    catalog = mpdb.get_model_profile_catalog()
    scope = prefill.identity(model)
    return inject_candidate(mpdb.database_snapshot({**mpdb.catalog_metadata(), **scope, "suite_family_id": "claude",
        "canonical_family_id": "claude", "catalog_resolved": True, "profile": catalog.get_profile(scope["profile_id"]),
        "interface": catalog.get_interface(scope["interface_id"]), "binding_source": "catalog_official_reference",
        "execution_target": {"provider_id": "anthropic_official", "request_model_id": model,
                             "route_profile": "vendor_direct", "api_form": prefill.API_FORM}}))


@pytest.fixture
def plan(snapshot, model):
    return prefill.build_prefill_plan(snapshot, suite_id=prefill.suite_id(model))


def native(model, text, *, stopped=False):
    return {"id": "offline-anthropic-prefill-id", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}], "stop_reason": "stop_sequence" if stopped else "end_turn",
        "stop_sequence": "CUT_HERE" if stopped else None, "usage": {"input_tokens": 64, "output_tokens": 14}}


@pytest.fixture
def records(plan):
    payloads = [native(plan.model, 'LEAD_CUT_HERE_OMEGA"}'), native(plan.model, "LEAD_", stopped=True),
        {"type": "error", "error": {"type": "invalid_request_error", "message": "messages.1.content: Input should be a valid string or list"}}]
    return [{"case_id": request.case_id, "request_body_sha256": request.body_sha256,
             "status_code": 400 if index == 2 else 200, "response_raw": json.dumps(payload)}
            for index, (request, payload) in enumerate(zip(plan.requests, payloads))]


def change(records, index, callback):
    payload = json.loads(records[index]["response_raw"])
    callback(payload)
    records[index]["response_raw"] = json.dumps(payload)


def test_three_exact_shared_requests_preserve_generic_thirty(snapshot, plan):
    assert len(snapshot["parameter_test_binding"]["test_cases"]) == 30
    assert plan.request_cap == len(plan.requests) == 3
    assert not {r.case_id for r in plan.requests} & set(snapshot["parameter_test_binding"]["test_cases"])
    for request in plan.requests:
        assert request.body["model"] == plan.model and request.body["max_tokens"] == 2048
        assert request.body["thinking"] == {"type": "disabled"} and request.body["stream"] is False
        assert prefill.digest(request.body) == request.body_sha256


def test_fresh_pass_requires_native_json_stop_and_type_control(plan, records):
    result = prefill.evaluate_prefill_plan(plan, records)
    assert result["complete"] and result["pass"] and result["stop_effect_observed"]
    assert result["case_results"][0]["assembled_json_verified"]
    assert result["case_results"][2]["field_rejection_attributed"]
    assert not result["historical_pass_reused"] and not result["token_exact_proof"] and not result["full_parameter_matrix_verified"]
    assert prefill.next_prefill_request(plan, records) is None


def test_nine_original_responses_independently_replay_with_sha_and_p1m(plan):
    raw = (ROOT / "references/anthropic_prefill_facts_20260908.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == prefill.FACT_SHA256
    facts = json.loads(raw)
    observation = next(row for row in facts["observations"] if row["request_model_id"] == plan.model)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(observation["raw_report_expires_at"].replace("Z", "+00:00")):
        pytest.skip("Original reports expired under approved P1M retention")
    records = []
    for request, control in zip(plan.requests, observation["controls"]):
        path = ROOT / control["evidence"]["path"]
        if not path.is_file():
            pytest.skip("Private P1M raw evidence is not distributed with this skill")
        response_path = path.with_name(path.name.replace("observation.json", "response.txt"))
        metadata = json.loads((path.parent / ".report-retention.json").read_bytes())
        assert metadata["period"] == "P1M" and metadata["expires_at"] == observation["raw_report_expires_at"]
        assert not path.is_symlink() and not response_path.is_symlink()
        record_raw, response_raw = path.read_bytes(), response_path.read_bytes()
        assert hashlib.sha256(record_raw).hexdigest() == control["evidence"]["sha256"]
        assert hashlib.sha256(response_raw).hexdigest() == control["response_sha256"]
        owners = {r["path"]: r["sha256"] for r in metadata["owned_files"]}
        assert owners[path.name] == control["evidence"]["sha256"] and owners[response_path.name] == control["response_sha256"]
        original = json.loads(record_raw)
        assert original["body_sha256"] == request.body_sha256 == control["request_sha256"]
        records.append({"case_id": request.case_id, "request_body_sha256": request.body_sha256,
                        "status_code": original["status_code"], "response_raw": response_raw,
                        "response_sha256_before_redaction": control["response_sha256"]})
    assert prefill.evaluate_prefill_plan(plan, records)["pass"]


def test_mutable_views_cannot_change_plan(plan):
    plan.requests[0].body["model"] = "wrong"
    plan.requests[0].expectation["assembled_json"]["answer"] = 17
    plan.snapshot["execution_target"]["request_model_id"] = "wrong"
    assert plan.requests[0].body["model"] == plan.model
    assert plan.requests[0].expectation["assembled_json"]["answer"] == 2
    with pytest.raises(FrozenInstanceError): plan.suite_id = "wrong"


@pytest.mark.parametrize("kwargs", [{"runs": True}, {"runs": 1.0}, {"runs": 2}, {"runs": 0}, {"job_type": "quick_load"},
    {"job_type": "cache_suite"}, {"suite_id": "unknown"}, {"endpoint": "https://api.anthropic.com/v1/messages?x=1"}])
def test_selection_changes_rejected(snapshot, model, kwargs):
    with pytest.raises(ValueError):
        prefill.build_prefill_plan(snapshot, **{"suite_id": prefill.suite_id(model), **kwargs})


@pytest.mark.parametrize("mutation", ["snapshot_digest", "source", "provider", "model_alias", "model_other", "form", "route",
    "contract", "embedded_policy", "disabled", "body", "expectation", "order", "manifest", "capability", "constraint", "missing_suite", "exclude", "override"])
def test_even_rehashed_snapshot_cannot_cross_suite_or_source(snapshot, model, mutation):
    value = copy.deepcopy(snapshot)
    interface = value["interface"]
    parameter = value["parameter_test_binding"]
    suite = parameter["fixed_case_suites"][prefill.suite_id(model)]
    if mutation == "snapshot_digest": value["snapshot_digest"] = "0" * 64
    elif mutation == "source": value["source_id"] = "aws_bedrock"
    elif mutation == "provider": value["execution_target"]["provider_id"] = "relay"
    elif mutation == "model_alias": value["execution_target"]["request_model_id"] = model.rsplit("-", 1)[0]
    elif mutation == "model_other": value["execution_target"]["request_model_id"] = "claude-fable-5"
    elif mutation == "form": value["api_form"] = "openai_chat_completions"
    elif mutation == "route": value["execution_target"]["route_profile"] = "compat"
    elif mutation == "contract": value["reference_contract_id"] = "claude_openai_compat"
    elif mutation == "embedded_policy": interface["test_bindings"][0]["parameter_test_enabled"] = False
    elif mutation == "disabled": interface["enabled"] = 1
    elif mutation == "body": suite["case_definitions"][0]["body"]["max_tokens"] = 2048.0
    elif mutation == "expectation": suite["case_definitions"][0]["expectation"]["assembled_json"]["answer"] = True
    elif mutation == "order": suite["case_definitions"].reverse()
    elif mutation == "manifest": interface["source_conflicts"]["fixed_prefill_suite_manifest_20260908"]["request_cap"] = 4
    elif mutation == "capability": interface["parameter_capabilities"]["messages"] = {"state": "unsupported"}
    elif mutation == "constraint": interface["parameter_constraints"]["thinking.type"]["allowed_values"] = ["enabled"]
    elif mutation == "missing_suite": del parameter["fixed_case_suites"][prefill.suite_id(model)]
    elif mutation == "exclude": parameter["excluded_test_profiles"] = [prefill.suite_id(model) + "/stop"]
    else: parameter.setdefault("expectations", {})[prefill.suite_id(model) + "/prefill"] = "unsupported"
    if mutation != "snapshot_digest": redigest(value)
    with pytest.raises((ValueError, RuntimeError)):
        prefill.build_prefill_plan(value, suite_id=prefill.suite_id(model))


@pytest.mark.parametrize("mutation", ["baseline_wrong_json", "baseline_wrong_model", "usage_bool", "output_cap", "thinking_block",
    "wrong_stop", "wrong_stop_text", "negative_200", "wrong_error_param", "wrong_error_message", "timeout", "hash", "partial"])
def test_failure_boundaries_never_reuse_historical_success(plan, records, mutation):
    if mutation == "baseline_wrong_json": change(records, 0, lambda p: p["content"][0].update(text='WRONG"}'))
    elif mutation == "baseline_wrong_model": change(records, 0, lambda p: p.update(model="claude-fable-5"))
    elif mutation == "usage_bool": change(records, 0, lambda p: p["usage"].update(input_tokens=True))
    elif mutation == "output_cap": change(records, 0, lambda p: p["usage"].update(output_tokens=2049))
    elif mutation == "thinking_block": change(records, 0, lambda p: p["content"].append({"type": "thinking", "thinking": "hidden"}))
    elif mutation == "wrong_stop": change(records, 1, lambda p: p.update(stop_reason="end_turn"))
    elif mutation == "wrong_stop_text": change(records, 1, lambda p: p["content"][0].update(text="LEAD_CUT_HERE"))
    elif mutation == "negative_200": records[2]["status_code"] = 200
    elif mutation == "wrong_error_param": change(records, 2, lambda p: p["error"].update(param="model"))
    elif mutation == "wrong_error_message": change(records, 2, lambda p: p["error"].update(message="Invalid model"))
    elif mutation == "timeout": records[0]["failure_type"] = "TimeoutError"
    elif mutation == "hash": records[0]["response_sha256_before_redaction"] = "0" * 64
    else: records.pop()
    result = prefill.evaluate_prefill_plan(plan, records)
    assert not result["pass"]
    if mutation.startswith("baseline_"):
        assert not any(row["pass"] for row in result["case_results"])
