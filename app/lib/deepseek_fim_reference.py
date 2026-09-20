"""Immutable, source-scoped FIM controls from the shared MPDB Binding."""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
import hashlib
import re

from .deepseek_beta_reference import canonical_bytes, decode_beta_response as decode_fim_json
from .model_profile_catalog import binding_from_database_snapshot

SUITE_ID = "deepseek_fim_causal_20260908"
PROFILE_ID = "text/deepseek/deepseek/deepseek-v4-pro-0813"
INTERFACE_ID = PROFILE_ID + "#openai_fim_completions_beta-default"
CONTRACT_ID = "deepseek_v4_pro_0813_fim_beta"
API_FORM = "openai_fim_completions_beta"
TRANSPORT = "fim_completions"
ENDPOINT = "https://api.deepseek.com/beta/completions"
FACT_SHA256 = "eafc3064a2180610d474f77f02dafe34034b227dc120d514b0da33ad4096e057"
SUITE_SHA256 = "b26ea2c81acd2663ed4fd045c74fef8a33ed5210ec53ffac131e1dcc2b131fd3"
MANIFEST_SHA256 = "986ebac29844ddf9d6d79fe1904b82b76a7a64d5e3a7d1125c68aadb6d493867"
POLICY_BINDING_ID = "interface/" + INTERFACE_ID.replace("#", "/")
CASE_IDS = tuple("deepseek_fim_causal_" + label for label in (
    "suffix_42", "suffix_73", "echo_42", "suffix_invalid_type", "stop_baseline", "stop_enabled", "echo_false", "echo_true"))


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _exact(record, expected, label):
    if not isinstance(record, dict) or any(canonical_bytes(record.get(k)) != canonical_bytes(v) for k, v in expected.items()):
        raise ValueError(label + " differs from the exact FIM source scope")


