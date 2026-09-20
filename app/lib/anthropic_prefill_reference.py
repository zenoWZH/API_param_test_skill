"""Immutable Claude 4.5 prefill controls consumed from the shared MPDB."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import re

from .deepseek_beta_reference import canonical_bytes, decode_beta_response as decode_prefill_json
from .model_profile_catalog import binding_from_database_snapshot

API_FORM, CONTRACT_ID, TRANSPORT = "anthropic_messages", "claude_native_messages", "claude_messages"
ENDPOINT, API_VERSION = "https://api.anthropic.com/v1/messages", "2023-06-01"
FACT_SHA256 = "bb4e878b9537d0c7549e9167a50320fac25d6fcba463756830e407bcd8c0f6cc"
REFERENCE_SHA256 = "516d97124d29c952520fd413b1345d402b3914d51402b308a549d7dcbaa783f9"
PINS = {
    "claude-haiku-4-5-20251001": ("874f89358af51e8aa7b259bd10dcc66fd10b4f1fcc7f0c6a9d7f8cce0c0671dc", "4fbf7ec2911317970fb159b5ac0d9b3324e83110331e2885a8bea7c79de9e11a"),
    "claude-opus-4-5-20251101": ("93a44181cfbf82a96fafb50799731e5caf0fda65377430f1ae2c2ebc7e9c7986", "6272aa775b83112ad853173b0899ca702df22efab74379cb7c8d238101dfb3ea"),
    "claude-sonnet-4-5-20250929": ("1179b729ad1e3d01702e67fcc303d6fa402a93f35198845e76fb762bf4ba0480", "b66f769c2491750ba4fed7f1fb9b401c17a5700421aaf35be98213d9949e4aab"),
}
MODELS = tuple(PINS)
LABELS = ("prefill", "stop", "invalid_content_type")


def suite_id(model):
    if model not in PINS:
        raise ValueError("Prefill requires one of the three exact Claude 4.5 dated model IDs")
    return "anthropic_prefill_" + model.replace("-", "_") + "_20260908"


SUITE_IDS = tuple(suite_id(model) for model in MODELS)


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def identity(model):
    suite_id(model)
    pid = "text/anthropic/claude/" + model
    return {"source_id": "anthropic", "profile_id": pid, "interface_id": pid + "#anthropic-messages-default",
            "contract_id": CONTRACT_ID, "api_form": API_FORM, "request_model_id": model}


def _exact(value, expected, label):
    if type(value) is not dict or any(canonical_bytes(value.get(k)) != canonical_bytes(v) for k, v in expected.items()):
        raise ValueError(label + " differs from the exact Claude 4.5 prefill scope")


def _suite(snapshot, selected):
    if selected not in SUITE_IDS:
        raise ValueError("Unknown source-authored Claude prefill suite")
    model = MODELS[SUITE_IDS.index(selected)]
    scope = identity(model)
    pid, iid = scope["profile_id"], scope["interface_id"]
    binding = binding_from_database_snapshot(snapshot)
    _exact(snapshot, {"snapshot_schema_version": 1, "source_id": "anthropic", "profile_id": pid,
        "interface_id": iid, "api_form": API_FORM, "reference_contract_id": CONTRACT_ID,
        "parameter_test_binding_id": "parameter/" + CONTRACT_ID, "modality": "text", "family_id": "claude",
        "model_slug": model, "catalog_resolved": True}, "Snapshot")
    _exact(binding["execution_target"], {"provider_id": "anthropic_official", "request_model_id": model,
        "route_profile": "vendor_direct", "api_form": API_FORM}, "Execution target")
    if binding["execution_target"].get("transport", TRANSPORT) != TRANSPORT:
        raise ValueError("Prefill execution transport conflict")
    profile, interface, contract = snapshot["profile"], snapshot["interface"], snapshot["reference_contract"]
    aliases = [model.rsplit("-", 1)[0], model]
    _exact(profile, {"source_id": "anthropic", "profile_id": pid, "canonical_model_id": "claude/" + model,
        "request_model_ids": aliases, "lifecycle": "active", "profile_state": "executable"}, "Profile")
    _exact(interface, {"source_id": "anthropic", "interface_id": iid, "profile_id": pid,
        "routing_mode": "vendor_direct", "api_form": API_FORM, "transport_adapter_id": TRANSPORT,
        "default_contract_id": CONTRACT_ID, "contract_ids": [CONTRACT_ID]}, "Interface")
    _exact(contract, {"source_id": "anthropic", "source_ids": ["anthropic"], "contract_id": CONTRACT_ID,
        "api_form": API_FORM, "family_id": "claude", "routing_mode": "vendor_direct"}, "Contract")
    if (interface.get("default_api_version", API_VERSION) != API_VERSION
            or interface.get("api_versions", {API_VERSION: {"path_template": "/v1/messages"}})
               != {API_VERSION: {"path_template": "/v1/messages"}}):
        raise ValueError("Prefill Interface version or endpoint conflict")
    policy, parameter = snapshot["test_binding"], snapshot["parameter_test_binding"]
    policy_id = "interface/" + iid.replace("#", "/")
    _exact(policy, {"source_id": "anthropic", "test_binding_id": policy_id, "interface_id": iid,
        "extension_type": "model_test_policy", "reference_contract_ids": [CONTRACT_ID],
        "default_reference_contract_id": CONTRACT_ID}, "Policy Binding")
    _exact(parameter, {"source_id": "anthropic", "test_binding_id": "parameter/" + CONTRACT_ID,
        "contract_id": CONTRACT_ID, "extension_type": "parameter"}, "Parameter Binding")
    embedded = interface.get("test_bindings")
    if type(embedded) is not list:
        raise ValueError("Prefill Interface lacks its policy Binding")
    matching = [r for r in embedded if type(r) is dict and r.get("test_binding_id") == policy_id]
    if len(matching) != 1 or canonical_bytes(matching[0]) != canonical_bytes(policy):
        raise ValueError("Prefill embedded policy differs from snapshot")
    for item in (profile, interface, contract, policy, parameter):
        if item.get("disabled_reason") or any(k in item and item[k] is not True
                for k in ("enabled", "executable", "runner_enabled", "parameter_test_enabled")):
            raise ValueError("An execution gate closes the fixed prefill suite")
    caps = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
    if any(type(caps.get(k)) is not dict or caps[k].get("state") != "supported"
           for k in ("model", "messages", "thinking.type", "max_tokens", "stream", "stop_sequences")):
        raise ValueError("A required prefill parameter is unavailable")
    constraints = {**contract.get("parameter_constraints", {}), **interface.get("parameter_constraints", {})}
    cap = constraints.get("max_tokens", {})
    if ("disabled" not in constraints.get("thinking.type", {}).get("allowed_values", [])
            or not cap.get("inclusive_minimum", 1) <= 2048 <= cap.get("inclusive_maximum", 2048)):
        raise ValueError("Fixed prefill values violate current snapshot constraints")
    suites = parameter.get("fixed_case_suites", {})
    if type(suites) is not dict or type(suites.get(selected)) is not dict or digest(suites[selected]) != PINS[model][0]:
        raise ValueError("Prefill suite differs from its shared source pin")
    suite = suites[selected]
    conflicts = interface.get("source_conflicts", {})
    if type(conflicts) is not dict or digest(conflicts.get("fixed_prefill_suite_manifest_20260908")) != PINS[model][1]:
        raise ValueError("Prefill Interface manifest pin mismatch")
    manifest = conflicts["fixed_prefill_suite_manifest_20260908"]
    _exact(manifest, {**scope, "suite_id": selected, "suite_definition_sha256": PINS[model][0],
        "request_cap": 3, "selection_mode": "explicit_only", "generic_runner_dispatch": False,
        "suite_artifact": {"path": "references/anthropic_prefill_fixed_suites_20260908.json", "sha256": REFERENCE_SHA256}}, "Manifest")
    _exact(suite, {**scope, "suite_id": selected, "request_cap": 3, "api_version": API_VERSION,
        "selection_mode": "explicit_only", "generic_runner_dispatch": False, "max_tokens": 2048,
        "thinking": {"type": "disabled"}, "stream": False, "concurrency": 1, "retries": 0,
        "extra_identity_requests": 0, "tools_allowed": False, "pressure_test_enabled": False, "cache_test_enabled": False,
        "report_retention": "P1M", "approval_leaf": "R7-APP-PARITY-CLAUDE-SOURCE-SCOPED",
        "reference_approval_leaf": "R7-ALL-MODELS-PREFIX-COMPLETION-LIVE"}, "Suite")
    _exact(suite["facts_artifact"], {"path": "references/anthropic_prefill_facts_20260908.json", "sha256": FACT_SHA256}, "Facts")
    _exact(suite["endpoint_constraint"], {"scheme": "https", "host": "api.anthropic.com", "path": "/v1/messages", "url": ENDPOINT}, "Endpoint")
    cases = suite["case_definitions"]
    case_ids = [selected + "/" + label for label in LABELS]
    if [row.get("case_id") for row in cases] != case_ids or set(case_ids) & set(parameter.get("test_cases", [])):
        raise ValueError("Fixed prefill cases crossed the generic suite or changed order")
    for row in cases:
        if (digest(row["body"]) != row["request_body_sha256"]
                or digest({k: v for k, v in row.items() if k != "case_definition_sha256"}) != row["case_definition_sha256"]):
            raise ValueError("Fixed prefill case body or expectation hash mismatch")
    parameters = {r["target_parameter"] for r in cases}
    expectations = {r["case_id"]: r["expectation"] for r in cases}
    def check_overrides(value, parameter_scope=False):
        if type(value) is not dict:
            raise ValueError("Prefill expectation overrides must be objects")
        for key, child in value.items():
            if key in expectations and canonical_bytes(child) != canonical_bytes(expectations[key]):
                raise ValueError("Binding overrides fixed prefill case expectation")
            if parameter_scope and (key in parameters or key in {"messages", "stop_sequences"}):
                if child == "unsupported":
                    raise ValueError("Binding excludes a fixed prefill parameter")
            if type(child) is dict and key not in expectations:
                check_overrides(child, parameter_scope)
    for record in (policy, parameter):
        excluded = record.get("excluded_test_profiles", [])
        if type(excluded) is not list or any(type(cid) is not str for cid in excluded) or set(excluded) & set(case_ids):
            raise ValueError("Binding excludes a fixed prefill case")
        for name in ("expectations", "default_expectations", "model_expectations"):
            check_overrides(record.get(name, {}))
        for name in ("parameter_expectations", "default_parameter_expectations", "model_parameter_expectations"):
            check_overrides(record.get(name, {}), True)
    return suite


@dataclass(frozen=True)
class PrefillRequest:
    case_id: str
    expectation: dict
    target_parameter: str
    _bytes: bytes = field(repr=False)

    @property
    def body(self): return decode_prefill_json(self._bytes)
    @property
    def body_bytes(self): return self._bytes
    @property
    def body_sha256(self): return hashlib.sha256(self._bytes).hexdigest()


@dataclass(frozen=True)
class PrefillPlan:
    _snapshot_bytes: bytes = field(repr=False)
    suite_id: str
    endpoint: str = ENDPOINT

    @property
    def snapshot(self): return decode_prefill_json(self._snapshot_bytes)
    @property
    def model(self): return MODELS[SUITE_IDS.index(self.suite_id)]
    @property
    def requests(self):
        if self.endpoint != ENDPOINT: raise ValueError("Prefill endpoint changed")
        return tuple(PrefillRequest(row["case_id"], copy.deepcopy(row["expectation"]), row["target_parameter"],
            canonical_bytes(row["body"])) for row in _suite(self.snapshot, self.suite_id)["case_definitions"])
    @property
    def request_cap(self): return 3


def build_prefill_plan(snapshot, *, suite_id, endpoint=ENDPOINT, runs=1, job_type="param_test"):
    if suite_id not in SUITE_IDS or endpoint != ENDPOINT or type(runs) is not int or runs != 1 or job_type != "param_test":
        raise ValueError("Prefill requires explicit selection of one three-case single-run parameter suite")
    _suite(snapshot, suite_id)
    return PrefillPlan(canonical_bytes(snapshot), suite_id, endpoint)


def _records(plan, records):
    if type(plan) is not PrefillPlan or type(records) is not list or len(records) > 3:
        raise ValueError("Prefill requires the immutable plan and at most three ordered records")
    requests = plan.requests
    for item, row in zip(requests, records):
        _exact(row, {"case_id": item.case_id, "request_body_sha256": item.body_sha256}, "Record")
        if ("status_code" not in row or row["status_code"] is not None and
                (type(row["status_code"]) is not int or not 100 <= row["status_code"] <= 599)
                or not isinstance(row.get("response_raw"), (str, bytes))):
            raise ValueError("Prefill record lacks original status or response bytes")
    return requests


def next_prefill_request(plan, records):
    requests = _records(plan, records)
    return requests[len(records)] if len(records) < 3 else None


def _native(payload, model):
    _exact(payload, {"model": model, "type": "message", "role": "assistant"}, "Native response")
    blocks, usage = payload.get("content"), payload.get("usage")
    if (payload.get("error") is not None or type(payload.get("id")) is not str or not payload["id"].strip()
            or type(blocks) is not list or not blocks or any(type(b) is not dict or b.get("type") != "text"
                or type(b.get("text")) is not str for b in blocks)
            or type(usage) is not dict or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens"))
            or not 0 < usage["output_tokens"] <= 2048):
        raise ValueError("Incomplete native prefill identity, text or usage")
    for name in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if name in usage and (type(usage[name]) is not int or usage[name] < 0):
            raise ValueError("Invalid native prefill cache counter")
    text = "".join(b["text"] for b in blocks)
    if not text.strip(): raise ValueError("Empty prefill continuation")
    return text, copy.deepcopy(usage)


def _negative(payload):
    _exact(payload, {"type": "error"}, "Native error")
    error = payload.get("error")
    if type(error) is not dict or error.get("type") != "invalid_request_error" or type(error.get("message")) is not str:
        return False
    normalize = lambda s: s.lower().replace("[1]", ".1").replace("_", "") if type(s) is str else ""
    if any(normalize(error[key]) not in ("messages.1.content", "messages.1") for key in ("param", "field")
           if key in error and error[key] is not None):
        return False
    return bool(re.search(r"\bmessages(?:\[1\]|\.1)\.content\b", error["message"], re.I)
                and re.search(r"type|string|array|list|valid|expected", error["message"], re.I))


def evaluate_prefill_plan(plan, records):
    requests = _records(plan, records)
    results = []
    for item, record in zip(requests, records):
        result = {"case_id": item.case_id, "target_parameter": item.target_parameter,
                  "expectation": copy.deepcopy(item.expectation), "status_code": record["status_code"],
                  "response_valid": False, "pass": False}
        try:
            if (record.get("failure_type") or record.get("interrupted")
                    or "response_complete" in record and record["response_complete"] is not True
                    or "client_entered" in record and record["client_entered"] is not True
                    or "interrupted" in record and type(record["interrupted"]) is not bool):
                raise ValueError("Prefill transport did not complete")
            raw = record["response_raw"] if type(record["response_raw"]) is bytes else record["response_raw"].encode()
            if "response_sha256_before_redaction" in record and record["response_sha256_before_redaction"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("Prefill original response hash mismatch")
            payload = decode_prefill_json(raw)
            expect = item.expectation
            if expect["kind"] == "attributed_rejection":
                result["field_rejection_attributed"] = record["status_code"] in expect["expected_statuses"] and _negative(payload)
                result["pass"] = bool(results and results[0]["pass"] and result["field_rejection_attributed"])
            elif record["status_code"] == 200:
                text, usage = _native(payload, plan.model)
                result.update(response_valid=True, native_usage=usage, returned_model=payload["model"],
                              continuation_sha256=hashlib.sha256(text.encode()).hexdigest())
                stopped = payload.get("stop_reason") == expect["required_stop_reason"] and payload.get("stop_sequence") == expect["required_stop_sequence"]
                if expect["kind"] == "assistant_prefill_json":
                    assembled = decode_prefill_json(expect["prefix"] + text)
                    result["assembled_json_verified"] = canonical_bytes(assembled) == canonical_bytes(expect["assembled_json"])
                    result["pass"] = bool(stopped and result["assembled_json_verified"])
                else:
                    result["stop_prefix_verified"] = text == expect["expected_continuation"]
                    result["pass"] = bool(results and results[0]["pass"] and stopped and result["stop_prefix_verified"])
        except (ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
            result["validation_error"] = type(exc).__name__
        results.append(result)
    return {"suite_id": plan.suite_id, "request_cap": 3, "requests_recorded": len(records), "complete": len(records) == 3,
        "pass": len(records) == 3 and all(row["pass"] for row in results), "case_results": results,
        "source_id": "anthropic", **identity(plan.model), "snapshot_digest": plan.snapshot["snapshot_digest"],
        "identity_probe_requests": 0, "full_parameter_matrix_verified": False, "token_exact_proof": False,
        "thinking_switch_effect_verified": False, "cache_effect_verified": False,
        "stop_effect_observed": len(results) > 1 and results[1]["pass"], "historical_pass_reused": False}
