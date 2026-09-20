"""Source-scoped image output token ranges; never infer tokens from file bytes.

Google sources checked 2026-09-09; OpenAI calculator sources checked 2026-09-08.
These are output-image estimates, excluding text and thinking. Pixel references
and comparison tolerances do not establish image size or aspect-ratio support.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .gpt_image_25_tokens import (
    CALCULATOR_URL as GPT_IMAGE_25_CALCULATOR,
    MODEL_IDS as GPT_IMAGE_25_MODELS,
    QUALITY_GRID as GPT_IMAGE_25_QUALITY_GRID,
    gpt_image_25_output_tokens,
)

OPENAI_IMAGE_GUIDE = "https://developers.openai.com/api/docs/guides/image-generation"
OPENAI_CALCULATOR = (
    "https://developers.openai.com/_astro/"
    "GptImage2TokenCalculator.react.Bi5Ri9Qv.js"
)
GOOGLE_IMAGE_GUIDE = "https://ai.google.dev/gemini-api/docs/image-generation"
GOOGLE_PRICING = "https://ai.google.dev/gemini-api/docs/pricing"
GOOGLE_VERTEX_PRICING = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
_GOOGLE_VERTEX_DIMENSION_SOURCES = {
    "gemini-3.1-flash-image": "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-image",
    "gemini-3.1-flash-lite-image": "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-lite-image",
    "gemini-3-pro-image": "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-pro-image",
}
IMAGE_TOKEN_RELATIVE_TOLERANCE = 0.10
IMAGE_TOKEN_ABSOLUTE_TOLERANCE = 8

_GOOGLE_1K = (
    (1024, 1024), (512, 2048), (384, 3072), (848, 1264),
    (1264, 848), (896, 1200), (2048, 512), (1200, 896),
    (928, 1152), (1152, 928), (3072, 384), (768, 1376),
    (1376, 768), (1584, 672),
)
_GOOGLE_512 = (
    (512, 512), (256, 1024), (192, 1536), (424, 632),
    (632, 424), (448, 600), (1024, 256), (600, 448),
    (464, 576), (576, 464), (1536, 192), (384, 688),
    (688, 384), (792, 168),
)
_GOOGLE_PRO_1K = tuple(
    size for size in _GOOGLE_1K
    if size not in {(512, 2048), (384, 3072), (2048, 512), (3072, 384)}
)
_GOOGLE_25 = {
    (1024, 1024), (832, 1248), (1248, 832), (864, 1184),
    (1184, 864), (896, 1152), (1152, 896), (768, 1344),
    (1344, 768), (1536, 672),
}
_LEGACY_OPENAI = {
    (1024, 1024): {"low": 272, "medium": 1056, "high": 4160},
    (1024, 1536): {"low": 408, "medium": 1584, "high": 6240},
    (1536, 1024): {"low": 400, "medium": 1568, "high": 6208},
}


def gpt_image_2_output_tokens(width: int, height: int, quality: str) -> int | None:
    """Evaluate the official calculator with JavaScript's positive half-up round."""
    if not _valid_size(width, height):
        return None
    pixels = width * height
    longest, shortest = max(width, height), min(width, height)
    grid = {"low": 16, "medium": 48, "high": 96}.get(quality)
    if (
        grid is None or width % 16 or height % 16
        or not 655_360 <= pixels <= 8_294_400
        or longest > 3840 or longest > shortest * 3
    ):
        return None
    short_grid = (2 * grid * shortest + longest) // (2 * longest)
    numerator = grid * short_grid * (2_000_000 + pixels)
    return (numerator + 3_999_999) // 4_000_000