def _suite(snapshot):
    if not SUITE_SHA256:
        raise ValueError("The shared FIM suite has not been installed and pinned")
    binding = binding_from_database_snapshot(snapshot)
    _exact(snapshot, {"snapshot_schema_version": 1, "source_id": "deepseek", "profile_id": PROFILE_ID,
        "interface_id": INTERFACE_ID, "api_form": API_FORM, "reference_contract_id": CONTRACT_ID,
        "parameter_test_binding_id": "parameter/" + CONTRACT_ID, "modality": "text", "family_id": "deepseek",
        "model_slug": "deepseek-v4-pro-0813", "catalog_resolved": True}, "Snapshot")
    _exact(binding["execution_target"], {"provider_id": "deepseek_official", "request_model_id": "deepseek-v4-pro",
        "route_profile": "vendor_direct", "api_form": API_FORM}, "Execution target")
    if binding["execution_target"].get("transport", TRANSPORT) != TRANSPORT:
        raise ValueError("FIM execution target transport conflict")
    _exact(snapshot["profile"], {"source_id": "deepseek", "profile_id": PROFILE_ID,
        "canonical_model_id": "deepseek/deepseek-v4-pro-0813", "request_model_ids": ["deepseek-v4-pro"],
        "lifecycle": "active", "profile_state": "executable"}, "Profile")
    interface, contract = snapshot["interface"], snapshot["reference_contract"]
    _exact(interface, {"source_id": "deepseek", "interface_id": INTERFACE_ID,
        "routing_mode": "vendor_direct", "api_form": API_FORM, "transport_adapter_id": TRANSPORT,
        "default_contract_id": CONTRACT_ID, "contract_ids": [CONTRACT_ID]}, "Interface")
    _exact(contract, {"source_id": "deepseek", "source_ids": ["deepseek"], "contract_id": CONTRACT_ID,
        "api_form": API_FORM, "family_id": "deepseek", "routing_mode": "vendor_direct"}, "Contract")
    if (interface.get("default_api_version", "beta") != "beta"
            or interface.get("api_versions", {"beta": {"stability": "beta", "path_template": "/beta/completions"}})
               != {"beta": {"stability": "beta", "path_template": "/beta/completions"}}):
        raise ValueError("FIM Interface path or version conflict")
    policy, parameter = snapshot["test_binding"], snapshot["parameter_test_binding"]
    _exact(policy, {"source_id": "deepseek", "test_binding_id": POLICY_BINDING_ID,
        "interface_id": INTERFACE_ID, "extension_type": "model_test_policy",
        "reference_contract_ids": [CONTRACT_ID], "default_reference_contract_id": CONTRACT_ID}, "Policy Binding")
    _exact(parameter, {"source_id": "deepseek", "test_binding_id": "parameter/" + CONTRACT_ID,
        "contract_id": CONTRACT_ID, "extension_type": "parameter"}, "Parameter Binding")
    embedded = interface.get("test_bindings")
    if not isinstance(embedded, list):
        raise ValueError("FIM Interface lacks its immutable policy Binding")
    matching = [row for row in embedded if isinstance(row, dict) and row.get("test_binding_id") == POLICY_BINDING_ID]
    if len(matching) != 1 or canonical_bytes(matching[0]) != canonical_bytes(policy):
        raise ValueError("FIM Interface and snapshot policy Binding conflict")
    for item in (snapshot["profile"], interface, contract, snapshot["test_binding"], snapshot["parameter_test_binding"]):
        if item.get("disabled_reason") or any(k in item and item[k] is not True
                for k in ("enabled", "executable", "runner_enabled", "parameter_test_enabled")):
            raise ValueError("An execution gate closes the fixed FIM suite")
        if "pressure_test_enabled" in item and item["pressure_test_enabled"] is not False:
            raise ValueError("Fixed FIM scope does not include pressure execution")
    contract_caps, interface_caps = contract.get("parameter_capabilities", {}), interface.get("parameter_capabilities", {})
    if not isinstance(contract_caps, dict) or not isinstance(interface_caps, dict):
        raise ValueError("FIM parameter capabilities must be objects")
    caps = {**contract_caps, **interface_caps}
    for name in ("model", "prompt", "suffix", "echo", "stop", "stream", "max_tokens", "temperature"):
        if not isinstance(caps.get(name), dict) or caps[name].get("state") != "supported":
            raise ValueError("A required FIM field is unavailable")
    suites = parameter.get("fixed_case_suites", {})
    if not isinstance(suites, dict):
        raise ValueError("FIM fixed suites must be an object")
    suite = suites.get(SUITE_ID)
    if not isinstance(suite, dict) or digest(suite) != SUITE_SHA256:
        raise ValueError("Fixed FIM suite differs from the installed source manifest")
    conflicts = interface.get("source_conflicts", {})
    if not isinstance(conflicts, dict):
        raise ValueError("FIM Interface observations must be an object")
    manifest = conflicts.get("fixed_fim_suite_manifest_20260908")
    if digest(manifest) != MANIFEST_SHA256:
        raise ValueError("Installed FIM suite manifest integrity pin mismatch")
    _exact(manifest, {"suite_id": SUITE_ID, "suite_definition_sha256": SUITE_SHA256,
        "interface_id": INTERFACE_ID, "source_id": "deepseek", "request_cap": 8,
        "generic_runner_dispatch": False, "selection_mode": "explicit_only"}, "Installed suite manifest")
    _exact(suite, {"suite_id": SUITE_ID, "request_cap": 8, "source_id": "deepseek", "profile_id": PROFILE_ID,
        "interface_id": INTERFACE_ID, "contract_id": CONTRACT_ID, "api_form": API_FORM,
        "api_version": "beta", "request_model_id": "deepseek-v4-pro", "selection_mode": "explicit_only",
        "generic_runner_dispatch": False, "max_tokens": 512, "stream": False, "retries": 0,
        "extra_identity_requests": 0, "approval_leaf": "R7-APP-PARITY-DEEPSEEK-0813"}, "Suite")
    _exact(suite["facts_artifact"], {"path": "references/deepseek_fim_followup_facts_20260907.json",
        "sha256": FACT_SHA256}, "Source facts")
    _exact(suite["endpoint_constraint"], {"scheme": "https", "host": "api.deepseek.com", "path": "/beta/completions",
        "url": ENDPOINT}, "Endpoint")
    cases = suite["case_definitions"]
    if [r.get("case_id") for r in cases] != list(CASE_IDS):
        raise ValueError("Fixed FIM case order or coverage changed")
    for row in cases:
        if (digest(row["body"]) != row["request_body_sha256"]
                or digest({k: v for k, v in row.items() if k != "case_definition_sha256"}) != row["case_definition_sha256"]):
            raise ValueError("Fixed FIM body digest mismatch")
    expectations = {row["case_id"]: row["expectation"] for row in cases}
    parameters = {row["target_parameter"] for row in cases}
    def check_overrides(value, *, parameter_scope=False):
        if not isinstance(value, dict):
            raise ValueError("FIM expectation overrides must be an object")
        for key, child in value.items():
            if key in expectations and canonical_bytes(child) != canonical_bytes(expectations[key]):
                raise ValueError("Binding overrides a fixed FIM case expectation")
            if parameter_scope and key in parameters:
                raise ValueError("Binding overrides fixed FIM parameter interpretation")
            if isinstance(child, dict) and key not in expectations:
                check_overrides(child, parameter_scope=parameter_scope)
    for record in (policy, parameter):
        excluded = record.get("excluded_test_profiles", [])
        if (not isinstance(excluded, list) or not all(isinstance(cid, str) for cid in excluded)
                or any(cid in excluded for cid in CASE_IDS)):
            raise ValueError("A fixed FIM case is excluded or exclusion policy is malformed")
        for name in ("expectations", "default_expectations", "model_expectations"):
            check_overrides(record.get(name, {}))
        for name in ("parameter_expectations", "default_parameter_expectations", "model_parameter_expectations"):
            check_overrides(record.get(name, {}), parameter_scope=True)
    return suite


