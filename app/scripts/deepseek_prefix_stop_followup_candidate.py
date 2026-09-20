"""Append four source-local beta stop controls without replacing first evidence."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from lib.deepseek_prefix_live import APPROVAL_CODE, EXECUTION_FLAGS, TARGETS
from scripts.deepseek_prefix_execution_candidate import (
    APPROVAL_KEY as FIRST_APPROVAL_KEY, CASE_IDS as FIRST_CASE_IDS, SCOPE,
    candidate_sha256 as first_candidate_sha256, canonical_bytes, matrix_cases,
)

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "references/deepseek_prefix_stop_followup_candidate_20260907.json"
APPROVAL_KEY = "approved_deepseek_prefix_stop_followup_20260907"
CASE_IDS = ("deepseek_beta_prefix_stop_followup_baseline", "deepseek_beta_prefix_stop_followup_enabled")
PREFIX = '{"answer":2,"tail":"'
TARGET_JSON = {"answer": 2, "tail": "LEAD_CUT_HERE_OMEGA"}
STOP_LITERAL = "CUT_HERE"
PROMPT = 'Reply with exactly this complete JSON object: {"answer":2,"tail":"LEAD_CUT_HERE_OMEGA"}. Do not add text or Markdown.'
FIRST_CANDIDATE_SHA256 = "bdde6206b30c5d84a252e66c1f9ee427988803b9476795fe4ce1a0b152762faf"


def stop_cases(model: str) -> list[dict]:
    if model not in {target[0] for target in TARGETS.values()}:
        raise ValueError("unreviewed beta model")
    base = {"model": model, "max_tokens": 512, "stream": False, "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": PROMPT},
                         {"role": "assistant", "content": PREFIX, "prefix": True}]}
    return [{"case_id": case_id, "body": {**copy.deepcopy(base), **({"stop": [STOP_LITERAL]} if index else {})},
             "expectation": "supported", "target_parameter": "stop",
             "assertions": ["exact native returned model and complete usage", "actual unstopped literal positive control",
                            "stop boundary with original native returned-prefix representation"],
             "live_status": "live_unverified"} for index, case_id in enumerate(CASE_IDS)]


def expected_candidate() -> dict:
    return {"schema_version": 1, "approval_code": APPROVAL_CODE, "source_id": "deepseek",
            "api_form": "deepseek_beta_chat_prefix", "api_version": "beta", "method": "POST",
            "url": "https://api.deepseek.com/beta/chat/completions", "request_count": 4, "request_cap": 4,
            "concurrency": 1, "retries": 0, "max_output_tokens": 512, "thinking": "disabled",
            "api_total_cost_cap": None, "report_retention": "P1M", "pressure_test_enabled": False,
            "previous_candidate_sha256": FIRST_CANDIDATE_SHA256,
            "previous_evidence_preserved": True, "live_status": "live_unverified",
            "purpose": "Fill only the beta stop gap with a nonconflicting prefix; do not repeat false or invalid-type cases.",
            "official_references": ["https://api-docs.deepseek.com/guides/chat_prefix_completion/",
                                    "https://api-docs.deepseek.com/api/create-chat-completion/"],
            "targets": [{"interface_id": iid, "model_id": model, "contract_id": cid, "case_definitions": stop_cases(model)}
                        for iid, (model, cid) in TARGETS.items()]}


def candidate_sha256() -> str:
    return hashlib.sha256(json.dumps(expected_candidate(), ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def apply_deepseek_prefix_stop_followup(catalog: dict, bindings: dict, additions: dict, *, candidate=None) -> dict:
    if additions.get(APPROVAL_KEY) != APPROVAL_CODE:
        return {"applied": False}
    if additions.get(FIRST_APPROVAL_KEY) != APPROVAL_CODE or first_candidate_sha256() != FIRST_CANDIDATE_SHA256:
        raise ValueError("the reviewed first beta execution delta must precede the stop follow-up")
    candidate = candidate if candidate is not None else json.loads(CANDIDATE.read_text())
    if canonical_bytes(candidate) != canonical_bytes(expected_candidate()):
        raise ValueError("beta stop candidate differs from the reviewed four requests")
    if not isinstance(additions.get("provenance_retrieved_at_overrides", {}), dict):
        raise ValueError("provenance overrides must be an object")
    c, b = copy.deepcopy(catalog), copy.deepcopy(bindings)
    provenance = {}
    for iid, (model, cid) in TARGETS.items():
        pid, slug = iid.split("#")
        profile, contract = c["profiles"][pid], c["contracts"][cid]
        interface = profile["interfaces"][slug]
        parameter_id, policy_id = "parameter/" + cid, "interface/" + iid.replace("#", "/")
        parameter, policy = b[parameter_id], b[policy_id]
        if (profile.get("source_id") != "deepseek" or profile.get("request_model_ids") != [model] or
                profile.get("lifecycle") != "active" or interface.get("api_form") != "deepseek_beta_chat_prefix" or
                interface.get("routing_mode") != "vendor_direct" or interface.get("transport_adapter_id") != "deepseek-beta-chat-prefix" or
                interface.get("request_model_ids") != [model] or interface.get("contract_ids") != [cid] or
                interface.get("default_contract_id") != cid or interface.get("default_api_version") != "beta" or
                interface.get("api_versions", {}).get("beta", {}).get("path_template") != "/beta/chat/completions"):
            raise ValueError("beta stop target identity or path changed")
        if (contract.get("source_id") != "deepseek" or contract.get("source_ids") != ["deepseek"] or
                contract.get("api_form") != "deepseek_beta_chat_prefix" or
                contract.get("certification_scope") != "official_documented_beta_prefix_live_unverified" or
                parameter.get("source_id") != "deepseek" or parameter.get("contract_id") != cid or
                policy.get("source_id") != "deepseek" or policy.get("interface_id") != iid or
                policy.get("reference_contract_ids") != [cid] or policy.get("default_reference_contract_id") != cid):
            raise ValueError("beta stop contract or bindings crossed source-local scope")
        for record in (interface, parameter, policy):
            if (any(record.get(flag) is not True for flag in EXECUTION_FLAGS) or record.get("disabled_reason") or
                    record.get("pressure_test_enabled") is not False or record.get("certification_scope") != SCOPE):
                raise ValueError("beta stop execution scope changed")
        capabilities = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
        if not isinstance(capabilities.get("stop"), dict) or capabilities["stop"].get("state") != "supported":
            raise ValueError("beta stop capability is not currently supported")
        original_cases = parameter.get("case_definitions", [])
        for case in matrix_cases(model):
            if canonical_bytes([item for item in original_cases if item.get("case_id") == case["case_id"]]) != canonical_bytes([case]):
                raise ValueError("first beta matrix case definition changed")
        for record in (parameter, policy):
            if record.get("api_version") != "beta" or not all(case in record.get("test_cases", []) for case in FIRST_CASE_IDS):
                raise ValueError("first beta matrix binding changed")
            if any(case in record["test_cases"] for case in CASE_IDS):
                raise ValueError("beta stop follow-up already exists")
            record["test_cases"].extend(CASE_IDS)
        parameter["case_definitions"].extend(stop_cases(model))
        parameter.setdefault("parameter_coverage", {})["stop"] += (
            " Separate four-request follow-up uses a nonconflicting JSON prefix; actual baseline must contain CUT_HERE before effect can pass."
        )
        for name in ("test-binding/" + parameter_id, "test-binding/" + policy_id):
            provenance[name] = "2026-09-07"
    catalog.clear()
    catalog.update(c)
    bindings.clear()
    bindings.update(b)
    additions.setdefault("provenance_retrieved_at_overrides", {}).update(provenance)
    return {"applied": True, "new_cases": 4, "request_cap": 4,
            "execution_gates_changed": False, "previous_cases_preserved": True,
            "candidate_sha256": candidate_sha256(), "live_status": "live_unverified"}
