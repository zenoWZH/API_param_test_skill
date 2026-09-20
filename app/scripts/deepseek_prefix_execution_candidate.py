"""Reviewed eight-request beta prefix execution delta, separate from stable Chat.

This author helper never writes artifacts or dispatches requests. The migration
owner must opt in after reviewing its exact source-local changes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from lib.deepseek_prefix_live import APPROVAL_CODE, EXECUTION_FLAGS, TARGETS

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "references/deepseek_prefix_execution_candidate_20260907.json"
APPROVAL_KEY = "approved_deepseek_prefix_execution_20260907"
GUIDE = "https://api-docs.deepseek.com/guides/chat_prefix_completion/"
API_REFERENCE = "https://api-docs.deepseek.com/api/create-chat-completion/"
CASE_IDS = (
    "deepseek_beta_prefix_matrix_false_control",
    "deepseek_beta_prefix_matrix_true_json",
    "deepseek_beta_prefix_matrix_stop",
    "deepseek_beta_prefix_matrix_invalid_type",
)
PREFIX = '{"answer":2,"tail":"PREFILLED_'
CONTROL_JSON = {"answer": 2, "tail": "LEAD_CUT_HERE_OMEGA"}
TARGET_JSON = {"answer": 2, "tail": "PREFILLED_LEAD_CUT_HERE_OMEGA"}
STOP_LITERAL = "CUT_HERE"
PROMPT = 'Reply with exactly this complete JSON object: {"answer":2,"tail":"LEAD_CUT_HERE_OMEGA"}. Do not add text or Markdown.'
SCOPE = "official_documented_beta_prefix_bounded_parameter_execution_live_unverified"


def matrix_cases(model: str) -> list[dict]:
    if model not in {target[0] for target in TARGETS.values()}:
        raise ValueError("unreviewed beta model")
    base = {"model": model, "max_tokens": 512, "stream": False,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": PROMPT},
                         {"role": "assistant", "content": PREFIX, "prefix": True}]}
    result = []
    for index, case_id in enumerate(CASE_IDS):
        body = copy.deepcopy(base)
        if index == 0:
            body["messages"][-1]["prefix"] = False
        elif index == 2:
            body["stop"] = [STOP_LITERAL]
        elif index == 3:
            body["messages"][-1]["prefix"] = "not-a-boolean"
        result.append({"case_id": case_id, "body": body,
                       "expectation": "unsupported" if index == 3 else "supported",
                       "target_parameter": "stop" if index == 2 else "messages[].prefix",
                       "assertions": ["exact native returned model", "complete native usage",
                                      "controlled beta prefix matrix semantics"],
                       "live_status": "live_unverified"})
    return result


def expected_candidate() -> dict:
    return {
        "schema_version": 1, "approval_code": APPROVAL_CODE,
        "source_id": "deepseek", "api_form": "deepseek_beta_chat_prefix",
        "api_version": "beta", "method": "POST",
        "url": "https://api.deepseek.com/beta/chat/completions",
        "request_count": 8, "request_cap": 8, "concurrency": 1, "retries": 0,
        "api_total_cost_cap": None, "report_retention": "P1M",
        "max_output_tokens": 512, "thinking": "disabled",
        "official_references": [GUIDE, API_REFERENCE],
        "retrieved_at": "2026-09-07",
        "evidence_scope": "beta only; stable Chat and FIM observations are not beta proof",
        "live_status": "live_unverified", "pressure_test_enabled": False,
        "targets": [{"interface_id": iid, "model_id": model, "contract_id": cid,
                     "case_definitions": matrix_cases(model)}
                    for iid, (model, cid) in TARGETS.items()],
    }


def candidate_sha256() -> str:
    return hashlib.sha256(canonical_bytes(expected_candidate())).hexdigest()


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def apply_deepseek_prefix_execution(catalog: dict, bindings: dict, additions: dict, *, candidate=None) -> dict:
    if additions.get(APPROVAL_KEY) != APPROVAL_CODE:
        return {"applied": False}
    candidate = candidate if candidate is not None else json.loads(CANDIDATE.read_text())
    if canonical_bytes(candidate) != canonical_bytes(expected_candidate()):
        raise ValueError("beta prefix candidate differs from the reviewed eight-request package")
    if not isinstance(additions.get("provenance_retrieved_at_overrides", {}), dict):
        raise ValueError("provenance overrides must be an object")
    c, b = copy.deepcopy(catalog), copy.deepcopy(bindings)
    provenance = {}
    for target in candidate["targets"]:
        iid, model, cid = (target[name] for name in ("interface_id", "model_id", "contract_id"))
        pid, slug = iid.split("#")
        profile = c["profiles"][pid]
        interface = profile["interfaces"][slug]
        contract = c["contracts"][cid]
        parameter_id, policy_id = "parameter/" + cid, "interface/" + iid.replace("#", "/")
        parameter, policy = b[parameter_id], b[policy_id]
        if profile.get("source_id") != "deepseek" or profile.get("request_model_ids") != [model] or profile.get("lifecycle") != "active":
            raise ValueError("beta prefix profile changed source, exact model or lifecycle")
        if (interface.get("api_form") != candidate["api_form"] or
                interface.get("routing_mode") != "vendor_direct" or
                interface.get("transport_adapter_id") != "deepseek-beta-chat-prefix" or
                interface.get("contract_ids") != [cid] or interface.get("default_contract_id") != cid or
                interface.get("request_model_ids") != [model] or
                interface.get("default_api_version") != "beta" or
                interface.get("api_versions", {}).get("beta", {}).get("path_template") != "/beta/chat/completions"):
            raise ValueError("beta prefix interface changed exact route or contract")
        if (contract.get("source_id") != "deepseek" or contract.get("source_ids") != ["deepseek"] or
                contract.get("api_form") != candidate["api_form"] or contract.get("family_id") != "deepseek" or
                parameter.get("source_id") != "deepseek" or parameter.get("contract_id") != cid or
                policy.get("source_id") != "deepseek" or policy.get("interface_id") != iid or
                policy.get("reference_contract_ids") != [cid] or policy.get("default_reference_contract_id") != cid):
            raise ValueError("beta prefix contract or bindings changed exact source")
        for record in (interface, parameter, policy):
            if (any(record.get(flag) is not False for flag in EXECUTION_FLAGS) or
                    record.get("disabled_reason") != "beta_prefix_live_execution_package_not_resolved" or
                    record.get("pressure_test_enabled") is not False or record.get("test_binding_status") != "required"):
                raise ValueError("beta prefix reference execution precondition changed")
            record.update(dict.fromkeys(EXECUTION_FLAGS, True))
            record.pop("disabled_reason")
            record["certification_scope"] = SCOPE
        for record in (interface, contract):
            record["official_sources"] = list(dict.fromkeys([*record.get("official_sources", []), GUIDE]))
        for record in (parameter, policy):
            record["api_version"] = "beta"
            old_ids = record.get("test_cases")
            if not isinstance(old_ids, list) or any(case in old_ids for case in CASE_IDS):
                raise ValueError("beta prefix matrix cases already present or malformed")
            record["test_cases"] = [*old_ids, *CASE_IDS]
        if not isinstance(parameter.get("case_definitions"), list):
            raise ValueError("beta prefix old case definitions missing")
        parameter["case_definitions"].extend(copy.deepcopy(target["case_definitions"]))
        parameter.setdefault("parameter_coverage", {}).update({
            "messages[].prefix": "Four-case, source-local beta matrix includes false control, prefix continuation, stop and invalid type; live effect remains unverified until execution.",
            "stop": "Exact CUT_HERE literal compared on the same beta JSON prefix input; reconstructed JSON ignores harmless formatting whitespace; no stable endpoint evidence transfer.",
        })
        for name in ("profile/" + pid, "interface/" + iid.replace("#", "/"), "contract/" + cid,
                     "test-binding/" + parameter_id, "test-binding/" + policy_id):
            provenance[name] = "2026-09-07"
    catalog.clear()
    catalog.update(c)
    bindings.clear()
    bindings.update(b)
    additions.setdefault("provenance_retrieved_at_overrides", {}).update(provenance)
    return {"applied": True, "interfaces_enabled": 2, "bindings_enabled": 4,
            "new_cases": 8, "request_cap": 8, "pressure_test_enabled": False,
            "candidate_sha256": candidate_sha256(), "live_status": "live_unverified"}
