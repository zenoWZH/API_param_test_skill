"""Source-scoped Grok image resolution/quality reference candidates.

These pure factories make no requests and do not certify an MPDB interface.
Official parameter support, decoded geometry, served quality, and token usage
are separate evidence fields. Non-square pixel dimensions remain observations.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .image_validation import ImageTestCase


SOURCE_CHECKED_AT = "2026-09-09"
MATRIX_ID = "grok_image_resolution_quality_v1"
PROMPT = "Generate exactly one image: a simple blue ceramic mug centered on a plain white background, no text."
GENERATION_GUIDE = "https://docs.x.ai/developers/model-capabilities/images/generation"
REST_REFERENCE = "https://docs.x.ai/developers/rest-api-reference/inference/images"
PRICING_REFERENCE = "https://docs.x.ai/developers/pricing"
RELEASE_NOTES = "https://docs.x.ai/developers/release-notes"
RETIREMENT_REFERENCE = "https://docs.x.ai/developers/migration/imagine-image-quality-nov-2"
RESOLUTIONS = ("1k", "2k")
QUALITIES_20 = ("low", "medium", "auto")
COMMON_ASPECT_RATIOS = (
    "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3",
    "2:1", "1:2", "19.5:9", "9:19.5", "20:9", "9:20",
)
ADDITIONAL_ASPECT_RATIOS_20 = ("21:9", "5:2")
ASPECT_RATIOS_20 = COMMON_ASPECT_RATIOS + ADDITIONAL_ASPECT_RATIOS_20
SQUARE_DIMENSIONS = {"1k": (1024, 1024), "2k": (2048, 2048)}
MODEL_ALIASES = {
    "grok-imagine-image": ("grok-imagine-image-2026-03-02",),
    "grok-imagine-image-quality": (
        "grok-imagine-image-quality-20260403",
        "grok-imagine-image-quality-latest",
        "grok-imagine-image-pro",
    ),
    "grok-imagine-image-2.0": (),
}
GROK_IMAGE_REFERENCE_MODELS = frozenset(
    model for canonical, aliases in MODEL_ALIASES.items() for model in (canonical, *aliases)
)


def canonical_grok_image_model(model: str) -> str:
    """Resolve only documented exact request IDs, never a substring match."""
    for canonical, aliases in MODEL_ALIASES.items():
        if model in (canonical, *aliases):
            return canonical
    raise ValueError(f"Not an exact supported Grok image reference model: {model!r}")


def is_grok_image_reference_model(model: str) -> bool:
    return model in GROK_IMAGE_REFERENCE_MODELS


def get_grok_image_reference_profile(model: str) -> dict[str, Any]:
    """Return reviewed documentation facts, with no implied live verification."""
    canonical = canonical_grok_image_model(model)
    is_20 = canonical == "grok-imagine-image-2.0"
    is_quality = canonical == "grok-imagine-image-quality"
    model_page = f"https://docs.x.ai/developers/models/{canonical}"
    references = [GENERATION_GUIDE, REST_REFERENCE, model_page, PRICING_REFERENCE, RELEASE_NOTES]
    if not is_20:
        references.append(RETIREMENT_REFERENCE)
    return {
        "matrix_id": MATRIX_ID,
        "source_id": "xai",
        "source_checked_at": SOURCE_CHECKED_AT,
        "model": model,
        "canonical_model": canonical,
        "aliases": list(MODEL_ALIASES[canonical]),
        "profile_id": f"image/xai/grok-imagine/{canonical}",
        "interface_id": f"image/xai/grok-imagine/{canonical}#openai-images-default",
        "api_form": "openai_images_generations",
        "route_profile": "vendor_direct",
        "official_references": references,
        "resolution_parameter": "resolution",
        "resolutions": list(RESOLUTIONS),
        "resolution_default": "1k",
        "resolution_tier_is_official_enum": True,
        "aspect_ratios": list(ASPECT_RATIOS_20 if is_20 else COMMON_ASPECT_RATIOS),
        "aspect_ratio_scope": "documented_2_0_enum" if is_20 else "conservative_common_documented_subset",
        "aspect_ratio_auto": "documented" if is_20 else "model_specific_default_observation",
        "aspect_ratio_scope_observations": [] if is_20 else list(ADDITIONAL_ASPECT_RATIOS_20),
        "aspect_ratio_scope_note": (
            None if is_20 else
            "The current guide demonstrates 2.0. Its migration guide names 21:9 and 5:2 "
            "as additions relative to image-quality; they are not promoted to legacy support."
        ),
        "quality_parameter_supported": is_20,
        "qualities": list(QUALITIES_20) if is_20 else [],
        "quality_default": "auto" if is_20 else None,
        "documented_auto_generation_quality": "low" if is_20 else None,
        "quality_boundary_policy": "documented_enum_rejection" if is_20 else "diagnostic_parameter_acceptance",
        "geometry_reference": {
            "square_dimensions": None if is_20 else {key: list(value) for key, value in SQUARE_DIMENSIONS.items()},
            "nominal_square_hypotheses": {key: list(value) for key, value in SQUARE_DIMENSIONS.items()} if is_20 else None,
            "square_dimension_authority": (
                "not_documented_for_exact_model" if is_20
                else "exact_model_card_square_tier_label"
            ),
            "square_dimension_sources": [
                f"https://docs.x.ai/developers/models/{'grok-imagine-image' if is_20 else canonical}"
            ],
            "non_square_dimensions": None,
            "non_square_pixel_status": "not_published_in_reviewed_official_sources",
            "aspect_ratio_relative_tolerance": 0.005,
            "aspect_ratio_tolerance_authority": "project_validation_policy_not_official_pixel_contract",
        },
        "token_expectation": {
            "status": "undocumented",
            "output_image_tokens": None,
            "quantity_reference": None,
            "billing_unit": "image",
            "documented_usage_field": "usage.cost_in_usd_ticks",
            "cost_is_token_count": False,
            "policy": "retain_actual_usage_without_price_to_token_conversion",
        },
        "lifecycle": {
            "status_as_of_source_check": "retirement_announced" if is_quality else "active",
            "notice_date": "2026-09-02" if is_quality else None,
            "retirement_date": "2026-11-02" if is_quality else None,
            "redirect_model_after_retirement": "grok-imagine-image-2.0" if is_quality else None,
            "redirect_quality_after_retirement": "low" if is_quality else None,
            "returned_model_must_be_observed": True,
        },
        "live_status": "live_unverified",
        "certification_status": "not_certified",
    }


def grok_image_resolution_quality_cases(
    model: str,
    *,
    api_form: str = "openai_images_generations",
    include_auto: bool = True,
    include_negative: bool = True,
) -> list[ImageTestCase]:
    """Build a deterministic full matrix for one exact model and generation API.

    ``include_auto`` controls auto-aspect/default-omission observations. Explicit
    ``quality=auto`` remains in the 2.0 quality enum and its complete cross product.
    Legacy quality probes diagnose unsupported-field handling without assuming
    that the service must reject a field it may instead ignore.
    """
    if api_form != "openai_images_generations":
        raise ValueError("Grok resolution/quality reference cases require the generation API form")
    profile = get_grok_image_reference_profile(model)
    is_20 = profile["quality_parameter_supported"]
    model_key = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")

    def case(
        suffix: str,
        parameters: dict[str, Any],
        *,
        outcome: str = "success",
        kind: str = "resolution_quality_cross",
        rejection_parameter: str | None = None,
        documented_outcome: str = "success",
        source_conflict: str | None = None,
    ) -> ImageTestCase:
        params = {"n": 1, "response_format": "b64_json", **parameters}
        ratio = params.get("aspect_ratio")
        resolution = params.get("resolution")
        quality = params.get("quality")
        known_ratio = ratio in profile["aspect_ratios"]
        expected_ratio = ratio if known_ratio and outcome != "rejection" else None
        square_hypothesis = SQUARE_DIMENSIONS.get(resolution) if ratio == "1:1" and outcome != "rejection" else None
        expected_size = square_hypothesis if not is_20 else None
        name = f"grok_rq_{model_key}_generation_{suffix}"
        documented_effective_quality = (
            "low" if quality in (None, "auto") else quality
        ) if is_20 and quality in (None, *QUALITIES_20) else None
        metadata = {
            "image_reference_candidate": True,
            "grok_resolution_quality": True,
            "reference_observation": True,
            "pin_parameters": True,
            "matrix_id": MATRIX_ID,
            "case_id": name,
            "test_profile": name,
            "case_kind": kind,
            "model": model,
            "canonical_model": profile["canonical_model"],
            "model_aliases": list(profile["aliases"]),
            "family": "grok-imagine",
            "api_form": api_form,
            "operation": "generation",
            "source_id": "xai",
            "reference_source": "xai",
            "source": GENERATION_GUIDE,
            "official_references": list(profile["official_references"]),
            "source_checked_at": SOURCE_CHECKED_AT,
            "prompt": PROMPT,
            "profile_id": profile["profile_id"],
            "interface_id": profile["interface_id"],
            "requested_resolution": resolution,
            "resolution": resolution,
            "resolution_tier": resolution,
            "resolution_tier_is_official_enum": True,
            "aspect_ratio": ratio,
            "expected_aspect_ratio": expected_ratio,
            "aspect_ratio_relative_tolerance": 0.005,
            "aspect_ratio_tolerance_authority": "project_validation_policy_not_official_pixel_contract",
            "expected_size": list(expected_size) if expected_size else None,
            "nominal_square_hypothesis": list(square_hypothesis) if is_20 and square_hypothesis else None,
            "nominal_square_hypothesis_authority": "legacy_tier_label_not_exact_model_contract" if is_20 and square_hypothesis else None,
            "pixel_expectation_status": (
                profile["geometry_reference"]["square_dimension_authority"] if expected_size
                else "observe_decoded_dimensions"
            ),
            "pixel_reference_sources": list(profile["geometry_reference"]["square_dimension_sources"]) if expected_size or square_hypothesis else [],
            "requested_quality": quality,
            "quality_parameter_present": "quality" in params,
            "quality_parameter_supported": is_20,
            "effective_quality": None,
            "effective_quality_status": "unverified_until_response_evidence",
            "documented_effective_quality": documented_effective_quality,
            "documented_effective_quality_authority": "documentation_not_response_observation" if is_20 else None,
            "token_expectation": copy.deepcopy(profile["token_expectation"]),
            "lifecycle": copy.deepcopy(profile["lifecycle"]),
            "documented_expected_outcome": documented_outcome,
            "rejection_parameter": rejection_parameter,
            "expected_rejection_field": rejection_parameter,
            "source_conflict": source_conflict,
            "live_status": "live_unverified",
            "certification_status": "not_certified",
        }
        return ImageTestCase(
            name=name,
            parameters=params,
            model_override=model,
            expected_outcome=outcome,
            expected_size=expected_size,
            expected_format=None,
            description=f"Official Grok image reference candidate: {suffix}.",
            tags=("grok-imagine", "resolution_quality_reference", kind),
            metadata=metadata,
        )

    cases: list[ImageTestCase] = []
    qualities = QUALITIES_20 if is_20 else (None,)
    for resolution in RESOLUTIONS:
        for ratio in profile["aspect_ratios"]:
            for quality in qualities:
                ratio_key = ratio.replace(".", "_").replace(":", "_")
                params = {"resolution": resolution, "aspect_ratio": ratio}
                if quality is not None:
                    params["quality"] = quality
                cases.append(case(f"{resolution}_aspect_{ratio_key}_quality_{quality or 'omitted'}", params))

    if include_auto:
        for resolution in RESOLUTIONS:
            for quality in qualities:
                params = {"resolution": resolution}
                if quality is not None:
                    params["quality"] = quality
                cases.append(case(
                    f"{resolution}_aspect_auto_quality_{quality or 'omitted'}",
                    {**params, "aspect_ratio": "auto"}, outcome="observation", kind="auto_aspect",
                    documented_outcome="success" if is_20 else "model_specific_observation",
                    rejection_parameter=None if is_20 else "aspect_ratio",
                ))
                cases.append(case(
                    f"{resolution}_aspect_omitted_quality_{quality or 'omitted'}",
                    params, outcome="observation", kind="omitted_aspect_default",
                ))
        for quality in qualities:
            params = {"aspect_ratio": "1:1"}
            if quality is not None:
                params["quality"] = quality
            cases.append(case(
                f"resolution_omitted_quality_{quality or 'omitted'}", params,
                outcome="observation", kind="omitted_resolution_default",
            ))
        if is_20:
            for resolution in RESOLUTIONS:
                cases.append(case(
                    f"{resolution}_quality_omitted", {"resolution": resolution, "aspect_ratio": "1:1"},
                    outcome="observation", kind="omitted_quality_default",
                ))
        cases.append(case("defaults_omitted", {}, outcome="observation", kind="all_defaults_omitted"))

    if not is_20:
        for resolution in RESOLUTIONS:
            for ratio in ADDITIONAL_ASPECT_RATIOS_20:
                cases.append(case(
                    f"{resolution}_aspect_{ratio.replace(':', '_')}_scope_observation",
                    {"resolution": resolution, "aspect_ratio": ratio},
                    outcome="observation", kind="legacy_aspect_scope",
                    rejection_parameter="aspect_ratio", documented_outcome="model_specific_observation",
                    source_conflict="current_2_0_guide_does_not_establish_legacy_model_scope",
                ))

    if include_negative:
        if not is_20:
            for quality in (*QUALITIES_20, "high"):
                cases.append(case(
                    f"quality_{quality}_unsupported_field_observation",
                    {"resolution": "1k", "aspect_ratio": "1:1", "quality": quality},
                    outcome="observation", kind="legacy_quality_boundary",
                    rejection_parameter="quality", documented_outcome="parameter_not_supported",
                ))
        else:
            cases.append(case(
                "reject_quality_high", {"resolution": "1k", "aspect_ratio": "1:1", "quality": "high"},
                outcome="rejection", kind="negative_boundary", rejection_parameter="quality",
                documented_outcome="rejection",
            ))
        for field, value in (("resolution", "4k"), ("aspect_ratio", "7:5")):
            params = {"resolution": "1k", "aspect_ratio": "1:1", field: value}
            if is_20:
                params["quality"] = "low"
            cases.append(case(
                f"reject_{field}_{value.replace(':', '_')}", params,
                outcome="rejection", kind="negative_boundary", rejection_parameter=field,
                documented_outcome="rejection",
            ))
    return cases
