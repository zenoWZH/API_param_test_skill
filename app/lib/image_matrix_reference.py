"""Exact-source admission and diagnostics for independent image matrix candidates.

These manifests select checked-in factories. They never provide request bodies,
enable a default MPDB policy, or certify a route from successful observations.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .image_validation import ImageTestCase, evaluate_case

SCHEMA_VERSION = 2
MATRIX_KINDS = {"gpt_resolution_quality", "grok_resolution_quality"}


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def matrix_identity(model: str, api_form: str) -> dict[str, str]:
    if model.startswith("gpt-image-"):
        from .gpt_image_resolution_quality import supported_api_forms

        if api_form not in supported_api_forms(model):
            raise ValueError("Unsupported exact GPT image matrix API form")
        suffix = {"openai_images_generations": "images/generations",
                  "openai_images_edits": "images/edits", "openai_responses": "responses"}[api_form]
        return {"matrix_kind": "gpt_resolution_quality", "source_id": "openai", "family": "gpt-image-2",
                "route_profile": "openai_official", "endpoint": "https://api.openai.com/v1/" + suffix}
    from .grok_image_resolution_quality import canonical_grok_image_model

    canonical_grok_image_model(model)
    if api_form != "openai_images_generations":
        raise ValueError("Grok image candidates require the official Images generation interface")
    return {"matrix_kind": "grok_resolution_quality", "source_id": "xai", "family": "grok-imagine",
            "route_profile": "xai_official", "endpoint": "https://api.x.ai/v1/images/generations"}


def image_matrix_cases(model: str, api_form: str) -> list[ImageTestCase]:
    identity = matrix_identity(model, api_form)
    if identity["source_id"] == "openai":
        from .gpt_image_resolution_quality import gpt_image_resolution_quality_cases

        return gpt_image_resolution_quality_cases(model, api_form=api_form)
    from .grok_image_resolution_quality import grok_image_resolution_quality_cases

    return grok_image_resolution_quality_cases(model, api_form=api_form)


def select_image_matrix_cases(model: str, api_form: str, *, suite: str = "smoke",
                              include_4k: bool = False, include_2k: bool = False,
                              include_negative: bool = True) -> list[ImageTestCase]:
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError("Unsupported image reference suite")
    cases = image_matrix_cases(model, api_form)
    if suite == "smoke":
        return cases[:1]
    return [case for case in cases
            if (include_negative or case.expected_outcome != "rejection")
            and (case.expected_outcome == "rejection" or include_4k
                 or str(case.metadata.get("resolution_tier") or "").lower() != "4k")
            and (case.expected_outcome == "rejection" or include_2k or not case.metadata.get("grok_resolution_quality")
                 or str(case.metadata.get("resolution_tier") or "").lower() != "2k")]


def build_image_matrix_candidate(*, model: str, api_form: str, capability: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewable manifest offline from an exact registered MPDB identity."""
    identity = matrix_identity(model, api_form)
    if (capability.get("profile_status") != "registered"
            or capability.get("source_id") != identity["source_id"]
            or capability.get("route_profile") not in {identity["route_profile"], "vendor_direct"}
            or capability.get("api_form") != api_form):
        raise ValueError("Image matrix candidate requires the exact registered official MPDB source/route/API")
    identity["route_profile"] = capability["route_profile"]
    fields = {"profile_id": capability.get("profile_id"), "interface_id": capability.get("interface_id"),
              "contract_id": capability.get("reference_contract_id") or capability.get("default_reference_source")}
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise ValueError("Image matrix candidate requires complete MPDB profile/interface/contract identity")
    cases = image_matrix_cases(model, api_form)
    case_ids = [case.name for case in cases]
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("Image matrix factory returned empty or duplicate cases")
    return {"schema_version": SCHEMA_VERSION, **identity, "model": model, "api_form": api_form,
            "candidate_id": identity["matrix_kind"] + "/" + model + "/" + api_form,
            **fields, "case_ids": case_ids,
            "case_definitions_sha256": canonical_digest([case.public() for case in cases]),
            "execution_mode": "reference_observation", "certified": False, "live_verified": False}


