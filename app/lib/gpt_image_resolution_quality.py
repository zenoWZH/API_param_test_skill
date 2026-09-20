"""Independent, official-source GPT image size x quality reference candidates.

This pure factory does not install MPDB bindings or claim live verification.
The 1K/2K/4K tiers group representative sizes for this project; GPT requests
carry native ``size`` strings, never a fabricated ``resolution`` parameter.
Existing GPT Image 2 and 2.5 parameter matrices remain independently defined.
"""
from __future__ import annotations

import copy
from math import gcd
from typing import Any

from .image_token_expectations import image_output_token_expectation
from .image_validation import ImageTestCase

MATRIX_ID = "gpt_image_resolution_quality_v1"
SOURCE_CHECKED_AT = "2026-09-09"
GUIDE = "https://developers.openai.com/api/docs/guides/image-generation"
PROMPTING_GUIDE = "https://developers.openai.com/api/docs/guides/image-prompting"
GENERATIONS_REFERENCE = "https://developers.openai.com/api/reference/python/resources/images/methods/generate"
EDITS_REFERENCE = "https://developers.openai.com/api/reference/python/resources/images/methods/edit"
RESPONSES_REFERENCE = "https://developers.openai.com/api/reference/python/resources/responses/methods/create"
MAINLINE_MODEL = "gpt-6-astra"
PROMPT = "Generate exactly one image: a simple blue ceramic mug centered on a plain white background, no text."
EDIT_PROMPT = "Change the blue square in the reference image to orange. Preserve the square's position, the white background, and the TEST 1 label."
CANONICAL_MODELS = (
    "gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2",
    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
)
SNAPSHOTS = {
    "gpt-image-2-2026-04-21": "gpt-image-2",
    "gpt-image-2.5-flare-2026-09-08": "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst-2026-09-08": "gpt-image-2.5-sunburst",
}
API_FORMS = {
    "openai_images_generations": ("generation", "/images/generations", GENERATIONS_REFERENCE),
    "openai_images_edits": ("edit", "/images/edits", EDITS_REFERENCE),
    "openai_responses": ("responses", "/responses", RESPONSES_REFERENCE),
}
_LEGACY_MODELS = frozenset(CANONICAL_MODELS[:3])
_BASE_QUALITIES = ("low", "medium", "high")
_EXTENDED_QUALITIES = (*_BASE_QUALITIES, "xhigh", "max")
# Portraits are independent cases: legacy token references are asymmetric.
_LEGACY_SIZES = (
    ("1K", "square", 1024, 1024),
    ("1K", "landscape_3_2", 1536, 1024),
    ("1K", "portrait_2_3", 1024, 1536),
)
_FLEXIBLE_SIZES = (
    *_LEGACY_SIZES,
    ("1K", "landscape_16_9", 1536, 864),
    ("1K", "portrait_9_16", 864, 1536),
    ("2K", "square", 2048, 2048),
    ("2K", "landscape_4_3", 2048, 1536),
    ("2K", "portrait_3_4", 1536, 2048),
    ("2K", "landscape_16_9", 2048, 1152),
    ("2K", "portrait_9_16", 1152, 2048),
    ("4K", "landscape_16_9", 3840, 2160),
    ("4K", "portrait_9_16", 2160, 3840),
    ("boundary", "minimum_pixels_landscape", 1024, 640),
    ("boundary", "minimum_pixels_portrait", 640, 1024),
    ("boundary", "maximum_aspect_landscape", 1536, 512),
    ("boundary", "maximum_aspect_portrait", 512, 1536),
)


def canonical_model(model: str) -> str:
    """Accept only IDs present in the authored catalog scope, without guessing."""
    if model in CANONICAL_MODELS:
        return model
    if model in SNAPSHOTS:
        return SNAPSHOTS[model]
    raise ValueError(f"Unsupported exact GPT image reference model: {model!r}")


def supported_api_forms(model: str) -> tuple[str, ...]:
    """Scope follows existing MPDB interfaces, not all possible vendor APIs."""
    canonical = canonical_model(model)
    if canonical in CANONICAL_MODELS[-2:]:
        return tuple(API_FORMS)
    return ("openai_images_generations",)