@dataclass(frozen=True)
class FimRequest:
    case_id: str
    expectation: dict
    target_parameter: str
    _bytes: bytes = field(repr=False)

    @property
    def body(self): return decode_fim_json(self._bytes)
    @property
    def body_bytes(self): return self._bytes
    @property
    def body_sha256(self): return hashlib.sha256(self._bytes).hexdigest()


@dataclass(frozen=True)
class FimPlan:
    _snapshot_bytes: bytes = field(repr=False)
    endpoint: str = ENDPOINT

    @property
    def snapshot(self): return decode_fim_json(self._snapshot_bytes)
    @property
    def requests(self):
        if self.endpoint != ENDPOINT: raise ValueError("FIM endpoint changed")
        return tuple(FimRequest(row["case_id"], copy.deepcopy(row["expectation"]), row["target_parameter"],
            canonical_bytes(row["body"])) for row in _suite(self.snapshot)["case_definitions"])
    @property
    def request_cap(self): return 8


def build_fim_plan(snapshot, *, suite_id, endpoint=ENDPOINT, runs=1, job_type="param_test"):
    if suite_id != SUITE_ID or endpoint != ENDPOINT or type(runs) is not int or runs != 1 or job_type != "param_test":
        raise ValueError("FIM requires explicit selection of the eight-case single-run parameter suite")
    _suite(snapshot)
    return FimPlan(canonical_bytes(snapshot), endpoint)


def _records(plan, records):
    if type(plan) is not FimPlan:
        raise ValueError("FIM records require the immutable source-authored plan")
    requests = plan.requests
    if not isinstance(records, list) or len(records) > 8:
        raise ValueError("Invalid FIM dispatch record count")
    for item, row in zip(requests, records):
        _exact(row, {"case_id": item.case_id, "request_body_sha256": item.body_sha256}, "Record")
        if "status_code" not in row or row.get("status_code") is not None and (
                type(row["status_code"]) is not int or not 100 <= row["status_code"] <= 599):
            raise ValueError("FIM HTTP status must be an integer")
        if not isinstance(row.get("response_raw"), (str, bytes)):
            raise ValueError("Original FIM response bytes are required")
    return requests


def next_fim_request(plan, records):
    requests = _records(plan, records)
    return requests[len(records)] if len(records) < 8 else None