def _read_candidate(path: str) -> tuple[dict[str, Any], bytes]:
    file = Path(path)
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Image candidate must be a bounded regular JSON file")
    raw = file.read_bytes()
    candidate = json.loads(raw)
    if not isinstance(candidate, dict):
        raise ValueError("Image candidate must be an object")
    return candidate, raw


def is_image_matrix_candidate(path: str) -> bool:
    candidate, _ = _read_candidate(path)
    return "matrix_kind" in candidate or candidate.get("schema_version") == SCHEMA_VERSION


def load_image_matrix_candidate(path: str, *, model: str, family: str, route_profile: str,
                                api_form: str, endpoint: str, capability: dict[str, Any]) -> dict[str, Any]:
    candidate, raw = _read_candidate(path)
    expected = build_image_matrix_candidate(model=model, api_form=api_form, capability=capability)
    if (family, route_profile, endpoint) != (expected["family"], expected["route_profile"], expected["endpoint"]):
        raise ValueError("Image matrix candidate does not match the exact official endpoint/family/route")
    if candidate != expected:
        raise ValueError("Image matrix candidate identity, cases or reviewed definitions digest changed")
    return {**candidate, "sha256": hashlib.sha256(raw).hexdigest()}


def persist_image_matrix_candidate(path: str, destination: Path, admitted: dict[str, Any]) -> None:
    candidate, raw = _read_candidate(path)
    if {**candidate, "sha256": hashlib.sha256(raw).hexdigest()} != admitted:
        raise ValueError("Image candidate changed after admission")
    if destination.is_symlink() or destination.exists():
        raise ValueError("Image candidate evidence destination already exists")
    with destination.open("xb") as stream:
        stream.write(raw)
    destination.chmod(0o600)


def rejection_attribution(case: ImageTestCase, error: Any) -> dict[str, Any]:
    explicit = case.metadata.get("rejection_parameter") or case.metadata.get("expected_rejection_field")
    fields = [explicit] if explicit else [field for field in ("size", "quality", "resolution", "aspect_ratio")
                                        if field in case.parameters]
    data = error if isinstance(error, dict) else {}
    text = str(data.get("param") or "") + " " + str(data.get("message") or (error if isinstance(error, str) else ""))
    matched = [field for field in fields if isinstance(field, str)
               and re.search(r"(?<![A-Za-z0-9_])" + re.escape(field) + r"(?![A-Za-z0-9_])", text, re.I)]
    return {"required": True, "matched": bool(matched), "target": explicit,
            "candidate_fields": fields, "matched_fields": matched}


