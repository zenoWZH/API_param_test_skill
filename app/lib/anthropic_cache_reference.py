"""Versioned Anthropic cache plans from the shared source-authored fixtures.

Preview descriptions contain no nonce or outbound body. A job creates exactly
two nonces once. New plans freeze three hit-expectation controls. Frozen v1
plans retain their original five requests and numeric qualification rules.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import re
import secrets

from .deepseek_beta_reference import canonical_bytes, decode_beta_response as decode_json
from .model_profile_catalog import binding_from_database_snapshot

API_FORM, CONTRACT_ID, TRANSPORT = "anthropic_messages", "claude_native_messages", "claude_messages"
ENDPOINT, COUNT_ENDPOINT = "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"
REFERENCE_SHA256 = "0cf2474faa7c6fbad90bf6232e1262f9515ab8a18cf178a22e2c1026b1473711"
FACT_SHA256 = "da03c240af26f8ea0f6df6c54287116b26b396b12ca25ac2d45992f4e5cfef70"
PINS = {'claude-haiku-4-5-20251001': ('122b7894b444716791998b3660f585310d66f073aa14f7750694a575d38d6077', '04abf7dc9500b1a368b25882c32437f049479fea1ed9763a40df67418520ad5c'), 'claude-opus-4-5-20251101': ('eeefe5b7968041c654d8964bc4e7e3b82254b50eedfeb2809107329c9d91bb3c', '3d2cc379d1cffabaf982d86019f62d562c472793e29efb888fe42322caed910b'), 'claude-opus-4-6': ('f6794ca75fed4b3f136bfad881958d8fd97afe30a376a065c608a1b5a79e9fc2', '01a6d8d981dea8fa18d9984f076e45767a4b210453632f01e579ddb56799c4dc'), 'claude-opus-4-7': ('7aab5630f4ea4970cf8c4a6b22d654cdb08ecfdc974c7855b2741f4e52af54da', '439e6a3a82c666fb1b2df495cb066ba3e9898869f1f94a90d9b4f987c8451a1b'), 'claude-opus-4-8': ('923c4109733ff489c092c35077bac0ac5246c1d1d5a3b3d3a2ed3bc79a9e4776', 'b36e7a0f957586e05da44326d044a7c3a7c24797b0a0840faafa8a13074adfde'), 'claude-opus-5': ('1ccb2efa856b1b00e8267e53969eb29aebcfa3356e837b3b8f3f9c3012633c63', 'c321e45c56a73b76eb8a539733aa599bf3613c1eefb51940d4be17919b71f09f'), 'claude-sonnet-4-5-20250929': ('9ec11204779e00ad67c3c24d615597e2adc09db762b7c08beaa6eb51d477cb31', 'c9988d96434924767fead4458a38c7ced2829480c71e9cd27b0ca25f1de28e49'), 'claude-sonnet-4-6': ('a86afa9cfdefe3261d6e18f86d303cb7abf42bd20a91ff5db8c584053bc0c9f7', '86be6bb725db5de69f4db388be728645f4cd5ff3c70c97e9e5c4b3f83e960013'), 'claude-sonnet-5': ('ca0ec36aaaba3b731e63c12c92570d88528224ba2de1b6de1c4ef1941cf53aa0', 'f58f77139b545180b956a78755e7161f2d2bd4dc87bebf9df0cb7aebf2f61cb7')}
LABELS = ("count_cold", "count_negative", "cold", "repeat", "negative")
CURRENT_LABELS = LABELS[2:]
CURRENT_POLICY = {"id": "scenario_expectations_v1", "official_numeric_reference_required": False,
    "expected_hits": [False, True, False], "count_requests_required": False,
    "metric_population": "three controls only; not customer hit rate"}
MODELS = ("claude-haiku-4-5-20251001", "claude-opus-4-5-20251101", "claude-opus-4-6",
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-6", "claude-sonnet-5")
SUITE_IDS = tuple("anthropic_cache_" + model.replace("-", "_") + "_20260908" for model in MODELS)


def digest(value): return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require(ok, message):
    if not ok: raise ValueError(message)


def suite_id(model):
    require(model in MODELS, "Cache suite requires an exact approved Anthropic model")
    return SUITE_IDS[MODELS.index(model)]


def identity(model):
    suite_id(model)
    pid = "text/anthropic/claude/" + model
    return {"source_id": "anthropic", "profile_id": pid, "interface_id": pid + "#anthropic-messages-default",
            "contract_id": CONTRACT_ID, "api_form": API_FORM, "request_model_id": model}


def _matches(record, expected):
    return type(record) is dict and all(canonical_bytes(record.get(k)) == canonical_bytes(v) for k, v in expected.items())


def _suite(snapshot, selected):
    require(selected in SUITE_IDS, "Unknown fixed cache suite")
    model = MODELS[SUITE_IDS.index(selected)]
    scope = identity(model)
    pid, iid = scope["profile_id"], scope["interface_id"]
    binding = binding_from_database_snapshot(snapshot)
    require(_matches(snapshot, {"snapshot_schema_version": 1, "source_id": "anthropic", "profile_id": pid,
        "interface_id": iid, "api_form": API_FORM, "reference_contract_id": CONTRACT_ID,
        "parameter_test_binding_id": "parameter/" + CONTRACT_ID, "modality": "text", "family_id": "claude",
        "model_slug": model, "catalog_resolved": True}), "Cache snapshot crossed exact source/model/form")
    require(_matches(binding["execution_target"], {"provider_id": "anthropic_official", "request_model_id": model,
        "route_profile": "vendor_direct", "api_form": API_FORM})
        and binding["execution_target"].get("transport", TRANSPORT) == TRANSPORT,
        "Cache execution target is not the exact official Anthropic route")
    profile, interface, contract = snapshot["profile"], snapshot["interface"], snapshot["reference_contract"]
    aliases = [model.rsplit("-", 1)[0], model] if model in (MODELS[0], MODELS[1], MODELS[6]) else [model]
    require(_matches(profile, {"source_id": "anthropic", "profile_id": pid, "canonical_model_id": "claude/" + model,
        "request_model_ids": aliases, "lifecycle": "active", "profile_state": "executable"})
        and _matches(interface, {"source_id": "anthropic", "interface_id": iid, "profile_id": pid,
            "routing_mode": "vendor_direct", "api_form": API_FORM, "transport_adapter_id": TRANSPORT,
            "default_contract_id": CONTRACT_ID, "contract_ids": [CONTRACT_ID]})
        and interface.get("request_model_ids", aliases) == aliases
        and interface.get("default_api_version", API_VERSION) == API_VERSION
        and interface.get("api_versions", {API_VERSION: {"path_template": "/v1/messages"}}) == {API_VERSION: {"path_template": "/v1/messages"}}
        and _matches(contract, {"source_id": "anthropic", "source_ids": ["anthropic"], "contract_id": CONTRACT_ID,
            "api_form": API_FORM, "family_id": "claude", "routing_mode": "vendor_direct"}),
        "Cache Profile, Interface or Contract changed")
    policy, parameter = snapshot["test_binding"], snapshot["parameter_test_binding"]
    policy_id = "interface/" + iid.replace("#", "/")
    require(_matches(policy, {"source_id": "anthropic", "test_binding_id": policy_id, "interface_id": iid,
        "extension_type": "model_test_policy", "reference_contract_ids": [CONTRACT_ID], "default_reference_contract_id": CONTRACT_ID})
        and _matches(parameter, {"source_id": "anthropic", "test_binding_id": "parameter/" + CONTRACT_ID,
            "contract_id": CONTRACT_ID, "extension_type": "parameter"}), "Cache Binding owner changed")
    embedded = [row for row in interface.get("test_bindings", []) if row.get("test_binding_id") == policy_id]
    require(len(embedded) == 1 and canonical_bytes(embedded[0]) == canonical_bytes(policy), "Cache embedded policy changed")
    for record in (profile, interface, contract, policy, parameter):
        require(not record.get("disabled_reason") and not any(k in record and record[k] is not True
            for k in ("enabled", "executable", "runner_enabled", "parameter_test_enabled")), "An execution gate closes this cache suite")
    caps = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
    require(all(caps.get(k, {}).get("state") == "supported" for k in
        ("model", "messages", "system", "thinking.type", "max_tokens", "stream", "cache_control")),
        "A required cache request field is not supported")
    constraints = {**contract.get("parameter_constraints", {}), **interface.get("parameter_constraints", {})}
    limits = constraints.get("max_tokens", {})
    require("disabled" in constraints.get("thinking.type", {}).get("allowed_values", [])
        and limits.get("inclusive_minimum", 1) <= 2048 <= limits.get("inclusive_maximum", 2048),
        "Fixed cache generation violates its source constraints")
    suite = parameter.get("fixed_case_suites", {}).get(selected)
    manifest = interface.get("source_conflicts", {}).get("fixed_cache_suite_manifest_20260908")
    require(model in PINS and type(suite) is dict and digest(suite) == PINS[model][0]
        and type(manifest) is dict and digest(manifest) == PINS[model][1], "Cache suite or Interface manifest differs from the shared pin")
    require(_matches(manifest, {**scope, "suite_id": selected, "request_cap": 5, "suite_definition_sha256": PINS[model][0],
        "suite_artifact": {"path": "references/anthropic_cache_fixed_suites_20260908.json", "sha256": REFERENCE_SHA256}}),
        "Cache manifest does not belong to this exact selection")
    cases = suite["case_definitions"]
    require([row["case_id"] for row in cases] == [selected + "/" + label for label in LABELS]
            and not set(row["case_id"] for row in cases) & set(parameter.get("test_cases", [])),
            "Cache cases changed order or entered the generic matrix")
    for index, case in enumerate(cases):
        endpoint = COUNT_ENDPOINT if index < 2 else ENDPOINT
        require(case.get("kind") == ("count" if index < 2 else "generation")
                and case.get("endpoint_constraint") == {"scheme": "https", "host": "api.anthropic.com",
                    "path": "/v1/messages/count_tokens" if index < 2 else "/v1/messages", "url": endpoint}
                and case.get("generation_fields") == ({} if index < 2 else {"max_tokens": 2048, "stream": False}),
                "Cache case kind, endpoint constraint or generation fields changed")
    for record in (policy, parameter):
        require(not set(record.get("excluded_test_profiles", [])) & {row["case_id"] for row in cases}, "A cache case is excluded")
        def check(value):
            if type(value) is dict:
                for key, item in value.items():
                    expectations = {row["case_id"]: row["expectation"] for row in cases}
                    require(key not in expectations or canonical_bytes(item) == canonical_bytes(expectations[key]),
                            "A Binding overrides a frozen cache case expectation")
                    required = {"model", "messages", "system", "thinking", "thinking.type", "max_tokens", "stream", "cache_control"}
                    require(not (key in required and (item == "unsupported" or type(item) is dict and item.get("state") == "unsupported")),
                            "A Binding override excludes the fixed cache suite")
                    check(item)
        for key in ("expectations", "model_expectations", "parameter_expectations", "default_parameter_expectations",
                    "model_parameter_expectations", "default_expectations"):
            check(record.get(key, {}))
    return suite


def _render(suite, nonces):
    policy = suite["nonce_policy"]
    require(type(nonces) is dict and set(nonces) == {"positive", "negative"}
        and all(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) and value != policy["placeholder"]
                and hashlib.sha256(value.encode()).hexdigest() not in policy["historical_nonce_sha256"] for value in nonces.values())
        and nonces["positive"] != nonces["negative"], "Cache job requires two fresh distinct hexadecimal nonces")
    result = []
    for case in suite["case_definitions"]:
        body = copy.deepcopy(suite["request_template"])
        body["system"] = nonces[case["nonce_role"]] + body["system"][32:]
        body.update(copy.deepcopy(case["generation_fields"]))
        result.append({key: copy.deepcopy(case[key]) for key in ("case_id", "label", "kind", "expectation")})
        result[-1].update(endpoint=case["endpoint_constraint"]["url"], body=body, body_sha256=digest(body))
    return result


def _render_current(suite, nonces):
    rows = _render(suite, nonces)[2:]
    for row in rows:
        row["expectation"] = {"kind": "cache_hit_expectation", "expected_hit": row["label"] == "repeat"}
    return rows


@dataclass(frozen=True)
class CacheRequest:
    case_id: str
    label: str
    kind: str
    endpoint: str
    expectation: dict
    _body_bytes: bytes | None = field(repr=False)
    target_parameter: str = "cache_control"

    @property
    def body_bytes(self):
        require(self._body_bytes is not None, "Preview cache requests have no frozen outbound body")
        return self._body_bytes
    @property
    def body(self): return decode_json(self.body_bytes)
    @property
    def body_sha256(self): return hashlib.sha256(self.body_bytes).hexdigest()


@dataclass(frozen=True)
class CacheSuiteDescription:
    _snapshot_bytes: bytes = field(repr=False)
    suite_id: str
    endpoint: str = ENDPOINT

    @property
    def snapshot(self): return decode_json(self._snapshot_bytes)
    @property
    def model(self): return MODELS[SUITE_IDS.index(self.suite_id)]
    @property
    def schema_version(self): return 2
    @property
    def request_cap(self): return 3 if self.schema_version == 2 else 5
    @property
    def requests(self):
        return tuple(CacheRequest(row["case_id"], row["label"], row["kind"], row["endpoint_constraint"]["url"],
                     {"kind": "cache_hit_expectation", "expected_hit": row["label"] == "repeat"}, None)
                     for row in _suite(self.snapshot, self.suite_id)["case_definitions"][2:])


@dataclass(frozen=True)
class CachePlan(CacheSuiteDescription):
    _plan_bytes: bytes = field(default=b"", repr=False)

    @property
    def frozen_payload(self): return decode_json(self._plan_bytes)
    @property
    def schema_version(self):
        value = self.frozen_payload.get("schema_version")
        require(type(value) is int and value in (1, 2), "Unknown frozen cache plan schema")
        return value
    @property
    def plan_digest(self): return self.frozen_payload["plan_digest"]
    @property
    def requests(self):
        value = self.frozen_payload
        suite = _suite(self.snapshot, self.suite_id)
        version = self.schema_version
        expected = {"schema_version": version, "suite_id": self.suite_id, "snapshot_digest": self.snapshot["snapshot_digest"],
            "nonces": value.get("nonces"), "requests": (_render_current if version == 2 else _render)(suite, value.get("nonces")), "request_cap": self.request_cap,
            "report_retention": "P1M", "nonce_generation_time": "job_creation_only"}
        if version == 2:
            expected["cache_evaluation_policy"] = copy.deepcopy(CURRENT_POLICY)
        expected["plan_digest"] = digest(expected)
        require(canonical_bytes(value) == canonical_bytes(expected) and self.endpoint == ENDPOINT,
                "Frozen cache job bodies, nonces, source snapshot or plan digest changed")
        return tuple(CacheRequest(row["case_id"], row["label"], row["kind"], row["endpoint"], copy.deepcopy(row["expectation"]),
                                  canonical_bytes(row["body"])) for row in value["requests"])


def build_cache_plan(snapshot, *, suite_id, endpoint=ENDPOINT, runs=1, job_type="param_test",
                     frozen_plan=None, create_runtime_nonce=False, nonce_factory=None):
    require(suite_id in SUITE_IDS and endpoint == ENDPOINT and type(runs) is int and runs == 1 and job_type == "param_test",
            "Cache requires one explicit parameter job on the exact endpoint")
    suite = _suite(snapshot, suite_id)
    require(type(create_runtime_nonce) is bool and not (create_runtime_nonce and frozen_plan is not None),
            "Cache plan cannot both create and replace frozen nonces")
    if create_runtime_nonce:
        factory = nonce_factory or secrets.token_hex
        nonces = {"positive": factory(16), "negative": factory(16)}
        value = {"schema_version": 2, "suite_id": suite_id, "snapshot_digest": snapshot["snapshot_digest"],
            "nonces": nonces, "requests": _render_current(suite, nonces), "request_cap": 3, "report_retention": "P1M",
            "nonce_generation_time": "job_creation_only", "cache_evaluation_policy": copy.deepcopy(CURRENT_POLICY)}
        value["plan_digest"] = digest(value)
    elif frozen_plan is not None:
        require(type(frozen_plan) is dict, "Frozen cache plan must be an object")
        value = copy.deepcopy(frozen_plan)
    else:
        return CacheSuiteDescription(canonical_bytes(snapshot), suite_id, endpoint)
    plan = CachePlan(canonical_bytes(snapshot), suite_id, endpoint, canonical_bytes(value))
    plan.requests
    return plan


def _native(payload, model, suite):
    require(_matches(payload, {"type": "message", "role": "assistant", "model": model, "stop_reason": "end_turn"})
            and type(payload.get("id")) is str and payload["id"].strip() and not payload.get("error") and not payload.get("refusal"),
            "Cache generation lacks a complete native identity or is refused")
    blocks, usage = payload.get("content"), payload.get("usage")
    require(type(blocks) is list and blocks and all(type(row) is dict and row.get("type") == "text"
        and type(row.get("text")) is str for row in blocks), "Cache generation includes missing, tool, thinking or refusal content")
    text = "".join(row["text"] for row in blocks)
    require(text.strip() and type(usage) is dict and all(type(usage.get(k)) is int and usage[k] >= 0
        for k in suite["native_response_contract"]["required_usage_fields"])
        and 0 < usage["output_tokens"] <= 2048, "Cache generation text or required usage is invalid")
    total = sum(usage[k] for k in suite["cache_effect_requirements"]["full_input_fields"])
    require(0 < total <= 16384, "Cache full native input lies outside qualification bounds")
    geo = usage.get("inference_geo")
    require(geo is None or type(geo) is str and geo.strip(), "Invalid reported inference geography")
    split = usage.get("cache_creation")
    fields = suite["native_response_contract"]["optional_creation_split_fields"]
    require(split is None or type(split) is dict and all(type(split.get(k)) is int and split[k] >= 0 for k in fields)
        and sum(split[k] for k in fields) == usage["cache_creation_input_tokens"], "Invalid native cache creation breakdown")
    if suite["fixture_kind"] == "library":
        negated = re.search(r"\b(?:not|never|cannot|can['’]t|isn['’]t|aren['’]t|unable)\b[^.!?;\n]{0,48}\b(?:8|eight)\b", text, re.I)
        conflicting_hours = re.search(r"\b(?:[0-7]|9|\d{2,}|one|two|three|four|five|six|seven|nine|ten|eleven|twelve)\s+hours?\b", text, re.I)
        require(bool(re.search(r"\b(?:8|eight)\b", text, re.I)) and not negated and not conflicting_hours,
                "Library fixture did not unambiguously answer eight hours")
    return {"native_usage": copy.deepcopy(usage), "total_input_tokens": total, "returned_model": model,
        "cached_read_tokens": usage["cache_read_input_tokens"], "cache_creation_tokens": usage["cache_creation_input_tokens"],
        "literal_ack_diagnostic": text.strip() == "CACHE_PROBE_OK", "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def _evaluate_legacy_cache_plan(plan, records):
    require(type(plan) is CachePlan and type(records) is list and len(records) <= 5, "Cache requires a frozen plan and at most five ordered records")
    requests, suite = plan.requests, _suite(plan.snapshot, plan.suite_id)
    results = []
    for request, record in zip(requests, records):
        require(_matches(record, {"case_id": request.case_id, "request_body_sha256": request.body_sha256,
            "request_kind": request.kind, "request_endpoint": request.endpoint, "fixed_plan_digest": plan.plan_digest}),
            "Cache observation crossed its frozen request or plan")
        value = {"case_id": request.case_id, "kind": request.kind, "label": request.label,
            "target_parameter": "cache_control", "expectation": copy.deepcopy(request.expectation),
            "status_code": record.get("status_code"), "response_valid": False, "pass": False,
            "token_exact_proof": False}
        try:
            require(type(record.get("status_code")) is int and record["status_code"] == 200
                and record.get("response_complete") is True and record.get("client_entered") is True
                and not record.get("failure_type") and not record.get("interrupted"), "Cache exchange did not complete successfully")
            raw = record["response_raw"].encode() if type(record["response_raw"]) is str else record["response_raw"]
            require(type(raw) is bytes and record.get("response_sha256_before_redaction") == hashlib.sha256(raw).hexdigest(),
                    "Cache retained response bytes changed")
            payload = decode_json(raw)
            if request.kind == "count":
                count = payload.get("input_tokens")
                valid = not payload.get("error") and type(count) is int and count >= 0
                q = suite["count_qualification"]
                value.update(response_valid=valid, input_estimate=count if valid else None, precision="official_estimate",
                    qualifies_for_generation=bool(valid and q["minimum_input_tokens"] <= count <= q["maximum_input_tokens"]))
                value["pass"] = value["qualifies_for_generation"]
            else:
                value.update(_native(payload, plan.model, suite), response_valid=True, **{"pass": True})
                value["pass"] = (value["cached_read_tokens"] == 0 and value["cache_creation_tokens"] >= suite["minimum_prefix_tokens"]
                    if request.label == "cold" else value["cached_read_tokens"] > 0 if request.label == "repeat" else value["cached_read_tokens"] == 0)
                if request.label == "repeat" and len(results) >= 3 and results[2].get("response_valid"):
                    cold = results[2]
                    value["cold_repeat_input_equal"] = value["total_input_tokens"] == cold["total_input_tokens"]
                    value["read_lte_cold_creation"] = value["cached_read_tokens"] <= cold["cache_creation_tokens"]
                    value["pass"] &= value["cold_repeat_input_equal"] and value["read_lte_cold_creation"]
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            value["validation_error"] = type(exc).__name__
        results.append(value)
    complete = len(records) == 5
    geos = [row.get("native_usage", {}).get("inference_geo") for row in results[2:]]
    region_consistent = len(geos) == 3 and len(set(geos)) == 1
    if complete and not region_consistent:
        results[-1]["pass"] = False
        results[-1]["reported_inference_geo_consistent"] = False
    effect = complete and all(row["pass"] for row in results) and region_consistent
    return {"suite_id": plan.suite_id, **identity(plan.model), "snapshot_digest": plan.snapshot["snapshot_digest"],
        "fixed_plan_digest": plan.plan_digest, "request_cap": 5, "requests_recorded": len(records), "complete": complete,
        "pass": bool(effect), "cache_effect_verified": bool(effect), "case_results": results,
        "count_requests_recorded": sum(row["kind"] == "count" for row in results),
        "generation_requests_recorded": sum(row["kind"] == "generation" for row in results),
        "count_requests_sent": sum(record.get("client_entered") is True and request.kind == "count"
                                   for request, record in zip(requests, records)),
        "generation_requests_sent": sum(record.get("client_entered") is True and request.kind == "generation"
                                        for request, record in zip(requests, records)),
        "official_input_estimates": [row.get("input_estimate") for row in results[:2]],
        "count_precision": "official_estimate", "minimum_prefix_tokens": suite["minimum_prefix_tokens"],
        "reported_inference_geo_consistent": region_consistent, "identity_probe_requests": 0,
        "full_parameter_matrix_verified": False, "token_exact_proof": False, "physical_region_verified": False,
        "provider_ttl_expiry_verified": False, "historical_pass_reused": False, "report_retention": "P1M"}


def evaluate_cache_plan(plan, records):
    require(type(plan) is CachePlan and type(records) is list and len(records) <= plan.request_cap,
            "Cache requires a frozen plan and bounded ordered records")
    if plan.schema_version == 1:
        return _evaluate_legacy_cache_plan(plan, records)
    from .cache_acceptance import validate_scenario
    requests, suite = plan.requests, _suite(plan.snapshot, plan.suite_id)
    results = []
    for request, record in zip(requests, records):
        require(_matches(record, {"case_id": request.case_id, "request_body_sha256": request.body_sha256,
            "request_kind": request.kind, "request_endpoint": request.endpoint, "fixed_plan_digest": plan.plan_digest}),
            "Cache observation crossed its frozen request or plan")
        value = {"case_id": request.case_id, "kind": request.kind, "label": request.label,
            "target_parameter": "cache_control", "expectation": copy.deepcopy(request.expectation),
            "status_code": record.get("status_code"), "response_valid": False, "pass": False,
            "token_exact_proof": False}
        try:
            require(type(record.get("status_code")) is int and record["status_code"] == 200
                and record.get("response_complete") is True and record.get("client_entered") is True
                and not record.get("failure_type") and not record.get("interrupted"), "Cache exchange did not complete successfully")
            raw = record["response_raw"].encode() if type(record["response_raw"]) is str else record["response_raw"]
            require(type(raw) is bytes and record.get("response_sha256_before_redaction") == hashlib.sha256(raw).hexdigest(),
                    "Cache retained response bytes changed")
            value.update(_native(decode_json(raw), plan.model, suite), response_valid=True)
            value["pass"] = (value["cached_read_tokens"] > 0) == request.expectation["expected_hit"]
            if request.label == "repeat" and results and results[0].get("response_valid"):
                value["cold_repeat_input_equal"] = value["total_input_tokens"] == results[0]["total_input_tokens"]
                value["pass"] &= value["cold_repeat_input_equal"]
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            value["validation_error"] = type(exc).__name__
        results.append(value)
    complete = len(records) == 3
    geos = [row.get("native_usage", {}).get("inference_geo") for row in results]
    region_consistent = complete and len(set(geos)) == 1
    acceptance = {"status": "insufficient_evidence", "reason": "three complete native controls required"}
    if complete and all(row["response_valid"] for row in results):
        bodies = [request.body for request in requests]
        prefixes = [body.pop("system") for body in bodies]
        scope = identity(plan.model)
        acceptance = validate_scenario(cache_reads=[row["cached_read_tokens"] for row in results],
            input_tokens=[row["total_input_tokens"] for row in results], prefixes=prefixes, settings=bodies,
            bindings=[scope] * 3, expected_binding=scope)
        if not region_consistent:
            results[-1]["pass"] = False
            results[-1]["reported_inference_geo_consistent"] = False
    effect = complete and all(row["pass"] for row in results) and region_consistent and acceptance["status"] == "pass"
    return {"suite_id": plan.suite_id, **identity(plan.model), "snapshot_digest": plan.snapshot["snapshot_digest"],
        "fixed_plan_digest": plan.plan_digest, "plan_schema_version": 2, "cache_evaluation_policy": copy.deepcopy(CURRENT_POLICY),
        "request_cap": 3, "requests_recorded": len(records), "complete": complete,
        "pass": bool(effect), "cache_effect_verified": bool(effect), "case_results": results,
        "count_requests_recorded": 0, "generation_requests_recorded": len(records), "count_requests_sent": 0,
        "generation_requests_sent": sum(record.get("client_entered") is True for record in records),
        "official_input_estimates": [], "count_precision": "not_requested", "minimum_prefix_tokens": None,
        "official_numeric_reference_required": False, "scenario_acceptance": acceptance,
        "reported_inference_geo_consistent": region_consistent, "identity_probe_requests": 0,
        "full_parameter_matrix_verified": False, "token_exact_proof": False, "physical_region_verified": False,
        "provider_ttl_expiry_verified": False, "historical_pass_reused": False, "report_retention": "P1M"}


def next_cache_request(plan, records):
    observed = evaluate_cache_plan(plan, records)
    for row in observed["case_results"]:
        if not row["response_valid"] or row["kind"] == "count" and not row["pass"]:
            return None
    return plan.requests[len(records)] if len(records) < plan.request_cap else None


def generation_token_audit_results(plan, results):
    """Exclude only the two exact frozen count wires from generation audits."""
    require(type(plan) is CachePlan, "Count audit exclusion requires the frozen cache plan")
    counts = {request.case_id: request for request in plan.requests if request.kind == "count"}
    selected = []
    for row in results:
        request = counts.get(row.get("name")) if type(row) is dict else None
        excluded = (request is not None and row.get("profile") == request.case_id
            and row.get("request_kind") == request.kind
            and row.get("request_endpoint") in (COUNT_ENDPOINT, "/v1/messages/count_tokens")
            and digest(row.get("request_body")) == request.body_sha256)
        if not excluded:
            selected.append(row)
    return selected
