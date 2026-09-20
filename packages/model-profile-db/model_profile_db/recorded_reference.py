"""Explicit read-only views for two approved, closed observation Interfaces.

This module reads core Catalog records only. It never resolves an execution
binding, selects a default Interface, reads reports, or uses runtime overrides.
"""
from __future__ import annotations

import copy

from .catalog import Catalog

_TARGETS = {
    "gemini_vertex_generate_content": {
        "source": "google_vertex", "family": "gemini", "model": "gemini-3.5-flash",
        "slug": "gemini-generate-content-default", "form": "gemini_generate_content", "route": "google_vertex",
        "source_type": "cloud_managed", "authority": "managed_cloud", "profile_state": "identity_only",
        "status": "not_certified", "namespace": "designated_resource_safety_parameter_observations_20260908",
        "fact": "references/inferenceai_safety_parameter_facts_20260908.json",
        "sha256": "59f09de829c2db3761c635b5699e5a9d2e74e1a1e35527968abb5a72979f7afd",
    },
    "zai_general_glm_5_3_chat": {
        "source": "zhipu", "family": "glm", "model": "glm-5.3", "slug": "zai-general-chat",
        "form": "openai_chat_completions", "route": "vendor_direct", "source_type": "official_direct",
        "authority": "origin_vendor", "profile_state": "executable", "status": "required",
        "namespace": "bounded_parameter_observations_20260908", "fact": "references/zai_general_facts_20260908.json",
        "sha256": "dce710b792c487c1480831e987d944ae000d9ed81d758b5c99cf4b2f61a1da81",
    },
}
CLOSED_RECORDED_CONTRACTS = frozenset(_TARGETS)
_FLAGS = ("enabled", "executable", "runner_enabled", "parameter_test_enabled", "pressure_test_enabled")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (_merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict)
                       else copy.deepcopy(value))
    return result