def image_output_token_expectation(
    model: str,
    request_body: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    reference_source: str | None = None,
    observed_partial_images: int | None = None,
) -> dict[str, Any] | None:
    """Return a bounded image-component expectation for verified decoded images.

    Use an exact official model ID or a separately resolved MPDB reference model,
    never a guessed provider alias. Unsupported models, resolution tiers, or
    source bindings return None. Decoded-pixel approximations validate quantity
    only; the caller must evaluate the requested size/aspect contract separately.
    """
    if not images:
        return None
    canonical = model.removeprefix("models/")
    source = str(reference_source or "").strip().lower()
    requested_n = request_body.get("n")
    if requested_n is not None and (
        not isinstance(requested_n, int) or isinstance(requested_n, bool)
        or requested_n < 1 or requested_n != len(images)
    ):
        return None
    openai_models = {
        "gpt-image-2", "gpt-image-1", "gpt-image-1.5",
        "gpt-image-1-mini",
    }
    google_models = {
        "gemini-2.5-flash-image", "gemini-3-pro-image",
        "gemini-3-pro-image-preview", "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview", "gemini-3.1-flash-lite-image",
    }
    if canonical in openai_models or canonical in GPT_IMAGE_25_MODELS:
        if source and source not in {"openai", "azure_openai"}:
            return None
        quality = str(request_body.get("quality") or "auto").lower()
        available_qualities = tuple(GPT_IMAGE_25_QUALITY_GRID) if canonical in GPT_IMAGE_25_MODELS else ("low", "medium", "high")
        qualities = available_qualities if quality == "auto" else (quality,)
        evidence_source = OPENAI_IMAGE_GUIDE
        dimension_source = OPENAI_IMAGE_GUIDE
        calculator = (
            gpt_image_25_output_tokens if canonical in GPT_IMAGE_25_MODELS
            else gpt_image_2_output_tokens if canonical == "gpt-image-2" else None
        )
        if calculator and not _valid_openai_requested_size(request_body, calculator, qualities[0]):
            return None
    elif canonical in google_models:
        if source and source not in {"google_ai_studio", "google_vertex", "gemini"}:
            return None
        qualities = ("image",)
        evidence_source = GOOGLE_VERTEX_PRICING if source == "google_vertex" else GOOGLE_PRICING
        # These Vertex model pages document resolution tiers, not the shared
        # AI Studio guide's exact pixel table. Preserve both source roles below.
        dimension_source = (
            _GOOGLE_VERTEX_DIMENSION_SOURCES.get(canonical, GOOGLE_IMAGE_GUIDE)
            if source == "google_vertex" else GOOGLE_IMAGE_GUIDE
        )
    else:
        return None

    preview_tokens = 0
    if canonical in GPT_IMAGE_25_MODELS and request_body.get("stream") is True:
        if (not isinstance(observed_partial_images, int) or isinstance(observed_partial_images, bool)
                or observed_partial_images < 0):
            return None
        requested_partials = request_body.get("partial_images", 0)
        if (not isinstance(requested_partials, int) or isinstance(requested_partials, bool)
                or not 0 <= requested_partials <= 3
                or observed_partial_images > requested_partials * len(images)):
            return None
        preview_tokens = observed_partial_images * 100
    possible_totals = {preview_tokens}
    image_estimates: list[dict[str, Any]] = []
    for info in images:
        width, height = info.get("width"), info.get("height")
        if not _valid_size(width, height):
            return None
        candidates: list[int] = []
        reference: dict[str, Any] | None = None
        for quality in qualities:
            if canonical in GPT_IMAGE_25_MODELS or canonical == "gpt-image-2":
                result = _openai_image_reference(width, height, quality, request_body, calculator)
                count, reference = result if result is not None else (None, None)
            elif canonical in openai_models:
                count = _LEGACY_OPENAI.get((width, height), {}).get(quality)
                reference = _pixel_reference(width, height, width, height, "official_resolution_table")
            else:
                result = _google_image_reference(canonical, width, height)
                count, reference = result if result is not None else (None, None)
            if count is None:
                return None
            candidates.append(count)
        reference["nominal_token_options"] = sorted(set(candidates))
        reference["dimension_source"] = dimension_source
        if canonical in google_models and reference["basis"] == "official_resolution_table":
            reference["reference_dimension_source"] = GOOGLE_IMAGE_GUIDE
        image_estimates.append(reference)
        possible_totals = {prefix + count for prefix in possible_totals for count in candidates}
        # Keep automatic-quality validation finite without collapsing distinct
        # published qualities into one permissive continuous range.
        if len(possible_totals) > 4096:
            return None

    expected_min, expected_max = min(possible_totals), max(possible_totals)
    ranges = [
        {
            "tokens": total,
            "min": max(1, total - max(IMAGE_TOKEN_ABSOLUTE_TOLERANCE, math.ceil(total * IMAGE_TOKEN_RELATIVE_TOLERANCE))),
            "max": total + max(IMAGE_TOKEN_ABSOLUTE_TOLERANCE, math.ceil(total * IMAGE_TOKEN_RELATIVE_TOLERANCE)),
        }
        for total in sorted(possible_totals)
    ]

    low_slack = max(
        IMAGE_TOKEN_ABSOLUTE_TOLERANCE,
        math.ceil(expected_min * IMAGE_TOKEN_RELATIVE_TOLERANCE),
    )
    high_slack = max(
        IMAGE_TOKEN_ABSOLUTE_TOLERANCE,
        math.ceil(expected_max * IMAGE_TOKEN_RELATIVE_TOLERANCE),
    )
    return {
        "min": max(1, expected_min - low_slack),
        "max": expected_max + high_slack,
        "tokens": expected_min if expected_min == expected_max else None,
        "nominal_min": expected_min,
        "nominal_max": expected_max,
        "ranges": ranges,
        "component": "image",
        "source": evidence_source,
        "dimension_source": dimension_source,
        "calculator_source": (GPT_IMAGE_25_CALCULATOR if canonical in GPT_IMAGE_25_MODELS
                              else OPENAI_CALCULATOR if canonical == "gpt-image-2" else None),
        "source_checked_at": "2026-09-09" if canonical in google_models else "2026-09-08",
        "reference_model": canonical,
        "reference_source": source or None,
        "identity_evidence": "caller-declared model and source; identity is validated separately",
        "image_count": len(images),
        "streamed_preview_tokens": preview_tokens,
        "evidence_level": "official_range",
        "quantity_is_estimate": True,
        "dimension_contract_support": "not_evaluated",
        "estimate_basis": (
            image_estimates[0]["basis"]
            if len({item["basis"] for item in image_estimates}) == 1 else "mixed"
        ),
        "image_estimates": image_estimates,
        "relative_tolerance": IMAGE_TOKEN_RELATIVE_TOLERANCE,
        "absolute_tolerance": IMAGE_TOKEN_ABSOLUTE_TOLERANCE,
    }