def auto_output_geometry_audit(images: list[Any]) -> dict[str, Any]:
    """Project sanity bounds on decoded auto output; no requested-size guarantee."""
    constraints = {"min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840, "max_aspect_ratio": 3}
    rows = []
    for image in images:
        width = image.get("width") if isinstance(image, dict) else getattr(image, "width", None)
        height = image.get("height") if isinstance(image, dict) else getattr(image, "height", None)
        valid = type(width) is int and type(height) is int and width > 0 and height > 0
        sane = (valid and constraints["min_pixels"] <= width * height <= constraints["max_pixels"]
                and max(width, height) <= constraints["max_edge"]
                and max(width, height) <= min(width, height) * constraints["max_aspect_ratio"])
        rows.append({"width": width, "height": height, "pass": bool(sane)})
    return {"scope": "decoded_auto_output_with_project_sanity_only", "pass": bool(rows) and all(row["pass"] for row in rows),
            "auto_output_geometry_certified": False, "constraints_are_official_auto_guarantees": False,
            "constraints": constraints, "images": rows}


def evaluate_matrix_observation(case: ImageTestCase, *, status_code: int | None, images: list[Any],
                                usage: dict[str, Any], latency_ms: float | None, error: Any) -> dict[str, Any]:
    """Decode/count/format/geometry checks remain strict for accepted observations."""
    checked = evaluate_case(replace(case, expected_outcome="success"), status_code=status_code,
                            images=images, usage=usage, latency_ms=latency_ms, error=error)
    attribution = rejection_attribution(case, error)
    accepted = isinstance(status_code, int) and 200 <= status_code < 300 and checked.get("pass") is True and not error
    auto_geometry = auto_output_geometry_audit(images) if case.parameters.get("size") == "auto" else None
    if auto_geometry is not None:
        accepted = accepted and auto_geometry["pass"]
        checked.update(geometry_check=auto_geometry, auto_output_geometry_certified=False,
                       auto_output_dimension_scope="observed_pixels_without_requested_custom_size_guarantee")
        if images and not auto_geometry["pass"]:
            checked["failures"] = [*checked.get("failures", []), "auto_output_project_sanity_failed"]
    rejected = status_code in {400, 422} and attribution["matched"] is True
    complete = accepted or rejected
    documented = case.metadata.get("documented_expected_outcome") or case.expected_outcome
    return {**checked, "status": "observed_acceptance" if accepted else "observed_parameter_rejection" if rejected else "unresolved",
            "diagnostic_pass": complete, "pass": False, "overall_pass": False, "compatibility_pass": False,
            "certified_route_contract_pass": False, "verification_level": "diagnostic_observation" if complete else "none",
            "parameter_rejection_attribution": attribution,
            "documented_outcome_match": accepted if documented == "success" else rejected if documented == "rejection" else None,
            "failures": [] if rejected else checked.get("failures", []),
            "dimension_validation_pass": not any("dimension_mismatch" in item for item in checked.get("failures", [])) if accepted or images else None,
            "aspect_ratio_validation_pass": not any("aspect_ratio_mismatch" in item for item in checked.get("failures", [])) if accepted or images else None}


def finalize_matrix_observation(case: ImageTestCase, result: dict[str, Any], *, payload: dict[str, Any],
                                protocol_failures: list[str]) -> None:
    """Reported cost and identity have independent, explicit evidence limits."""
    source = case.metadata.get("source_id") or case.metadata.get("source")
    # Parameter observations retain accounting anomalies without rewriting
    # them into parameter rejection or claiming accounting accuracy.
    token_ok = True
    result["quantity_audit_pass"] = result.get("token_accuracy_pass")
    result["usage_validation_pass"] = result.get("token_validation_pass")
    result["diagnostic_scope"] = "request_parameter_acceptance_and_decoded_output_constraints"
    returned = payload.get("model")
    identity = result.get("model_identity_audit") or {}
    identity_ok = not returned or identity.get("status") != "mismatch"
    if source == "xai":
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        cost = usage.get("cost_in_usd_ticks", payload.get("cost_in_usd_ticks"))
        cost_valid = type(cost) is int and cost >= 0 if cost is not None else None
        result["cost_usage_audit"] = {"field": "cost_in_usd_ticks", "value": cost,
                                     "pass": cost_valid, "status": "unverified" if cost_valid is None else "pass" if cost_valid else "fail",
                                     "scope": "reported_cost_schema_only", "output_token_quantity_inferred": False}
        token_ok = cost_valid is not False
        result["token_accuracy_pass"] = None
        result["token_quantity_scope"] = "unverified_undocumented_image_token_counts"
    observed = result.get("diagnostic_pass") is True and not protocol_failures and not payload.get("error") and token_ok and identity_ok
    if result.get("status") == "observed_parameter_rejection":
        observed = result.get("diagnostic_pass") is True and not protocol_failures and token_ok
    if not observed:
        result["status"] = "unresolved"
    result.update({"diagnostic_pass": observed, "pass": False, "overall_pass": False, "compatibility_pass": False,
                   "certified_route_contract_pass": False, "compatibility_status": "not_certified",
                   "overall_status": result["status"], "returned_image_model": returned,
                   "returned_image_model_status": "unreported" if not returned else identity.get("status", "unverified"),
                   "requested_quality": case.parameters.get("quality"),
                   "effective_quality": payload.get("quality"),
                   "quality_evidence": "response_echo" if payload.get("quality") is not None else "request_acceptance_only"})