def _observation_boundary(target, contract_id, profile_id, interface_id, interface, contract):
    conflicts = interface.get("source_conflicts") or {}
    entry = conflicts.get(target["namespace"])
    artifact = {"path": target["fact"], "sha256": target["sha256"]}
    if target["source"] == "google_vertex":
        exact = {"source_id": target["source"], "modality": "text", "family_id": target["family"],
            "model_slug": target["model"], "request_model_id": target["model"], "profile_id": profile_id,
            "interface_id": interface_id, "contract_id": contract_id, "api_form": target["form"]}
        labels = ("threshold_high", "threshold_low", "invalid_category", "invalid_threshold")
        _require(isinstance(entry, dict) and all(entry.get(key) == value for key, value in exact.items())
                 and entry.get("fact_artifact") == artifact and entry.get("execution_provider") == "inferenceai_gemini"
                 and entry.get("observation_authority") == "user_designated_vertex_resource_via_inferenceai"
                 and entry.get("google_reference_api_version") == "v1" and entry.get("supplier_wire_version") == "v1beta"
                 and entry.get("requests_sent") == 4 and all(entry.get(key) is False for key in
                     ("google_direct", "physical_upstream_verified", "filtering_effect_proven", "full_parameter_matrix_verified",
                      "ordinary_mpdb_gates_changed", "replaces_or_completes_explicit_content_probe", "method_default_inferred")),
                 "Closed Vertex view requires the exact designated-resource observation and its authority limits")
        observations = entry.get("observations")
        _require(isinstance(observations, list) and [row.get("case_id") for row in observations] ==
                 ["inferenceai_safety_parameter_" + label for label in labels]
                 and all(all(row.get(key) == value for key, value in exact.items())
                         and row.get("filtering_effect_proven") is False for row in observations),
                 "Closed Vertex observations crossed their exact model or source")
        return {"reference_label": "Vertex Gemini 3.5 Flash (InferenceAI designated resource observations)",
                **{key: copy.deepcopy(entry[key]) for key in ("execution_provider", "observation_authority",
                    "google_reference_api_version", "supplier_wire_version", "google_direct",
                    "physical_upstream_verified", "filtering_effect_proven")}, "fact_artifact": artifact}
    route = {"registered_vendor_source_id": "zhipu", "registered_route_source_id": "zai_general",
             "scheme": "https", "host": "api.z.ai", "path": "/api/paas/v4/chat/completions",
             "url": "https://api.z.ai/api/paas/v4/chat/completions", "allow_redirects": False}
    _require(all(record.get("observed_source_id") == "zai_general" and record.get("fact_artifact") == artifact
                 and all(record.get("reference_scope", {}).get(key) == value for key, value in route.items())
                 for record in (interface, contract))
             and all(interface.get(key) is False for key in _FLAGS)
             and all(contract.get(key) is False for key in _FLAGS)
             and conflicts.get("original_false_verdicts_preserved") is True
             and conflicts.get("response_format_invalid_accepted") is True
             and conflicts.get("plain_text_control_failed") is True
             and all(conflicts.get(key) is False for key in ("response_format_effect_verified",
                 "coding_equivalence_verified", "mainland_equivalence_verified", "cache_effect_verified", "thinking_effect_verified")),
             "Closed General view requires its exact General route and preserved failed controls")
    labels = ("format_text", "format_json", "format_invalid")
    _require(isinstance(entry, list) and [row.get("case_id") for row in entry] ==
             ["zai_general/glm-5.3/" + label for label in labels]
             and [row.get("original_verdict", {}).get("pass") for row in entry] == [False, True, False]
             and all(row.get("url") == route["url"] and row.get("request", {}).get("model") == target["model"]
                     and row.get("observation", {}).get("returned_model") == target["model"]
                     and row.get("observation", {}).get("native_envelope_verified") is True
                     and row.get("dispatch_allowed") is False and row.get("isolated_parameter_effect_verified") is False
                     for row in entry), "Closed General samples crossed the reviewed route, model or original outcomes")
    return {"reference_label": "GLM-5.3 Z.ai General (recorded reference)", "observed_source_id": "zai_general",
            "reference_scope": copy.deepcopy(interface["reference_scope"]), "fact_artifact": artifact,
            **{key: copy.deepcopy(conflicts[key]) for key in ("response_format_effect_verified",
                "coding_equivalence_verified", "mainland_equivalence_verified", "original_false_verdicts_preserved")}}