def _valid_size(width: Any, height: Any) -> bool:
    return all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (width, height)
    )


def _pixel_reference(width: int, height: int, reference_width: int, reference_height: int, basis: str) -> dict[str, Any]:
    return {
        "basis": basis,
        "actual_dimensions": {"width": width, "height": height},
        "reference_dimensions": {"width": reference_width, "height": reference_height},
    }


def _valid_openai_requested_size(request_body: dict[str, Any], calculator: Callable[[int, int, str], int | None], quality: str) -> bool:
    requested = request_body.get("size")
    if requested is None or requested == "auto":
        return True
    if not isinstance(requested, str):
        return False
    dimensions = requested.split("x")
    if len(dimensions) != 2 or not all(value.isascii() and value.isdecimal() for value in dimensions):
        return False
    return calculator(*(int(value) for value in dimensions), quality) is not None


def _openai_image_reference(width: int, height: int, quality: str, request_body: dict[str, Any], calculator: Callable[[int, int, str], int | None]) -> tuple[int, dict[str, Any]] | None:
    count = calculator(width, height, quality)
    if count is not None:
        return count, _pixel_reference(width, height, width, height, "official_calculator")
    # Approximate only decoded dimensions of explicit auto-size requests whose
    # sole geometric violation is the 16px grid. Keep the pure calculator strict.
    if (
        request_body.get("size") != "auto" or not (width % 16 or height % 16)
        or not 655_360 <= width * height <= 8_294_400
        or max(width, height) > 3840 or max(width, height) > 3 * min(width, height)
    ):
        return None
    nearby = []
    for reference_width in {width // 16 * 16, (width + 15) // 16 * 16}:
        for reference_height in {height // 16 * 16, (height + 15) // 16 * 16}:
            count = calculator(reference_width, reference_height, quality)
            if count is not None:
                nearby.append((reference_width, reference_height, count))
    if not nearby:
        return None
    reference_width, reference_height, count = min(
        nearby,
        key=lambda item: (
            (item[0] - width) ** 2 + (item[1] - height) ** 2,
            abs(item[0] * item[1] - width * height), item[0], item[1],
        ),
    )
    return count, _pixel_reference(
        width, height, reference_width, reference_height, "decoded_auto_size_nearest_16px_grid",
    )


def _google_token_tiers(model: str) -> tuple[tuple[str, int, int], ...]:
    if model == "gemini-2.5-flash-image":
        return (("1K", 1024, 1290),)
    if model == "gemini-3.1-flash-lite-image":
        return (("1K", 1024, 1120),)
    if model in {"gemini-3-pro-image", "gemini-3-pro-image-preview"}:
        return (("1K", 1024, 1120), ("2K", 2048, 1120), ("4K", 4096, 2000))
    if model in {"gemini-3.1-flash-image", "gemini-3.1-flash-image-preview"}:
        return (("512", 512, 747), ("1K", 1024, 1120), ("2K", 2048, 1680), ("4K", 4096, 2520))
    return ()


def _google_image_reference(model: str, width: int, height: int) -> tuple[int, dict[str, Any]] | None:
    count = _google_image_tokens(model, width, height)
    tiers = _google_token_tiers(model)
    if count is not None:
        tier, edge, _ = min(
            (item for item in tiers if item[2] == count),
            key=lambda item: abs(width * height - item[1] ** 2),
        )
        reference = _pixel_reference(width, height, width, height, "official_resolution_table")
    else:
        # Pixel area identifies a nominal token tier only. It neither promises
        # support for the decoded aspect ratio nor verifies the requested size.
        matches = [
            item for item in tiers
            if abs(width * height - item[1] ** 2) <= item[1] ** 2 * IMAGE_TOKEN_RELATIVE_TOLERANCE
        ]
        if len(matches) != 1:
            return None
        tier, edge, count = matches[0]
        reference = _pixel_reference(width, height, edge, edge, "decoded_pixel_area")
        reference["relative_pixel_area_difference"] = abs(width * height - edge ** 2) / edge ** 2
        reference["pixel_area_relative_tolerance"] = IMAGE_TOKEN_RELATIVE_TOLERANCE
    reference["nominal_resolution"] = tier
    reference["nominal_pixel_area"] = edge ** 2
    return count, reference


def _google_image_tokens(model: str, width: int, height: int) -> int | None:
    size = (width, height)
    if model == "gemini-2.5-flash-image":
        return 1290 if size in _GOOGLE_25 else None
    if model == "gemini-3.1-flash-lite-image":
        return 1120 if size in _GOOGLE_PRO_1K else None
    if model in {"gemini-3-pro-image", "gemini-3-pro-image-preview"}:
        base, levels = _GOOGLE_PRO_1K, ((1, 1120), (2, 1120), (4, 2000))
    else:
        if size in _GOOGLE_512:
            return 747
        base, levels = _GOOGLE_1K, ((1, 1120), (2, 1680), (4, 2520))
    for scale, tokens in levels:
        if any(size == (w * scale, h * scale) for w, h in base):
            return tokens
    return None