def _token_reference(model: str, size: tuple[int, int] | None, quality: str,
                     *, rejected: bool) -> dict[str, Any]:
    canonical = canonical_model(model)
    reference: dict[str, Any] = {
        "component": "image_output", "request_model": model,
        "reference_model": canonical, "reference_source": "openai",
        "source": GUIDE, "live_verified": False,
        "dimension_basis": "requested_dimensions",
        "requires_decoded_dimensions": True,
        "requires_model_identity_evidence": True,
        "excludes": ["input_text", "input_image", "mainline_text", "mainline_reasoning", "streaming_previews"],
        "missing_usage_policy": "unverified_not_zero",
    }
    if rejected:
        return {**reference, "status": "not_applicable_expected_rejection", "expectation": None}
    if size is None:
        return {**reference, "status": "deferred_until_decoded_size", "expectation": None}
    width, height = size
    expectation = image_output_token_expectation(
        canonical, {"size": f"{width}x{height}", "quality": quality, "n": 1},
        [{"width": width, "height": height}], reference_source="openai",
    )
    if expectation is None:
        raise ValueError("Authored GPT reference has no exact-model output token expectation")
    return {**reference, "status": "documented_auto_quality_candidates" if quality == "auto" else "documented_reference",
            "expectation": expectation}


def gpt_image_resolution_quality_cases(
    model: str, *, api_form: str = "openai_images_generations",
    include_auto: bool = True, include_negative: bool = True,
) -> list[ImageTestCase]:
    """Cross every authored fixed size with the model's discrete qualities.

    ``include_auto`` adds quality=auto at every size, size=auto at every
    quality, and auto/auto. Those observations retain acceptance and decoded
    output requirements without predicting the selected size or quality.
    Negative boundaries are appended separately and change one field each.
    Images edits use the existing public GPT square fixture at execution time;
    this factory generates no image bytes and performs no network or file I/O.
    """
    canonical = canonical_model(model)
    if api_form not in supported_api_forms(model):
        raise ValueError(f"No authored GPT image reference interface: {model!r} / {api_form!r}")
    operation, path, api_reference = API_FORMS[api_form]
    qualities = _EXTENDED_QUALITIES if canonical in CANONICAL_MODELS[-2:] else _BASE_QUALITIES
    selected_qualities = (*qualities, "auto") if include_auto else qualities
    references = [GUIDE, PROMPTING_GUIDE, api_reference]
    cases: list[ImageTestCase] = []

    def add(tier: str, label: str, size: str, quality: str, *, reject_field: str | None = None) -> None:
        rejected = reject_field is not None
        auto = not rejected and (size == "auto" or quality == "auto")
        dimensions = tuple(map(int, size.split("x"))) if size != "auto" else None
        suffix = f"{tier.lower()}_{label}__size_{size}__quality_{quality}"
        name = f"gpt_rq_{model.replace('-', '_').replace('.', '_')}_{operation}_{suffix}"
        image_parameters = {"size": size, "quality": quality, "output_format": "png"}
        if operation == "responses":
            tool = {"type": "image_generation", "model": model, "action": "generate", **image_parameters}
            parameters = {"model": MAINLINE_MODEL, "input": PROMPT, "tools": [tool],
                          "tool_choice": {"type": "image_generation"}, "reasoning": {"effort": "low"},
                          "max_output_tokens": 4096, "store": False, "stream": False}
        else:
            parameters = {**image_parameters, "n": 1}
        aspect = None
        if dimensions is not None:
            divisor = gcd(*dimensions)
            aspect = f"{dimensions[0] // divisor}:{dimensions[1] // divisor}"
        metadata = {
            "matrix_id": MATRIX_ID, "image_reference_candidate": True,
            "gpt_resolution_quality": True, "reference_observation": True,
            "pin_parameters": True, "case_id": f"{model}/{operation}/{MATRIX_ID}/{suffix}",
            "test_profile": name, "request_model": model, "reference_model": canonical,
            "source_id": "openai", "reference_source": "openai", "source": GUIDE,
            "official_references": references.copy(), "source_checked_at": SOURCE_CHECKED_AT,
            "api_form": api_form, "endpoint": "https://api.openai.com/v1" + path,
            "operation": operation, "resolution_tier": tier,
            "resolution_tier_is_official_enum": False,
            "resolution_tier_policy": "project_representative_groups_native_size_on_wire",
            "requested_size": size, "requested_quality": quality, "requested_aspect_ratio": aspect,
            "parameter_group": "negative_boundary" if rejected else "auto_observation" if auto else "positive_boundary" if tier == "boundary" else "resolution_quality_cross",
            "rejection_parameter": reject_field, "expected_count": 0 if rejected else 1,
            "acceptance_required": not rejected, "decoded_image_required": not rejected,
            "quality_acceptance_is_visual_quality_proof": False,
            "experimental_size": bool(dimensions and dimensions[0] * dimensions[1] > 2560 * 1440),
            "experimental_size_basis": "project_pixel_count_interpretation_of_above_2560x1440",
            "token_expectation": _token_reference(model, dimensions, quality, rejected=rejected),
            "prompt": EDIT_PROMPT if operation == "edit" else PROMPT,
        }
        if operation == "edit":
            metadata.update({"input_image_count": 1, "input_fixture": "gpt_image_25_square_png_v1"})
        if operation == "responses":
            metadata.update({"tool_model": model, "mainline_model": MAINLINE_MODEL, "input_kind": "text"})
        cases.append(ImageTestCase(
            name=name, parameters=parameters,
            model_override=MAINLINE_MODEL if operation == "responses" else model,
            expected_outcome="rejection" if rejected else "observation" if auto else "success",
            expected_size=None if rejected else dimensions,
            expected_format=None if rejected else "PNG",
            description=f"{model} {operation}: native size={size}, quality={quality}; {label.replace('_', ' ')}.",
            tags=(MATRIX_ID, operation, metadata["parameter_group"]), metadata=metadata,
        ))

    sizes = _LEGACY_SIZES if canonical in _LEGACY_MODELS else _FLEXIBLE_SIZES
    for tier, label, width, height in sizes:
        for quality in selected_qualities:
            add(tier, label, f"{width}x{height}", quality)
    if include_auto:
        for quality in selected_qualities:
            add("auto", "model_selected", "auto", quality)
    if include_negative:
        negative_sizes = (
            (("not_in_legacy_size_enum", "1024x768"), ("unsupported_2k", "2048x2048"),
             ("unsupported_4k", "3840x2160")) if canonical in _LEGACY_MODELS else
            (("size_grid", "1025x1024"), ("size_aspect", "1552x512"),
             ("size_min_pixels", "1024x624"), ("size_max_pixels", "3072x3072"),
             ("size_max_edge", "3856x2048"))
        )
        for label, size in negative_sizes:
            add("negative", "reject_" + label, size, "low", reject_field="size")
        negative_qualities = ("ultra",) if qualities == _EXTENDED_QUALITIES else ("xhigh", "max", "ultra")
        for quality in negative_qualities:
            add("negative", "reject_quality", "1024x1024", quality, reject_field="quality")
    return cases


def gpt_image_resolution_quality_responses_cases(
    model: str, *, include_auto: bool = True, include_negative: bool = True,
) -> list[dict[str, Any]]:
    """Expose raw Responses rows for an independently admitted reference runner.

    This intentionally does not call or alter the closed GPT 2.5 runner's
    exact-matrix validator. Each row preserves mainline and tool identities.
    """
    result = []
    for case in gpt_image_resolution_quality_cases(
        model, api_form="openai_responses", include_auto=include_auto, include_negative=include_negative,
    ):
        row = {
            "name": case.name, "case_id": case.metadata["case_id"], "tool_model": model,
            "mainline_model": MAINLINE_MODEL, "api_form": "openai_responses",
            "endpoint": case.metadata["endpoint"], "parameters": copy.deepcopy(case.parameters["tools"][0]),
            "input_kind": "text", "expected_outcome": case.expected_outcome,
            "stream": False, "expected_count": case.metadata["expected_count"],
            "description": case.description, "official_references": case.metadata["official_references"].copy(),
            "metadata": copy.deepcopy(case.metadata),
        }
        if case.metadata["rejection_parameter"]:
            row["expected_rejection_field"] = case.metadata["rejection_parameter"]
        result.append(row)
    return result