def recorded_reference_capability(catalog: Catalog, *, modality, family, model, reference_contract_id,
                                  api_form, route_profile, provider_override=None):
    """Return a closed core view for an explicit approved selection, else None.

    A recognized Contract with any mismatched selector raises instead of falling
    through to another source, alias, route, version, or executable Interface.
    """
    if reference_contract_id not in CLOSED_RECORDED_CONTRACTS:
        return None
    target = _TARGETS[reference_contract_id]
    _require(isinstance(catalog, Catalog), "Closed reference views require the core Catalog")
    _require(modality == "text" and family == target["family"] and model == target["model"]
             and api_form == target["form"] and route_profile == target["route"]
             and provider_override in (None, {}), "Closed reference selection requires the exact model, source Contract, route and API form without overrides")
    pid = "text/" + target["source"] + "/" + target["family"] + "/" + target["model"]
    iid = pid + "#" + target["slug"]
    source, profile = catalog.get_source(target["source"]), catalog.get_profile(pid)
    interface, contract = catalog.get_interface(iid), catalog.get_contract(reference_contract_id)
    _require(source.get("source_id") == target["source"] and source.get("source_type") == target["source_type"]
             and source.get("authority") == target["authority"]
             and profile.get("profile_id") == pid and profile.get("source_id") == target["source"]
             and profile.get("modality") == "text" and profile.get("family_id") == target["family"]
             and profile.get("model_slug") == target["model"] and profile.get("request_model_ids") == [target["model"]]
             and profile.get("lifecycle") == "active" and profile.get("profile_state") == target["profile_state"]
             and iid in profile.get("interface_ids", []) and interface.get("interface_id") == iid
             and interface.get("source_id") == target["source"] and interface.get("profile_id") == pid
             and interface.get("api_form") == target["form"] and interface.get("routing_mode") == target["route"]
             and interface.get("request_model_ids", profile["request_model_ids"]) == [target["model"]]
             and interface.get("enabled") is False and interface.get("executable") is False
             and interface.get("test_binding_status") == target["status"]
             and interface.get("default_contract_id") == reference_contract_id
             and interface.get("contract_ids") == [reference_contract_id]
             and contract.get("contract_id") == reference_contract_id and contract.get("source_id") == target["source"]
             and contract.get("source_ids") == [target["source"]] and contract.get("family_id") == target["family"]
             and contract.get("api_form") == target["form"] and contract.get("routing_mode") == target["route"],
             "Closed reference core identity or closed Interface state changed")
    boundary = _observation_boundary(target, reference_contract_id, pid, iid, interface, contract)
    return {"storage_source": "model_profile_database", "legacy_yaml_read": False, "read_only_core_facts": True,
        "read_only_recorded_reference": True, "reference_only": True, "modality": "text", "family": family,
        "suite_family_id": family, "canonical_family_id": family, "model": model, "source_id": target["source"],
        "profile_id": pid, "interface_id": iid, "profile_status": target["status"], "test_binding_status": target["status"],
        "test_binding_id": None, "parameter_test_binding_id": None, **{key: False for key in _FLAGS},
        "test_policy_parameter_test_enabled": False, "test_policy_pressure_test_enabled": False,
        "reference_source_enabled": False, "reference_source_executable": False,
        "known_model": True, "known_api_profile": True, "api_form": api_form,
        "transport": interface.get("transport_adapter_id"), "route_profile": route_profile,
        "reference_source": reference_contract_id, "reference_contract_id": reference_contract_id,
        "allowed_reference_sources": [], "allowed_reference_contract_ids": [], "test_profiles": [],
        "parameter_capabilities": _merge(contract.get("parameter_capabilities") or {}, interface.get("parameter_capabilities") or {}),
        "parameter_constraints": _merge(contract.get("parameter_constraints") or {}, interface.get("parameter_constraints") or {}),
        "source_conflicts": copy.deepcopy(interface.get("source_conflicts") or {}),
        "official_sources": copy.deepcopy(contract.get("official_sources") or []),
        "default_expectation": "reference_only", "expectations": {}, "parameter_expectations": {},
        "execution_target": None, "execution_target_boundary": {"included": False, "used_for_reference_resolution": False},
        "test_scope": "read_only_closed_core_observations", "certification_scope": "no_execution_binding_created",
        "full_parameter_matrix_verified": False, "disabled_reason": interface.get("disabled_reason") or
            "Reference display only; this Interface has no approved executable parameter binding.",
        "catalog_digest": catalog.digest, "test_extension_digest": catalog.payload.get("test_extension_digest"),
        **boundary}


def recorded_reference_payload(capability):
    """Create an empty-coverage Contract display without reading Test Bindings."""
    _require(capability.get("read_only_recorded_reference") is True and capability.get("read_only_core_facts") is True
             and all(capability.get(key) is False for key in _FLAGS) and capability.get("execution_target") is None
             and capability.get("test_binding_id") is None and capability.get("parameter_test_binding_id") is None,
             "Recorded reference payload must remain closed and without an execution binding")
    contract_id = capability["reference_contract_id"]
    return {"contract_id": contract_id, "reference_contract_id": contract_id, "reference_source": contract_id,
        "label": capability["reference_label"], "official_sources": copy.deepcopy(capability["official_sources"]),
        "model_family": capability["family"], "api_form": capability["api_form"], "route_profile": capability["route_profile"],
        "contract_reference_source": contract_id, "certification_scope": capability["certification_scope"],
        "evidence": "recorded_closed_core_observations", "test_profiles": [], "params": [], "comparison": [],
        "param_count": 0, "tested_params": [], "untested_params": [], **{key: False for key in _FLAGS}}
