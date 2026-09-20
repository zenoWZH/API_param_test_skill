"""Author optional official text-model media workflows without changing gates.

Only checked-in public config and the sanitized configured-target snapshot are
read. Model input support, configured API forms, and the existing MPDB execution
policy must all agree before publication.
This module writes no artifacts and performs no HTTP or provider requests.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-09-20"
LIVE_APPROVAL_DATE = "2026-09-20"
PREFIX = "workflow/media-input/"
SUPPORTED_FORMS = frozenset({"openai_chat_completions", "openai_responses",
                             "anthropic_messages", "gemini_generate_content"})
OFFICIAL_API_HOSTS = {
    "api.openai.com": "openai", "api.anthropic.com": "anthropic",
    "api.x.ai": "xai", "generativelanguage.googleapis.com": "google_ai_studio",
    "api.deepseek.com": "deepseek", "api.moonshot.ai": "moonshot",
    "api.moonshot.cn": "moonshot", "api.minimax.io": "minimax",
    "api.minimax.chat": "minimax", "api.z.ai": "zhipu",
    "open.bigmodel.cn": "zhipu",
}
SUMMARY = (
    "Optional exact-provider/model/API image, video and audio input workflow "
    "updated on 2026-09-20 with real media fixtures and video decisions. "
    "Individual source facts retain their own retrieval dates. Existing generic "
    "reference policies and pressure gates remain unchanged. Factory digest "
    "freezes media cases and fixture bytes; it is not live compatibility or "
    "token-quantity proof."
)
LIVE_APPROVAL_SUMMARY = (
    "User-approved official media-input validation on 2026-09-20, approval "
    "official-media-input-validation-20260920. Permission is limited to the "
    "packaged exact source/provider/request-model/API/interface/contract tuples "
    "and media_input factory version 1. Ordinary parameter policies, pressure, "
    "Interactions, native AWS and retired-model gates are unchanged. "
    "Documentation and an executable workflow are not live verification."
)


def _approval_policy():
    from model_profile_db.compiler import (
        MEDIA_INPUT_VALIDATION_APPROVAL_ID, is_approved_media_input_workflow,
    )
    return MEDIA_INPUT_VALIDATION_APPROVAL_ID, is_approved_media_input_workflow


def _provenance_values(binding: dict) -> tuple[str, str]:
    approval_id, approved = _approval_policy()
    if (binding.get("execution_permission") or {}).get("approval_id") == approval_id and approved(binding):
        return LIVE_APPROVAL_DATE, LIVE_APPROVAL_SUMMARY
    return DATE, SUMMARY


def public_config() -> dict[str, Any]:
    """Intentionally bypass load_config, dotenv and private provider overlays."""
    return yaml.load((ROOT / "config.yaml").read_text(encoding="utf-8"),
                     Loader=yaml.CSafeLoader)


def _snapshot_config(config: dict, targets: list[dict]) -> tuple[dict, set[str]]:
    """Preserve explicit local targets without storing their private endpoints.

    Public provider settings take precedence. Local-only entries carry their
    sanitized exact selectors; the runtime adapter validates the real endpoint.
    """
    result = copy.deepcopy(config)
    providers = result.setdefault("providers", {})
    public_ids = set(providers)
    local_ids = set()
    for target in targets:
        provider_id = target["provider_id"]
        if provider_id in public_ids:
            continue
        source_id, model, form = target["source_id"], target["model"], target["api_form"]
        route = target.get("route_profile")
        if not route:
            raise ValueError("Sanitized media target lacks its configured route: " + provider_id + "/" + model)
        provider = providers.setdefault(provider_id, {"reference_source_id": source_id,
            "models": {"candidates": [], "routes": {}, "reference_source_ids": {}}})
        if provider["reference_source_id"] != source_id:
            raise ValueError("Conflicting sanitized provider source: " + provider_id)
        models = provider["models"]
        if model not in models["candidates"]:
            models["candidates"].append(model)
        models["reference_source_ids"][model] = source_id
        selector = {key: target[key] for key in ("profile_id", "interface_id", "contract_id", "transport_adapter_id", "endpoint_class")
                    if target.get(key)}
        if selector.get("contract_id"):
            selector["reference_contract_id"] = selector.pop("contract_id")
        forms = models["routes"].setdefault(model, {}).setdefault(route, {"api_forms": {}})["api_forms"]
        if form in forms and forms[form] != selector:
            raise ValueError("Conflicting sanitized media API selector")
        forms[form] = selector
        local_ids.add(provider_id)
    return result, local_ids


def _interfaces(catalog: dict) -> list[tuple[str, dict, str, dict]]:
    result = []
    for profile_id, profile in catalog.get("profiles", {}).items():
        if profile.get("modality") != "text":
            continue
        nested = profile.get("interfaces")
        if isinstance(nested, dict):
            result.extend((profile_id, profile, profile_id + "#" + slug, interface)
                          for slug, interface in nested.items())
        else:
            result.extend((profile_id, profile, interface_id, catalog["interfaces"][interface_id])
                          for interface_id in profile.get("interface_ids", []))
    return result


def _resolved_contract(catalog: dict, contract_id: str, seen=()) -> dict:
    if contract_id in seen:
        raise ValueError("Media workflow contract inheritance cycle")
    raw = catalog.get("contracts", {}).get(contract_id)
    if not isinstance(raw, dict):
        raise ValueError("Media workflow requires an existing contract: " + contract_id)
    parent = raw.get("inherits")
    result = _resolved_contract(catalog, parent, (*seen, contract_id)) if parent else {}
    return {**result, **raw}


def _execution_permission(profile: dict, interface: dict, contract: dict,
                          binding: dict, bindings: dict) -> dict | None:
    policies = [row for row in bindings.values()
                if row.get("extension_type") == "model_test_policy"
                and row.get("interface_id") == binding["interface_id"]
                and binding["contract_id"] in row.get("reference_contract_ids", [])]
    if (profile.get("profile_state", "executable") == "executable"
            and interface.get("test_binding_status", "required") == "required"
            and contract.get("test_binding_status", "required") == "required"
            and all(row.get(field, True) is True for row in (interface, contract)
                    for field in ("enabled", "executable"))
            and policies
            and all(row.get("parameter_test_enabled") is True
                    and not str(row.get("disabled_reason") or "").strip()
                    and all(row.get(field, True) is True
                            for field in ("enabled", "executable", "runner_enabled"))
                    for row in policies)):
        return {"scope": "exact_workflow", "mode": "inherit"}
    approval_id, approved = _approval_policy()
    candidate = {**binding, "execution_permission": {
        "scope": "exact_workflow", "mode": "scoped_research", "approval_id": approval_id}}
    if approved(candidate):
        return candidate["execution_permission"]
    # The compiler already defines this exact native Fable exception. Require
    # the existing same-target scoped workflow as evidence; never invent one.
    target = binding["execution_target"]
    if (binding["source_id"] == "anthropic"
            and target["provider_id"] == "anthropic_official"
            and target["request_model_id"] in {"claude-fable-5", "claude-fable-5-1"}
            and target["api_form"] == "anthropic_messages"):
        identity = ("source_id", "profile_id", "interface_id", "contract_id", "execution_target")
        for row in bindings.values():
            permission = row.get("execution_permission", {})
            if (row.get("extension_type") == "test_workflow" and row.get("enabled") is True
                    and not row.get("gateway_mapping")
                    and all(row.get(key) == binding[key] for key in identity)
                    and permission.get("mode") == "scoped_research"
                    and permission.get("approval_id")):
                return copy.deepcopy(permission)
    return None


def build_media_input_workflows(catalog: dict, bindings: dict, *, config: dict | None = None,
                               configured_targets: list[dict] | None = None) -> dict:
    from lib.media_input_matrix import load_reference
    from lib.test_runner.adapters import media_input

    if config is None:
        config = public_config()
        if configured_targets is None:
            configured_targets = json.loads((ROOT / "references/media_input/configured_targets.json").read_text())["targets"]
    config, local_provider_ids = _snapshot_config(config, configured_targets or [])
    reference = load_reference()
    model_references = {(row["source_id"], row["model"], row["api_form"]): row
                        for row in reference["models"]}
    interface_rows = _interfaces(catalog)
    source_digest = media_input.source_digest()
    proposed, skipped = {}, []
    for provider_id, provider in sorted(config.get("providers", {}).items()):
        snapshot_only = provider_id in local_provider_ids
        endpoint = urlsplit(str(provider.get("base_url") or ""))
        source_id = provider["reference_source_id"] if snapshot_only else OFFICIAL_API_HOSTS.get(endpoint.hostname or "")
        if not snapshot_only and (not source_id or endpoint.scheme != "https" or endpoint.username
                or endpoint.password or endpoint.port not in (None, 443)):
            continue
        source = catalog.get("sources", {}).get(source_id, {})
        if source.get("source_type") not in {"official_direct", "cloud_managed"}:
            continue
        model_config = provider.get("models") or {}
        declared_source = provider.get("reference_source_id")
        if declared_source and declared_source != source_id:
            raise ValueError("Official media provider source/host mismatch: " + provider_id)
        for request_model in sorted(set(model_config.get("candidates") or [])):
            per_source = (model_config.get("reference_source_ids") or {}).get(request_model)
            if per_source and per_source != source_id:
                raise ValueError("Official media model source/host mismatch: " + request_model)
            reference_model = (model_config.get("reference_model_ids") or {}).get(request_model, request_model)
            for route, route_config in sorted((model_config.get("routes", {}).get(request_model) or {}).items()):
                for form, form_config in sorted((route_config.get("api_forms") or {}).items()):
                    selector = {"provider_id": provider_id, "source_id": source_id,
                                "request_model_id": request_model, "api_form": form, "route_profile": route}

                    def skip(reason: str) -> None:
                        skipped.append({**selector, "reason": reason})

                    if form not in SUPPORTED_FORMS or route not in {"vendor_direct", "vendor_compat", "google_ai_studio", "aliyun_maas"}:
                        skip("api_form_or_route_outside_media_workflow")
                        continue
                    reference_row = model_references.get((source_id, request_model, form))
                    if not reference_row:
                        skip("exact_media_documentation_missing")
                        continue
                    lifecycle = reference_row.get("lifecycle") or {}
                    if isinstance(lifecycle, str):
                        lifecycle = {"status": lifecycle}
                    if lifecycle.get("status") in {"retired", "discontinued"}:
                        skip("documented_model_retired")
                        continue
                    if not any(reference_row.get(k, {}).get("status") == "supported" for k in ("image", "audio", "video")):
                        skip("no_documented_supported_media_input")
                        continue
                    endpoint_class = (form_config or {}).get("endpoint_class")
                    if source_id == "zhipu" and "/coding/" in endpoint.path:
                        endpoint_class = "zai_coding"
                    if endpoint_class == "zai_coding":
                        if "zai_coding" not in reference_row.get("endpoint_classes", []):
                            skip("coding_endpoint_media_not_documented")
                            continue
                    matches = [(pid, p, iid, interface) for pid, p, iid, interface in interface_rows
                               if p.get("source_id") == source_id
                               and reference_model in p.get("request_model_ids", [])
                               and request_model in (interface.get("request_model_ids") or p.get("request_model_ids", []))
                               and interface.get("api_form") == form and interface.get("routing_mode") == route
                               and (endpoint_class != "zai_coding" or "#zai-coding-" in iid)]
                    exact = form_config or {}
                    matches = [(pid, p, iid, interface) for pid, p, iid, interface in matches
                               if (not exact.get("profile_id") or exact["profile_id"] == pid)
                               and (not exact.get("interface_id") or exact["interface_id"] == iid)
                               and (not exact.get("transport_adapter_id") or exact["transport_adapter_id"] == interface.get("transport_adapter_id"))]
                    if len(matches) != 1:
                        skip("exact_reference_identity_missing_or_ambiguous")
                        continue
                    profile_id, profile, interface_id, interface = matches[0]
                    if endpoint_class == "zai_coding" and "#zai-coding-" not in interface_id:
                        skip("coding_endpoint_reference_interface_mismatch")
                        continue
                    if profile.get("lifecycle") in {"retired", "discontinued"}:
                        skip("catalog_model_retired")
                        continue
                    transport = interface.get("transport_adapter_id")
                    wire_config = (provider.get("api_interfaces") or {}).get(transport, {})
                    wire_url = urlsplit(str(wire_config.get("base_url") or provider.get("base_url") or ""))
                    if not snapshot_only and (wire_url.scheme != "https" or OFFICIAL_API_HOSTS.get(wire_url.hostname or "") != source_id
                            or wire_url.username or wire_url.password or wire_url.port not in (None, 443)):
                        skip("transport_endpoint_not_official_source")
                        continue
                    contract_id = (form_config or {}).get("reference_contract_id") or interface.get("default_contract_id")
                    if not contract_id or contract_id not in interface.get("contract_ids", []):
                        skip("exact_reference_contract_missing")
                        continue
                    contract = _resolved_contract(catalog, contract_id)
                    # MPDB identifiers are lowercase; the wire request ID stays
                    # exact (for example MiniMax-M3) in execution_target.
                    workflow_id = f"media-input/{provider_id}/{request_model.casefold()}/{form}"
                    binding_id = "workflow/" + workflow_id
                    row = {"extension_type": "test_workflow", "workflow_schema_version": 1,
                           "workflow_id": workflow_id, "source_id": source_id, "profile_id": profile_id,
                           "interface_id": interface_id, "contract_id": contract_id,
                           "execution_target": {"provider_id": provider_id, "request_model_id": request_model,
                                                "api_form": form, "transport_adapter_id": transport},
                           "provenance_record_id": "test-binding/" + binding_id, "enabled": True,
                           "workflow": {"factory_id": media_input.FACTORY_ID, "version": media_input.FACTORY_VERSION,
                                        "source_sha256": source_digest}}
                    permission = _execution_permission(profile, interface, contract, row, bindings)
                    if permission is None:
                        skip("existing_reference_execution_policy_disabled")
                        continue
                    case_ids = media_input.case_ids_for(source_id, request_model, form)
                    if not case_ids:
                        skip("no_executable_media_cases")
                        continue
                    row["execution_permission"] = permission
                    row["workflow"] = {"factory_id": media_input.FACTORY_ID, "version": media_input.FACTORY_VERSION,
                                       "source_sha256": source_digest, "case_ids": case_ids}
                    if binding_id in proposed and proposed[binding_id] != row:
                        raise ValueError("Ambiguous configured media workflow: " + binding_id)
                    proposed[binding_id] = row
    if media_input.source_digest() != source_digest:
        raise ValueError("Media input workflow source drifted while authoring")
    return {"bindings": proposed, "skipped": skipped}


def apply_media_input_workflows(catalog: dict, bindings: dict, additions: dict, *, config=None,
                                configured_targets=None) -> dict:
    result = build_media_input_workflows(catalog, bindings, config=config, configured_targets=configured_targets)
    proposed = result["bindings"]
    for binding_id, row in proposed.items():
        if binding_id in bindings and bindings[binding_id] != row:
            raise ValueError("Conflicting existing media workflow binding: " + binding_id)
    overrides = copy.deepcopy(additions)
    for row in proposed.values():
        record_id = row["provenance_record_id"]
        date, summary = _provenance_values(row)
        for key, value in (("provenance_retrieved_at_overrides", date),
                           ("provenance_section_summary_overrides", summary)):
            values = overrides.setdefault(key, {})
            if record_id in values and values[record_id] != value:
                raise ValueError("Conflicting media workflow provenance: " + record_id)
            values[record_id] = value
    bindings.update(copy.deepcopy(proposed))
    additions.clear()
    additions.update(overrides)
    return {"applied": bool(proposed), "workflows": len(proposed),
            "workflow_binding_ids": sorted(proposed), "skipped": result["skipped"],
            "case_counts": {key: len(row["workflow"]["case_ids"]) for key, row in proposed.items()},
            "legacy_bindings_changed": False, "native_execution_gates_changed": False, "live_verified": False}


def attach_media_input_provenance(records: dict, bindings: dict) -> None:
    """Describe media-page evidence only in the new provenance namespace.

    official_urls is the compiler's exact Contract/Interface source projection;
    keep it unchanged. The workflow digest binds the additional media research,
    whose per-target links are recorded in this workflow's section summary.
    """
    from lib.media_input_matrix import load_reference

    reference = load_reference()
    sources = {row["id"]: row for row in reference["sources"]}
    models = {(row["source_id"], row["model"], row["api_form"]): row for row in reference["models"]}
    protocols = {(row["source_id"], row["api_form"]): row for row in reference["protocols"]}
    for binding_id, binding in bindings.items():
        if not binding_id.startswith(PREFIX):
            continue
        target = binding["execution_target"]
        row = models[(binding["source_id"], target["request_model_id"], target["api_form"])]
        protocol = protocols[(binding["source_id"], target["api_form"])]
        evidence = set(protocol["evidence"])
        for modality in ("image", "audio", "video"):
            if row.get(modality, {}).get("status") == "supported":
                evidence.update(row[modality]["evidence"])
        record = records[binding["provenance_record_id"]]
        urls = sorted({sources[key]["url"] for key in evidence})
        record["section_summary"] = _provenance_values(binding)[1] + " Media research evidence: " + "; ".join(urls)
