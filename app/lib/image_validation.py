from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .param_outcome import (
    EXPECTED_REJECTION_STATUS_CODES,
    map_probe_outcome,
)
from .reference_specs import (
    load_model_capability_profile,
    resolve_profile_expectation,
)


GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3_840
GPT_IMAGE_2_MAX_ASPECT_RATIO = 3.0
GROK_IMAGINE_DIMENSIONS = {
    ("1k", "1:1"): (1024, 1024),
    ("2k", "1:1"): (2048, 2048),
}


@dataclass(frozen=True)
class ImageTestCase:
    name: str
    parameters: dict[str, Any]
    model_override: str | None = None
    expected_outcome: str = "success"
    expected_size: tuple[int, int] | None = None
    expected_format: str | None = "PNG"
    description: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def request_body(self, model: str, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model_override or model,
            "prompt": prompt,
            **self.parameters,
        }

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.expected_size is not None:
            payload["expected_size"] = list(self.expected_size)
        payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int | None
    height: int | None
    byte_length: int
    sha256: str
    has_alpha: bool | None = None
    visual_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def pixel_count(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        return self.width * self.height

    def public(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "pixel_count": self.pixel_count,
        }


def gpt_image_2_cases(
    suite: str = "smoke",
    *,
    include_4k: bool = False,
    include_negative: bool = True,
) -> list[ImageTestCase]:
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError(f"Unsupported image test suite: {suite!r}")

    cases = [
        _positive_case(
            "baseline_1024_square",
            "1024x1024",
            "Baseline square image; verifies response decoding, format, and exact pixels.",
            tags=("baseline", "resolution"),
        )
    ]
    if suite == "smoke":
        return cases

    cases.extend(
        [
            _positive_case(
                "standard_portrait",
                "1024x1536",
                "Standard portrait size.",
                tags=("resolution", "standard"),
            ),
            _positive_case(
                "arbitrary_landscape",
                "1536x864",
                "Valid arbitrary resolution with both edges divisible by 16.",
                tags=("resolution", "arbitrary"),
            ),
            _positive_case(
                "square_2k",
                "2048x2048",
                "Four-times-pixel-count comparison against the 1024 square baseline.",
                tags=("resolution", "high_resolution", "postprocess_signal"),
            ),
            _gpt_image_parameter_case(
                "batch_n2_1024_square",
                {"n": 2},
                "Generate two images and require both payloads to decode at the requested size.",
                tags=("batch", "count"),
            ),
            _gpt_image_parameter_case(
                "background_auto",
                {"background": "auto"},
                "Exercise GPT Image 2's supported automatic background mode.",
                tags=("background",),
            ),
            _gpt_image_parameter_case(
                "moderation_low",
                {"moderation": "low"},
                "Exercise GPT Image 2's documented low moderation setting.",
                tags=("moderation",),
            ),
            _gpt_image_parameter_case(
                "jpeg_compression_50",
                {"output_compression": 50},
                "Request JPEG output with explicit compression.",
                tags=("format", "compression"),
                output_format="jpeg",
            ),
        ]
    )

    if include_4k or suite == "full":
        if not include_4k:
            raise ValueError("The full suite requires include_4k=True to acknowledge the 4K request.")
        cases.append(
            _positive_case(
                "landscape_4k",
                "3840x2160",
                "Maximum documented 4K landscape boundary.",
                tags=("resolution", "experimental", "postprocess_signal", "4k"),
            )
        )

    if include_negative:
        cases.extend(
            [
                _negative_case(
                    "reject_non_multiple_of_16",
                    "1537x864",
                    "Width is not divisible by 16.",
                ),
                _negative_case(
                    "reject_aspect_ratio_over_3_to_1",
                    "3072x768",
                    "The requested aspect ratio is 4:1.",
                ),
                _negative_case(
                    "reject_below_minimum_pixels",
                    "512x512",
                    "The requested pixel count is below the GPT Image 2 minimum.",
                ),
                _negative_case(
                    "reject_edge_over_3840",
                    "4096x1920",
                    "The maximum edge exceeds 3840 while other constraints remain valid.",
                ),
                _gpt_image_parameter_case(
                    "reject_transparent_background",
                    {"background": "transparent"},
                    "GPT Image 2 does not support transparent backgrounds.",
                    tags=("negative_boundary", "background"),
                    expected_outcome="rejection",
                ),
            ]
        )
    return cases


def banana_variant_cases(
    suite: str = "smoke",
    *,
    model_template: str = "nano-banana-pro-{resolution_lower}",
    include_4k: bool = False,
    include_cross_control: bool = True,
    include_negative: bool = True,
    transport: str = "images-generations",
) -> list[ImageTestCase]:
    """Build OpenAI-compatible probes for provider-specific Banana aliases.

    The provider aliases are not part of Google's native API contract, so the
    template is deliberately configurable. Square 1K/2K/4K expectations follow
    the documented Gemini 3 image output matrix. Cross-control cases swap the
    requested ``size`` and alias suffix to reveal which input actually controls
    the returned pixels.
    """
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError(f"Unsupported image test suite: {suite!r}")
    if transport not in {
        "images-generations",
        "chat-completions",
        "gemini-interactions",
    }:
        raise ValueError(f"Unsupported Banana image transport: {transport!r}")
    uses_resolution_alias = (
        "{resolution}" in model_template or "{resolution_lower}" in model_template
    )
    if not uses_resolution_alias and suite != "smoke" and include_cross_control:
        raise ValueError(
            "A fixed Banana model requires include_cross_control=False because it has no resolution alias to cross-check."
        )
    if suite == "full" and not include_4k:
        raise ValueError("The full suite requires include_4k=True to acknowledge the 4K request.")

    cases = [
        _banana_case(
            "banana_1k_aligned",
            model_template,
            model_resolution="1K",
            requested_resolution="1K",
            expected_size=(1024, 1024),
            control_probe="aligned",
        )
    ]
    if suite == "smoke":
        return cases

    cases.append(
        _banana_case(
            "banana_2k_aligned",
            model_template,
            model_resolution="2K",
            requested_resolution="2K",
            expected_size=(2048, 2048),
            control_probe="aligned",
        )
    )
    latest_flash_image = model_template in {
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-image-preview",
    }
    if latest_flash_image and transport in {
        "chat-completions",
        "gemini-interactions",
    }:
        cases.extend(
            [
                _banana_case(
                    "banana_512_square",
                    model_template,
                    model_resolution="512",
                    requested_resolution="512",
                    expected_size=(512, 512),
                    control_probe="official_parameter",
                ),
                _banana_case(
                    "banana_1k_landscape_16_9",
                    model_template,
                    model_resolution="1K",
                    requested_resolution="1K",
                    expected_size=(1376, 768),
                    control_probe="official_parameter",
                    aspect_ratio="16:9",
                ),
            ]
        )
        if include_negative:
            cases.extend(
                [
                    _banana_case(
                        "banana_reject_lowercase_1k",
                        model_template,
                        model_resolution="1K",
                        requested_resolution="1k",
                        expected_size=None,
                        control_probe="negative_parameter",
                        expected_outcome="rejection",
                    ),
                    _banana_case(
                        "banana_reject_aspect_ratio_7_5",
                        model_template,
                        model_resolution="1K",
                        requested_resolution="1K",
                        expected_size=None,
                        control_probe="negative_parameter",
                        aspect_ratio="7:5",
                        expected_outcome="rejection",
                    ),
                ]
            )
    if include_cross_control:
        cases.extend(
            [
                _banana_case(
                    "banana_model_1k_request_2k",
                    model_template,
                    model_resolution="1K",
                    requested_resolution="2K",
                    expected_size=None,
                    control_probe="crossed",
                ),
                _banana_case(
                    "banana_model_2k_request_1k",
                    model_template,
                    model_resolution="2K",
                    requested_resolution="1K",
                    expected_size=None,
                    control_probe="crossed",
                ),
            ]
        )
    if include_4k:
        cases.append(
            _banana_case(
                "banana_4k_aligned",
                model_template,
                model_resolution="4K",
                requested_resolution="4K",
                expected_size=(4096, 4096),
                control_probe="aligned",
            )
        )
    return cases


def grok_imagine_cases(
    suite: str = "smoke",
    *,
    include_2k: bool = False,
    include_negative: bool = True,
) -> list[ImageTestCase]:
    """Build xAI Grok Imagine generation parameter probes.

    Grok uses ``aspect_ratio`` and the logical ``resolution`` tiers ``1k`` and
    ``2k`` rather than OpenAI's ``size`` field. The transport format
    (``url``/``b64_json``) is independent from the encoded image format, so
    successful cases accept any structurally valid PNG, JPEG, or WebP payload.
    """
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError(f"Unsupported image test suite: {suite!r}")
    if suite == "full" and not include_2k:
        raise ValueError(
            "The Grok full suite requires include_2k=True to acknowledge billable 2K requests."
        )

    cases = [
        _grok_case(
            "grok_1k_square_b64",
            aspect_ratio="1:1",
            resolution="1k",
            description=(
                "Baseline Grok Imagine request; verifies b64_json decoding and exact 1K square pixels."
            ),
            tags=("baseline", "resolution", "delivery_b64"),
        )
    ]
    if suite == "smoke":
        return cases

    cases.extend(
        [
            _grok_case(
                "grok_1k_landscape_16_9",
                aspect_ratio="16:9",
                resolution="1k",
                description="Official 16:9 aspect-ratio probe at the 1K tier.",
                tags=("resolution", "aspect_ratio", "landscape"),
            ),
            _grok_case(
                "grok_1k_portrait_9_16",
                aspect_ratio="9:16",
                resolution="1k",
                description="Official 9:16 aspect-ratio probe at the 1K tier.",
                tags=("resolution", "aspect_ratio", "portrait"),
            ),
            _grok_case(
                "grok_1k_batch_n2",
                aspect_ratio="1:1",
                resolution="1k",
                n=2,
                description="Official n=2 batch probe; both images must decode and match 1K square pixels.",
                tags=("batch", "count", "delivery_b64"),
            ),
            _grok_case(
                "grok_1k_square_url",
                aspect_ratio="1:1",
                resolution="1k",
                response_format="url",
                description="Official URL-delivery probe; the temporary URL must be downloadable and decodable.",
                tags=("delivery_url", "resolution"),
            ),
        ]
    )

    if include_2k:
        cases.extend(
            [
                _grok_case(
                    "grok_2k_square_b64",
                    aspect_ratio="1:1",
                    resolution="2k",
                    description="Billable 2K square correspondence probe.",
                    tags=("resolution", "high_resolution", "postprocess_signal", "2k"),
                ),
                _grok_case(
                    "grok_2k_landscape_16_9",
                    aspect_ratio="16:9",
                    resolution="2k",
                    description="Billable 2K 16:9 correspondence probe.",
                    tags=("resolution", "high_resolution", "postprocess_signal", "2k"),
                ),
                _grok_case(
                    "grok_2k_portrait_9_16",
                    aspect_ratio="9:16",
                    resolution="2k",
                    description="Billable 2K 9:16 correspondence probe.",
                    tags=("resolution", "high_resolution", "postprocess_signal", "2k"),
                ),
            ]
        )

    if include_negative:
        cases.extend(
            [
                _grok_negative_case(
                    "grok_reject_aspect_ratio_7_5",
                    {"aspect_ratio": "7:5"},
                    "Reject an aspect ratio outside xAI's documented enum.",
                ),
                _grok_negative_case(
                    "grok_reject_resolution_4k",
                    {"resolution": "4k"},
                    "Reject the unsupported 4K resolution tier.",
                ),
            ]
        )

    if suite == "full" and include_negative:
        cases.append(
            _grok_negative_case(
                "grok_reject_n11",
                {"n": 11},
                "Reject a batch larger than the documented maximum of 10 images.",
            )
        )
    return cases


def validate_gpt_image_2_size(size: str) -> list[str]:
    if size == "auto":
        return []
    match = re.fullmatch(r"(\d+)x(\d+)", str(size))
    if not match:
        return ["size must be auto or WIDTHxHEIGHT"]
    width, height = int(match.group(1)), int(match.group(2))
    failures: list[str] = []
    if width % 16 or height % 16:
        failures.append("width and height must both be divisible by 16")
    if max(width, height) > GPT_IMAGE_2_MAX_EDGE:
        failures.append(f"maximum edge must not exceed {GPT_IMAGE_2_MAX_EDGE}")
    pixel_count = width * height
    if pixel_count < GPT_IMAGE_2_MIN_PIXELS:
        failures.append(f"pixel count must be at least {GPT_IMAGE_2_MIN_PIXELS}")
    if pixel_count > GPT_IMAGE_2_MAX_PIXELS:
        failures.append(f"pixel count must not exceed {GPT_IMAGE_2_MAX_PIXELS}")
    if min(width, height) <= 0 or max(width, height) / min(width, height) > GPT_IMAGE_2_MAX_ASPECT_RATIO:
        failures.append("aspect ratio must be between 1:3 and 3:1")
    return failures


def inspect_image_bytes(raw: bytes, *, visual_forensics: bool = True) -> ImageInfo:
    if not raw:
        raise ValueError("image payload is empty")

    image_format, width, height, has_alpha = _read_image_header(raw)
    visual_metrics = analyze_visual_detail(raw) if visual_forensics else {
        "available": False,
        "reason": "disabled",
    }
    return ImageInfo(
        format=image_format,
        width=width,
        height=height,
        byte_length=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        has_alpha=has_alpha,
        visual_metrics=visual_metrics,
    )


def apply_capability_expectations(
    cases: list[ImageTestCase],
    *,
    family: str,
    model: str,
    modality: str = "image",
    api_form: str | None = None,
    route_profile: str | None = None,
) -> list[ImageTestCase]:
    """Overlay model_capability_profiles expectations onto image cases.

    unsupported → expected_outcome=rejection
    supported keeps the authored success/observation outcome; an explicit
    supported override can flip a negative probe back to success.
    """
    capability = load_model_capability_profile(
        modality,
        family,
        model,
        api_form=api_form,
        route_profile=route_profile,
    )
    if (
        capability.get("known_model") is not True
        or capability.get("known_api_profile") is not True
        or capability.get("route_profile_known") is not True
    ):
        raise KeyError(
            f"Missing registered {modality} model/API profile for "
            f"{family}/{capability.get('api_form') or api_form}/{model}."
        )

    updated: list[ImageTestCase] = []
    for case in cases:
        expectation = resolve_profile_expectation(
            modality,
            family,
            model,
            case.name,
            capability_profile=capability,
        )
        metadata = dict(case.metadata)
        metadata["expectation"] = expectation
        metadata["capability_family"] = family
        metadata["capability_model"] = model
        metadata["capability_api_form"] = capability.get("api_form")
        metadata["capability_route_profile"] = capability.get("route_profile")
        if expectation == "unsupported":
            updated.append(
                ImageTestCase(
                    name=case.name,
                    parameters=dict(case.parameters),
                    model_override=case.model_override,
                    expected_outcome="rejection",
                    expected_size=None,
                    expected_format=None,
                    description=case.description,
                    tags=tuple(case.tags),
                    metadata=metadata,
                )
            )
            continue
        expected_outcome = case.expected_outcome
        if case.expected_outcome == "rejection":
            expected_outcome = "success"
        updated.append(
            ImageTestCase(
                name=case.name,
                parameters=dict(case.parameters),
                model_override=case.model_override,
                expected_outcome=expected_outcome,
                expected_size=case.expected_size,
                expected_format=case.expected_format,
                description=case.description,
                tags=tuple(case.tags),
                metadata=metadata,
            )
        )
    return updated


def evaluate_case(
    case: ImageTestCase,
    *,
    status_code: int | None,
    images: Iterable[ImageInfo] = (),
    usage: dict[str, Any] | None = None,
    latency_ms: float | None = None,
    error: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    image_list = list(images)
    failures: list[str] = []
    expectation = str(
        (case.metadata or {}).get("expectation")
        or ("unsupported" if case.expected_outcome == "rejection" else "supported")
    )

    if case.expected_outcome == "observation":
        rejected = status_code in EXPECTED_REJECTION_STATUS_CODES
        successful = status_code is not None and 200 <= status_code <= 299
        if not rejected and not successful:
            failures.append("cross_control_probe_failed")
        if successful:
            expected_count = int(case.parameters.get("n") or 1)
            if len(image_list) != expected_count:
                failures.append(
                    f"output_count_mismatch:expected={expected_count}:actual={len(image_list)}"
                )
            for index, image in enumerate(image_list):
                if case.expected_format and image.format.upper() != case.expected_format.upper():
                    failures.append(
                        f"format_mismatch:index={index}:"
                        f"expected={case.expected_format}:actual={image.format}"
                    )
        passed = not failures
        return {
            "case": case.name,
            "pass": passed,
            "status": "observed" if passed else "fail",
            "expectation": expectation,
            "verification_level": "diagnostic_observation" if passed else "none",
            "status_code": status_code,
            "requested": dict(case.parameters),
            "actual_images": [image.public() for image in image_list],
            "usage": usage or {},
            "latency_ms": latency_ms,
            "failures": failures,
            "error": error,
            "tags": list(case.tags),
            "metadata": dict(case.metadata),
        }

    if case.expected_outcome == "rejection":
        outcome = map_probe_outcome("unsupported", status_code=status_code, validation_ok=True)
        if not outcome["pass"]:
            if status_code is not None and 200 <= status_code <= 299:
                failures.append("invalid_parameter_accepted")
            else:
                failures.append("expected_parameter_rejection")
        return {
            "case": case.name,
            "pass": bool(outcome["pass"]),
            "status": str(outcome["status"]),
            "expectation": "unsupported",
            "verification_level": "expected_rejection" if outcome["pass"] else "none",
            "status_code": status_code,
            "requested": dict(case.parameters),
            "actual_images": [image.public() for image in image_list],
            "usage": usage or {},
            "latency_ms": latency_ms,
            "failures": failures,
            "error": error,
            "tags": list(case.tags),
            "metadata": dict(case.metadata),
        }

    content_failures: list[str] = []
    if status_code is not None and 200 <= status_code <= 299:
        expected_count = int(case.parameters.get("n") or 1)
        if len(image_list) != expected_count:
            content_failures.append(
                f"output_count_mismatch:expected={expected_count}:actual={len(image_list)}"
            )
        for index, image in enumerate(image_list):
            if case.expected_format and image.format.upper() != case.expected_format.upper():
                content_failures.append(
                    f"format_mismatch:index={index}:expected={case.expected_format}:actual={image.format}"
                )
            if case.expected_size and (image.width, image.height) != case.expected_size:
                content_failures.append(
                    "dimension_mismatch:index={}:expected={}x{}:actual={}x{}".format(
                        index,
                        case.expected_size[0],
                        case.expected_size[1],
                        image.width,
                        image.height,
                    )
                )
            expected_aspect_ratio = case.metadata.get("expected_aspect_ratio")
            if expected_aspect_ratio and not _matches_aspect_ratio(
                image.width,
                image.height,
                str(expected_aspect_ratio),
            ):
                content_failures.append(
                    "aspect_ratio_mismatch:index={}:expected={}:actual={}x{}".format(
                        index,
                        expected_aspect_ratio,
                        image.width,
                        image.height,
                    )
                )

    validation_ok = (
        status_code is not None
        and 200 <= status_code <= 299
        and not content_failures
    )
    outcome = map_probe_outcome(
        "supported",
        status_code=status_code,
        validation_ok=validation_ok,
    )
    failures.extend(content_failures)
    if not validation_ok and not content_failures:
        if str(outcome["status"]) == "incompatible":
            failures.append("parameter_rejected")
        elif str(outcome["status"]) == "fail":
            failures.append("request_failed")

    verification_level = "constraint_verified" if outcome["pass"] else "response_only"

    return {
        "case": case.name,
        "pass": bool(outcome["pass"]),
        "status": str(outcome["status"]),
        "expectation": "supported",
        "verification_level": verification_level,
        "status_code": status_code,
        "requested": dict(case.parameters),
        "actual_images": [image.public() for image in image_list],
        "usage": usage or {},
        "latency_ms": latency_ms,
        "failures": failures,
        "error": error,
        "tags": list(case.tags),
        "metadata": dict(case.metadata),
    }


def infer_resolution_correspondence(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Classify whether Banana alias suffixes or request sizes control output pixels."""
    probes: list[dict[str, Any]] = []
    outcomes: list[str] = []
    aligned_failures: list[str] = []
    for result in results:
        metadata = result.get("metadata") or {}
        if metadata.get("family") != "banana":
            continue
        probe_type = metadata.get("control_probe")
        if probe_type == "aligned" and not result.get("pass"):
            aligned_failures.append(str(result.get("case")))
            continue
        if probe_type != "crossed":
            continue

        model_size = _metadata_size(metadata.get("model_expected_size"))
        requested_size = _result_requested_size(result)
        images = result.get("actual_images") or []
        first = images[0] if images and isinstance(images[0], dict) else {}
        actual_size = (
            _optional_int(first.get("width")),
            _optional_int(first.get("height")),
        )
        if result.get("status_code") in EXPECTED_REJECTION_STATUS_CODES:
            outcome = "conflict_rejected"
        elif actual_size == requested_size:
            outcome = "request_parameter_controls"
        elif actual_size == model_size:
            outcome = "model_alias_controls"
        else:
            outcome = "unclassified_output"
        outcomes.append(outcome)
        probes.append(
            {
                "case": result.get("case"),
                "model": result.get("model"),
                "status_code": result.get("status_code"),
                "model_expected_size": list(model_size) if model_size else None,
                "requested_size": list(requested_size) if requested_size else None,
                "actual_size": list(actual_size) if all(actual_size) else None,
                "outcome": outcome,
            }
        )

    distinct = sorted(set(outcomes))
    if aligned_failures:
        verdict = "unreliable_baseline"
    elif not outcomes:
        verdict = "unknown"
    elif len(distinct) == 1:
        verdict = distinct[0]
    else:
        verdict = "mixed_behavior"
    return {
        "verdict": verdict,
        "confirmed": bool(outcomes) and not aligned_failures and verdict != "mixed_behavior",
        "aligned_failures": aligned_failures,
        "probes": probes,
        "outcomes": distinct,
        "interpretation": {
            "request_parameter_controls": "returned pixels follow size even when the alias suffix conflicts",
            "model_alias_controls": "returned pixels follow the alias suffix even when size conflicts",
            "conflict_rejected": "the provider enforces correspondence by rejecting conflicting inputs",
            "mixed_behavior": "crossed probes do not share one stable resolution-control rule",
        }.get(verdict, "insufficient or unreliable evidence"),
    }


def infer_postprocess_suspicion(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for result in results:
        if not result.get("pass") or result.get("status") != "pass":
            continue
        requested_size = _result_requested_size(result)
        images = result.get("actual_images") or []
        first = images[0] if images and isinstance(images[0], dict) else {}
        actual_width = _optional_int(first.get("width"))
        actual_height = _optional_int(first.get("height"))
        if not requested_size or not actual_width or not actual_height:
            continue
        if requested_size != (actual_width, actual_height):
            continue
        usage = result.get("usage") or {}
        visual = first.get("visual_metrics") or {}
        observations.append(
            {
                "case": result.get("case"),
                "width": actual_width,
                "height": actual_height,
                "pixels": actual_width * actual_height,
                "latency_ms": _optional_float(result.get("latency_ms")),
                "output_tokens": _usage_output_tokens(usage),
                "image_bytes": _optional_int(first.get("byte_length")),
                "bytes_per_megapixel": _bytes_per_megapixel(
                    _optional_int(first.get("byte_length")), actual_width * actual_height
                ),
                "half_scale_residual_ratio": _optional_float(
                    visual.get("half_scale_residual_ratio")
                ),
                "neighbor_equal_ratio": _optional_float(
                    visual.get("neighbor_equal_ratio")
                ),
            }
        )

    observations.sort(key=lambda item: item["pixels"])
    if len(observations) < 2:
        return {
            "verdict": "unknown",
            "score": 0,
            "confirmed": False,
            "observations": observations,
            "comparisons": [],
            "evidence": ["insufficient_resolution_samples"],
            "limitations": _postprocess_limitations(),
        }

    baseline = observations[0]
    evidence: list[str] = []
    comparisons: list[dict[str, Any]] = []
    score = 0
    for item in observations[1:]:
        pixel_ratio = item["pixels"] / baseline["pixels"]
        if pixel_ratio < 3.5:
            continue
        token_ratio = _ratio(item.get("output_tokens"), baseline.get("output_tokens"))
        latency_ratio = _ratio(item.get("latency_ms"), baseline.get("latency_ms"))
        bytes_per_mp_ratio = _ratio(
            item.get("bytes_per_megapixel"), baseline.get("bytes_per_megapixel")
        )
        residual_ratio = _ratio(
            item.get("half_scale_residual_ratio"),
            baseline.get("half_scale_residual_ratio"),
        )
        pair_evidence: list[str] = []
        pair_score = 0
        if token_ratio is not None and token_ratio <= 1.15:
            pair_evidence.append("output_tokens_nearly_flat")
            pair_score += 2
        if latency_ratio is not None and latency_ratio <= 1.25:
            pair_evidence.append("latency_nearly_flat")
            pair_score += 1
        if bytes_per_mp_ratio is not None and bytes_per_mp_ratio <= 0.45:
            pair_evidence.append("bytes_per_megapixel_collapsed")
            pair_score += 1
        if residual_ratio is not None and residual_ratio <= 0.75:
            pair_evidence.append("high_resolution_detail_residual_dropped")
            pair_score += 1
        score = max(score, pair_score)
        evidence.extend(f"{item['case']}:{entry}" for entry in pair_evidence)
        comparisons.append(
            {
                "baseline_case": baseline["case"],
                "high_resolution_case": item["case"],
                "pixel_ratio": round(pixel_ratio, 4),
                "output_token_ratio": _rounded(token_ratio),
                "latency_ratio": _rounded(latency_ratio),
                "bytes_per_megapixel_ratio": _rounded(bytes_per_mp_ratio),
                "detail_residual_ratio": _rounded(residual_ratio),
                "score": pair_score,
                "evidence": pair_evidence,
            }
        )

    if score >= 4:
        verdict = "strongly_suspected"
    elif score >= 2:
        verdict = "suspected"
    else:
        verdict = "unknown"
    if not comparisons:
        evidence.append("no_comparison_reached_four_times_pixel_count")
    elif not evidence:
        evidence.append("no_postprocess_signal_crossed_configured_heuristics")
    return {
        "verdict": verdict,
        "score": score,
        "confirmed": False,
        "observations": observations,
        "comparisons": comparisons,
        "evidence": evidence,
        "thresholds": {
            "minimum_pixel_ratio": 3.5,
            "flat_output_token_ratio_max": 1.15,
            "flat_latency_ratio_max": 1.25,
            "bytes_per_megapixel_ratio_max": 0.45,
            "detail_residual_ratio_max": 0.75,
        },
        "limitations": _postprocess_limitations(),
    }


def analyze_visual_detail(raw: bytes) -> dict[str, Any]:
    try:
        from PIL import Image, ImageChops, ImageFilter, ImageStat
    except ImportError:
        return {
            "available": False,
            "reason": "Pillow is not installed; install requirements-image.txt for visual heuristics",
        }

    try:
        with Image.open(io.BytesIO(raw)) as source:
            gray = source.convert("L")
        width, height = gray.size
        if min(width, height) < 4:
            return {"available": False, "reason": "image is too small for visual heuristics"}

        down = gray.resize(
            (max(width // 2, 1), max(height // 2, 1)),
            resample=Image.Resampling.LANCZOS,
        )
        restored = down.resize(gray.size, resample=Image.Resampling.BICUBIC)
        residual = ImageChops.difference(gray, restored)
        residual_rms = float(ImageStat.Stat(residual).rms[0])
        contrast = float(ImageStat.Stat(gray).stddev[0])
        edge_mean = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0])

        horizontal = ImageChops.difference(gray.crop((1, 0, width, height)), gray.crop((0, 0, width - 1, height)))
        vertical = ImageChops.difference(gray.crop((0, 1, width, height)), gray.crop((0, 0, width, height - 1)))
        horizontal_hist = horizontal.histogram()
        vertical_hist = vertical.histogram()
        equal_pairs = horizontal_hist[0] + vertical_hist[0]
        total_pairs = (width - 1) * height + width * (height - 1)
        return {
            "available": True,
            "method": "single-image weak heuristics; not proof of an upscale stage",
            "contrast_stddev": round(contrast, 6),
            "half_scale_residual_rms": round(residual_rms, 6),
            "half_scale_residual_ratio": round(residual_rms / max(contrast, 1e-9), 6),
            "edge_mean": round(edge_mean, 6),
            "neighbor_equal_ratio": round(equal_pairs / max(total_pairs, 1), 6),
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"visual analysis failed: {exc.__class__.__name__}: {exc}",
        }


def _positive_case(
    name: str,
    size: str,
    description: str,
    *,
    tags: tuple[str, ...],
) -> ImageTestCase:
    failures = validate_gpt_image_2_size(size)
    if failures:
        raise ValueError(f"Positive case {name!r} has invalid size {size!r}: {failures}")
    width, height = _parse_size(size) or (None, None)
    return ImageTestCase(
        name=name,
        parameters={
            "n": 1,
            "quality": "low",
            "size": size,
            "output_format": "png",
        },
        expected_outcome="success",
        expected_size=(int(width), int(height)),
        expected_format="PNG",
        description=description,
        tags=tags,
    )


def _negative_case(name: str, size: str, description: str) -> ImageTestCase:
    if not validate_gpt_image_2_size(size):
        raise ValueError(f"Negative case {name!r} unexpectedly has a valid size {size!r}")
    return ImageTestCase(
        name=name,
        parameters={
            "n": 1,
            "quality": "low",
            "size": size,
            "output_format": "png",
        },
        expected_outcome="rejection",
        expected_size=None,
        expected_format=None,
        description=description,
        tags=("negative_boundary", "resolution"),
    )


def _gpt_image_parameter_case(
    name: str,
    overrides: dict[str, Any],
    description: str,
    *,
    tags: tuple[str, ...],
    output_format: str | None = None,
    expected_outcome: str = "success",
) -> ImageTestCase:
    parameters: dict[str, Any] = {
        "n": 1,
        "quality": "low",
        "size": "1024x1024",
        "output_format": output_format or "png",
    }
    parameters.update(overrides)
    expected_format = {
        "png": "PNG",
        "jpeg": "JPEG",
        "webp": "WEBP",
    }[output_format or "png"]
    return ImageTestCase(
        name=name,
        parameters=parameters,
        expected_outcome=expected_outcome,
        expected_size=(1024, 1024) if expected_outcome == "success" else None,
        expected_format=expected_format if expected_outcome == "success" else None,
        description=description,
        tags=("gpt-image-2", *tags),
        metadata={
            "family": "gpt-image-2",
            "forced_output_format": output_format,
        },
    )


def _grok_case(
    name: str,
    *,
    aspect_ratio: str,
    resolution: str,
    description: str,
    tags: tuple[str, ...],
    n: int = 1,
    response_format: str = "b64_json",
) -> ImageTestCase:
    expected_size = GROK_IMAGINE_DIMENSIONS.get((resolution, aspect_ratio))
    return ImageTestCase(
        name=name,
        parameters={
            "n": n,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "response_format": response_format,
        },
        expected_outcome="success",
        expected_size=expected_size,
        expected_format=None,
        description=description,
        tags=("grok-imagine", *tags),
        metadata={
            "family": "grok-imagine",
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "response_format": response_format,
            "expected_aspect_ratio": aspect_ratio,
            "expected_size": list(expected_size) if expected_size else None,
        },
    )


def _grok_negative_case(
    name: str,
    override: dict[str, Any],
    description: str,
) -> ImageTestCase:
    parameters: dict[str, Any] = {
        "n": 1,
        "aspect_ratio": "1:1",
        "resolution": "1k",
        "response_format": "b64_json",
    }
    parameters.update(override)
    return ImageTestCase(
        name=name,
        parameters=parameters,
        expected_outcome="rejection",
        expected_size=None,
        expected_format=None,
        description=description,
        tags=("grok-imagine", "negative_boundary"),
        metadata={"family": "grok-imagine", "invalid_parameter": next(iter(override))},
    )


def _matches_aspect_ratio(
    width: int | None,
    height: int | None,
    expected: str,
) -> bool:
    if not width or not height:
        return False
    match = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", expected)
    if not match:
        return False
    expected_ratio = float(match.group(1)) / float(match.group(2))
    actual_ratio = width / height
    return abs(actual_ratio - expected_ratio) / expected_ratio <= 0.005


def _banana_case(
    name: str,
    model_template: str,
    *,
    model_resolution: str,
    requested_resolution: str,
    expected_size: tuple[int, int] | None,
    control_probe: str,
    aspect_ratio: str = "1:1",
    expected_outcome: str | None = None,
) -> ImageTestCase:
    square_sizes = {
        "512": (512, 512),
        "1K": (1024, 1024),
        "2K": (2048, 2048),
        "4K": (4096, 4096),
    }
    requested_size = square_sizes[requested_resolution.upper()]
    model_size = square_sizes[model_resolution]
    uses_resolution_alias = (
        "{resolution}" in model_template or "{resolution_lower}" in model_template
    )
    model = model_template.format(
        resolution=model_resolution,
        resolution_lower=model_resolution.lower(),
    )
    return ImageTestCase(
        name=name,
        model_override=model,
        parameters={
            "n": 1,
            "quality": "low",
            "size": f"{requested_size[0]}x{requested_size[1]}",
            "output_format": "png",
        },
        expected_outcome=expected_outcome or (
            "success"
            if control_probe in {"aligned", "official_parameter"}
            else "observation"
        ),
        expected_size=expected_size,
        expected_format="PNG",
        description=(
            f"Banana {'alias ' + model_resolution if uses_resolution_alias else 'fixed model'} "
            f"with request parameter {requested_resolution}; "
            f"{control_probe} resolution-control probe."
        ),
        tags=("banana", "resolution", f"control_{control_probe}"),
        metadata={
            "family": "banana",
            "model_mode": "resolution_alias" if uses_resolution_alias else "fixed",
            "control_probe": control_probe,
            "aspect_ratio": aspect_ratio,
            "model_resolution": model_resolution if uses_resolution_alias else None,
            "requested_resolution": requested_resolution,
            "model_expected_size": list(model_size) if uses_resolution_alias else None,
        },
    )


def _read_image_header(raw: bytes) -> tuple[str, int | None, int | None, bool | None]:
    if raw.startswith(bytes.fromhex("89504e470d0a1a0a")):
        if len(raw) < 26 or raw[12:16] != b"IHDR":
            raise ValueError("invalid PNG IHDR")
        width, height = struct.unpack(">II", raw[16:24])
        color_type = raw[25]
        _validate_png_structure(raw, width, height, raw[24], color_type, raw[28])
        return "PNG", width, height, color_type in {4, 6}
    if raw.startswith(bytes.fromhex("ffd8ff")):
        width, height = _jpeg_size(raw)
        return "JPEG", width, height, False
    if len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        width, height, has_alpha = _webp_size(raw)
        return "WEBP", width, height, has_alpha
    raise ValueError(f"unsupported image signature: {raw[:16].hex()}")


def _validate_png_structure(
    raw: bytes,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace_method: int,
) -> None:
    position = 8
    idat_parts: list[bytes] = []
    saw_ihdr = False
    saw_iend = False
    while position + 12 <= len(raw):
        length = struct.unpack(">I", raw[position : position + 4])[0]
        chunk_type = raw[position + 4 : position + 8]
        data_start = position + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(raw):
            raise ValueError(f"truncated PNG chunk {chunk_type!r}")
        expected_crc = struct.unpack(">I", raw[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + raw[data_start:data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError(f"invalid PNG CRC for {chunk_type!r}")
        if chunk_type == b"IHDR":
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            idat_parts.append(raw[data_start:data_end])
        elif chunk_type == b"IEND":
            saw_iend = True
            if crc_end != len(raw):
                raise ValueError("unexpected data after PNG IEND")
            break
        position = crc_end

    if not saw_ihdr or not idat_parts or not saw_iend:
        raise ValueError("PNG must contain IHDR, IDAT, and IEND chunks")
    try:
        decompressed = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        raise ValueError(f"invalid PNG IDAT stream: {exc}") from exc
    if not decompressed:
        raise ValueError("PNG IDAT stream is empty")
    if interlace_method == 0:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
        if channels is None:
            raise ValueError(f"unsupported PNG color type: {color_type}")
        row_bytes = math.ceil(width * channels * bit_depth / 8)
        expected_length = (row_bytes + 1) * height
        if len(decompressed) != expected_length:
            raise ValueError(
                f"PNG scanline length mismatch: expected={expected_length}:actual={len(decompressed)}"
            )


def _jpeg_size(raw: bytes) -> tuple[int, int]:
    position = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while position + 4 <= len(raw):
        while position < len(raw) and raw[position] != 0xFF:
            position += 1
        while position < len(raw) and raw[position] == 0xFF:
            position += 1
        if position >= len(raw):
            break
        marker = raw[position]
        position += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(raw):
            break
        segment_length = struct.unpack(">H", raw[position : position + 2])[0]
        if segment_length < 2 or position + segment_length > len(raw):
            break
        if marker in sof_markers:
            if segment_length < 7:
                break
            height, width = struct.unpack(">HH", raw[position + 3 : position + 7])
            return width, height
        position += segment_length
    raise ValueError("JPEG dimensions not found")


def _webp_size(raw: bytes) -> tuple[int, int, bool | None]:
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        return width, height, bool(raw[20] & 0x10)
    if chunk == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
        bits = int.from_bytes(raw[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height, bool((bits >> 28) & 1)
    if chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == bytes.fromhex("9d012a"):
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return width, height, False
    raise ValueError(f"unsupported WebP chunk: {chunk!r}")


def _parse_size(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _result_requested_size(result: dict[str, Any]) -> tuple[int, int] | None:
    requested = result.get("requested") or {}
    direct = _parse_size(str(requested.get("size") or ""))
    if direct:
        return direct
    expected = _metadata_size((result.get("metadata") or {}).get("expected_size"))
    if expected:
        return expected
    extra_body = requested.get("extra_body")
    google = extra_body.get("google") if isinstance(extra_body, dict) else None
    image_config = google.get("image_config") if isinstance(google, dict) else None
    image_size = image_config.get("image_size") if isinstance(image_config, dict) else None
    if image_size is None:
        image_size = (result.get("metadata") or {}).get("requested_resolution")
    edge = {
        "512": 512,
        "0.5K": 512,
        "1K": 1024,
        "2K": 2048,
        "4K": 4096,
    }.get(str(image_size).upper())
    return (edge, edge) if edge else None


def _metadata_size(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    width, height = _optional_int(value[0]), _optional_int(value[1])
    if width is None or height is None:
        return None
    return width, height


def _usage_output_tokens(usage: dict[str, Any]) -> float | None:
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        image_tokens = _optional_float(completion_details.get("image_tokens"))
        if image_tokens is not None and image_tokens > 0:
            return image_tokens
    direct = _optional_float(usage.get("output_tokens"))
    if direct is not None and direct > 0:
        return direct
    return _optional_float(usage.get("completion_tokens"))


def _bytes_per_megapixel(byte_length: int | None, pixels: int) -> float | None:
    if byte_length is None or pixels <= 0:
        return None
    return byte_length / (pixels / 1_000_000)


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in {None, 0}:
        return None
    return float(numerator) / float(denominator)


def _rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None and math.isfinite(value) else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _postprocess_limitations() -> list[str]:
    return [
        "Black-box output cannot prove whether generation was native-resolution, latent refinement, or post-upscale.",
        "The current text-to-image cases do not have a stable seed, so samples are nondeterministic.",
        "Latency, token, byte-density, and single-image detail metrics are heuristic evidence only.",
        "Only provider stage metadata or a controlled edit/upscale pair can support a confirmed pipeline claim.",
    ]