def _native(payload, body):
    _exact(payload, {"model": "deepseek-v4-pro", "object": "text_completion"}, "Native FIM response")
    if (not isinstance(payload.get("id"), str) or not payload["id"].strip()
            or payload["id"] != payload["id"].strip()
            or type(payload.get("created")) is not int or payload["created"] < 0 or "error" in payload):
        raise ValueError("Invalid native FIM identity")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Native FIM requires one choice")
    choice = choices[0]
    _exact(choice, {"index": 0, "finish_reason": "stop"}, "FIM choice")
    if not isinstance(choice.get("text"), str) or not choice["text"]:
        raise ValueError("Missing FIM completion")
    usage = payload.get("usage")
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    if (not isinstance(usage, dict) or any(type(usage.get(f)) is not int or usage[f] < 0 for f in fields)
            or usage["prompt_tokens"] <= 0 or not 0 < usage["completion_tokens"] <= body["max_tokens"]
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
            or usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"] != usage["prompt_tokens"]):
        raise ValueError("Native FIM usage is incomplete or inconsistent")
    return choice["text"], copy.deepcopy(usage)


def _only_changed(a, b, field):
    left, right = copy.deepcopy(a), copy.deepcopy(b)
    old, new = left.pop(field, None), right.pop(field, None)
    return canonical_bytes(left) == canonical_bytes(right) and canonical_bytes(old) != canonical_bytes(new)


def evaluate_fim_plan(plan, records):
    requests = _records(plan, records)
    results, states = [], {}
    bodies = {r.case_id: r.body for r in requests}
    for item, record in zip(requests, records):
        row = {"case_id": item.case_id, "target_parameter": item.target_parameter,
               "expectation": copy.deepcopy(item.expectation), "status_code": record["status_code"],
               "response_valid": False, "pass": False}
        state = {}
        try:
            if (record.get("failure_type") or record.get("interrupted")
                    or "response_complete" in record and record["response_complete"] is not True
                    or "client_entered" in record and record["client_entered"] is not True):
                raise ValueError("FIM transport did not complete")
            if "interrupted" in record and type(record["interrupted"]) is not bool:
                raise ValueError("Invalid FIM interruption marker")
            raw_bytes = record["response_raw"] if isinstance(record["response_raw"], bytes) else record["response_raw"].encode("utf-8")
            if ("response_sha256_before_redaction" in record
                    and record["response_sha256_before_redaction"] != hashlib.sha256(raw_bytes).hexdigest()):
                raise ValueError("FIM original response hash mismatch")
            payload = decode_fim_json(record["response_raw"])
            state["payload"] = payload
            expectation, body = item.expectation, item.body
            kind = expectation["kind"]
            if kind != "attributed_rejection" and record["status_code"] == 200:
                text, usage = _native(payload, body)
                state.update(text=text, valid=True)
                row.update(response_valid=True, usage=usage, returned_model_id=payload["model"])
                if kind == "fim_ast_literal":
                    actual = body["prompt"] + text + body.get("suffix", "")
                    # The fixed string fixture opens its quote in prompt and
                    # closes it in suffix. Its middle is the literal contents.
                    middle = expectation["value"] if isinstance(expectation["value"], str) else repr(expectation["value"])
                    expected = body["prompt"] + middle + body.get("suffix", "")
                    row["pass"] = ast.dump(ast.parse(actual), include_attributes=False) == ast.dump(ast.parse(expected), include_attributes=False)
                    row["ast_literal_verified"] = row["pass"]
                else:
                    prior = states.get(expectation["positive_control"], {})
                    good_body = bodies[expectation["positive_control"]]
                    if kind == "stop_prefix":
                        marker = expectation["stop"]
                        row["pass"] = bool(prior.get("pass") and _only_changed(good_body, body, "stop")
                            and marker in prior["text"] and text == expectation["expected_text"]
                            and text == prior["text"].split(marker, 1)[0])
                    elif kind == "echo_prompt_plus_completion":
                        row["pass"] = bool(prior.get("pass") and _only_changed(good_body, body, "echo")
                            and "suffix" not in good_body and "suffix" not in body and text == body["prompt"] + prior["text"])
            elif kind == "attributed_rejection" and record["status_code"] in expectation["expected_statuses"]:
                error = payload.get("error")
                if (not isinstance(error, dict)
                        or "choices" in payload and (type(payload["choices"]) is not list or payload["choices"])
                        or "model" in payload and payload["model"] != "deepseek-v4-pro"):
                    raise ValueError("FIM rejection lacks native error")
                prior = states.get(expectation["positive_control"], {})
                message = error.get("message", "")
                if not isinstance(message, str) or error.get("type") not in expectation["error_types"]:
                    raise ValueError("FIM error identity is incomplete")
                field = "echo" if item.target_parameter == "echo+suffix" else "suffix"
                if "message_equals" in expectation:
                    attributed = message == expectation["message_equals"] and error.get("param") in (None, "echo", "suffix")
                else:
                    attributed = ((error.get("param") == "suffix" or error.get("param") is None
                        and bool(re.search(r"\bsuffix\b", message, re.I)))
                        and bool(re.search(r"\b(?:string|type|deserialize)\b", message, re.I))
                        and not re.search(r"\b(?:invalid|unknown)\s+(?:model|temperature|prompt|max_tokens)\b", message, re.I))
                row["pass"] = bool(prior.get("pass") and _only_changed(bodies[expectation["positive_control"]], body, field) and attributed)
                row["field_rejection_attributed"] = row["pass"]
        except (ValueError, TypeError, KeyError, SyntaxError, RecursionError) as exc:
            row["validation_error"] = type(exc).__name__
        state["pass"] = row["pass"]
        states[item.case_id] = state
        results.append(row)
    indexed = {row["case_id"]: row for row in results}
    ab = bool(all(indexed.get(cid, {}).get("pass") for cid in CASE_IDS[:2]) and _only_changed(bodies[CASE_IDS[0]], bodies[CASE_IDS[1]], "suffix"))
    return {"suite_id": SUITE_ID, "request_cap": 8, "requests_recorded": len(records), "complete": len(records) == 8,
        "pass": len(records) == 8 and all(row["pass"] for row in results) and ab,
        "case_results": results, "suffix_ab_effect_observed": ab,
        "stop_effect_observed": indexed.get(CASE_IDS[5], {}).get("pass") is True,
        "echo_effect_observed": indexed.get(CASE_IDS[7], {}).get("pass") is True,
        "source_id": "deepseek", "interface_id": INTERFACE_ID, "contract_id": CONTRACT_ID, "api_form": API_FORM,
        "snapshot_digest": plan.snapshot["snapshot_digest"], "identity_probe_requests": 0,
        "code_executed": False, "token_exact_proof": False, "full_parameter_matrix_verified": False}
