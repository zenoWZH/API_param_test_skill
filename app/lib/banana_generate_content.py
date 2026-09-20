"""Exact, source-scoped Gemini image candidates for GenerateContent v1beta.

The case factory is pure: callers validate the official runtime binding before
executing diagnostic candidates. Candidate completion is never certification.
"""

from __future__ import annotations

import json
from typing import Any

from .image_validation import ImageTestCase
from .gemini_api_version import is_ai_studio_origin


OFFICIAL_IMAGE_SOURCE = "https://ai.google.dev/gemini-api/docs/generate-content/image-generation#aspect-ratios-and-image-size"
BANANA_GC_MODELS = frozenset({
    "gemini-2.5-flash-image", "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image", "gemini-3-pro-image",
})
STANDARD_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
EXTREME_RATIOS = ("1:4", "1:8", "4:1", "8:1")
IMAGE_1K_DIMENSIONS = {
    "1:1": (1024, 1024), "2:3": (848, 1264), "3:2": (1264, 848),
    "3:4": (896, 1200), "4:3": (1200, 896), "4:5": (928, 1152),
    "5:4": (1152, 928), "9:16": (768, 1376), "16:9": (1376, 768),
    "21:9": (1584, 672), "1:4": (512, 2048), "1:8": (384, 3072),
    "4:1": (2048, 512), "8:1": (3072, 384),
}
FLASH_25_DIMENSIONS = {
    "1:1": (1024, 1024), "2:3": (832, 1248), "3:2": (1248, 832),
    "3:4": (864, 1184), "4:3": (1184, 864), "4:5": (896, 1152),
    "5:4": (1152, 896), "9:16": (768, 1344), "16:9": (1344, 768),
    "21:9": (1536, 672),
}
PROFILE_SUFFIXES = (
    "documented_matrix", "image_size_boundaries", "extreme_aspect_boundaries",
    "lowercase_1k", "invalid_aspect_7_5",
)


def contract_id_for_model(model: str) -> str:
    if model not in BANANA_GC_MODELS:
        raise ValueError(f"No exact Banana GenerateContent candidate for {model!r}.")
    return model.replace("-", "_").replace(".", "_") + "_generate_content_v1beta"


def has_exact_banana_gc_reference(model: str, capability: dict[str, Any]) -> bool:
    """Select the comparison contract independently of an adapter's URL version."""
    if model not in BANANA_GC_MODELS or capability.get("api_form") != "gemini_generate_content":
        return False
    identity = capability.get("reference_identity") or {}
    source_ids = {value for value in (capability.get("source_id"), identity.get("source_id")) if value}
    contract_ids = {value for value in (
        capability.get("reference_contract_id"), capability.get("default_reference_source"),
        identity.get("reference_contract_id"),
    ) if value}
    return source_ids == {"google_ai_studio"} and contract_ids == {contract_id_for_model(model)}


def banana_gc_comparison_metadata(model: str, capability: dict[str, Any], endpoint: str) -> dict[str, Any]:
    if not has_exact_banana_gc_reference(model, capability):
        return {}
    adapter = not is_ai_studio_origin(endpoint) or capability.get("route_profile") != "google_ai_studio"
    return {
        "reference_api_version": "v1beta",
        "comparison_scope": "adapter_only" if adapter else "official_source",
        "certification_scope": "adapter_only" if adapter else capability.get("certification_scope"),
        **({"certified_route_contract_pass": False} if adapter else {}),
    }


def documented_aspect_ratios(model: str) -> tuple[str, ...]:
    contract_id_for_model(model)
    return STANDARD_RATIOS + EXTREME_RATIOS if model in {
        "gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"
    } else STANDARD_RATIOS


def documented_image_sizes(model: str) -> tuple[str | None, ...]:
    contract_id_for_model(model)
    return {
        "gemini-2.5-flash-image": (None,),
        "gemini-3.1-flash-lite-image": ("1K",),
        "gemini-3.1-flash-image": ("512", "1K", "2K", "4K"),
        "gemini-3-pro-image": ("1K", "2K", "4K"),
    }[model]


def _documented_size(model: str, ratio: str, tier: str | None) -> tuple[int, int] | None:
    if model == "gemini-2.5-flash-image":
        return FLASH_25_DIMENSIONS[ratio]
    if model == "gemini-3.1-flash-image" and ratio == "21:9" and tier == "512":
        return None  # Official 792x168 row conflicts with its stated 21:9 ratio.
    width, height = IMAGE_1K_DIMENSIONS[ratio]
    scale = {"512": 0.5, "1K": 1, "2K": 2, "4K": 4}[str(tier)]
    return int(width * scale), int(height * scale)


def build_banana_generate_content_cases(
    model: str,
    suite: str = "smoke",
    *,
    include_4k: bool = False,
    include_negative: bool = True,
    diagnostic: bool = False,
    capability_profile: dict[str, Any] | None = None,
) -> list[ImageTestCase]:
    """Expand documented Cartesian cells and isolated undocumented boundaries.

    ``diagnostic`` only describes the emitted cases; it grants no permission to
    execute them. The author runner must validate its candidate and binding.
    """
    contract = contract_id_for_model(model)
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError(f"Unsupported image test suite: {suite!r}")
    if suite == "full" and not include_4k:
        raise ValueError("The full Banana GC matrix requires include_4k=True.")
    capability = capability_profile or {}
    if not diagnostic:
        if (capability.get("parameter_test_enabled") is not True
                or capability.get("test_policy_parameter_test_enabled") is not True):
            raise ValueError("Exact Banana GC parameter matrix is not certified/enabled.")
        if not has_exact_banana_gc_reference(model, capability):
            raise ValueError("Exact Banana GC source/model/API contract mismatch.")

    cases: list[ImageTestCase] = []

    def append(ratio: str, tier: str | None, *, suffix: str = "documented_matrix", boundary: str | None = None) -> None:
        if tier == "4K" and not include_4k:
            return
        profile_id = f"{contract}_{suffix}"
        conflict = model == "gemini-3.1-flash-image" and ratio == "21:9" and tier == "512"
        dimensions = _documented_size(model, ratio, tier) if boundary is None else None
        image_config: dict[str, Any] = {"aspectRatio": ratio}
        if tier is not None:
            image_config["imageSize"] = tier
        name = f"{contract}__{str(tier or 'default').lower()}__aspect_{ratio.replace(':', '_')}"
        if boundary:
            name = f"{contract}__boundary_{boundary}"
        # Normal execution consumes exact beta evidence; documented capability
        # alone never invents an executable outcome for an unverified cell.
        outcome = "observation"
        evidence = None
        if not diagnostic:
            evidence_map = capability.get("image_case_expectations") or {}
            if not isinstance(evidence_map, dict):
                raise ValueError("Banana GC image_case_expectations must be a mapping.")
            evidence = evidence_map.get(name)
            if evidence is not None:
                if (not isinstance(evidence, dict)
                        or evidence.get("expectation") not in {"supported", "unsupported"}
                        or not isinstance(evidence.get("evidence_refs"), list)
                        or not evidence["evidence_refs"]
                        or not isinstance(evidence.get("documentation_match"), bool)):
                    raise ValueError(f"Incomplete exact beta image evidence for {name}.")
                if conflict and (
                    evidence["documentation_match"]
                    or len({json.dumps(ref, sort_keys=True) for ref in evidence["evidence_refs"]}) < 3
                ):
                    raise ValueError(f"Document-conflict beta evidence requires three distinct deviation observations for {name}.")
                outcome = "success" if evidence["expectation"] == "supported" else "rejection"
                if outcome == "success":
                    actual_size = evidence.get("expected_size")
                    if (not isinstance(actual_size, (list, tuple)) or len(actual_size) != 2
                            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in actual_size)):
                        raise ValueError(f"Accepted beta image evidence requires exact pixels for {name}.")
                    if evidence["documentation_match"] and (
                        dimensions is None or tuple(actual_size) != dimensions
                    ):
                        raise ValueError(f"Beta image evidence contradicts its documentation_match flag for {name}.")
                    dimensions = tuple(actual_size)
                else:
                    dimensions = None
        metadata = {
            "banana_gc_exact": True, "banana_gc_candidate": outcome == "observation",
            "profile_driven": True, "api_form": "gemini_generate_content",
            "api_version": "v1beta", "route_profile": "google_ai_studio",
            "model_scope": model, "stateless": True, "test_profile": profile_id,
            "requested_resolution": tier, "omit_image_size": tier is None,
            "aspect_ratio": ratio, "matrix_group": "boundary" if boundary else "documented",
            "probe_field": (
                "aspect_ratio" if ratio != "1:1"
                else "image_size" if tier is not None else None
            ),
            "dimension_source": "document_conflict" if conflict else "official_documentation" if dimensions else "unverified_boundary",
            "official_source": OFFICIAL_IMAGE_SOURCE,
        }
        if conflict:
            metadata["documented_dimensions_conflict"] = [792, 168]
            metadata["expected_aspect_ratio"] = ratio
        if not diagnostic:
            metadata.pop("api_version", None)
            metadata["reference_api_version"] = "v1beta"
        if evidence is not None:
            documented_size = (792, 168) if conflict else _documented_size(model, ratio, tier) if boundary is None else None
            metadata["documented_expected_size"] = list(documented_size) if documented_size else None
            metadata["documentation_match"] = evidence["documentation_match"]
            metadata["beta_evidence_refs"] = list(evidence["evidence_refs"])
            metadata["expectation"] = evidence["expectation"]
            if evidence["expectation"] == "supported" and not evidence["documentation_match"]:
                metadata["dimension_source"] = "official_live_documentation_deviation"
        cases.append(ImageTestCase(
            name=name,
            parameters={"generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": image_config}},
            model_override=model, expected_outcome=outcome, expected_size=dimensions,
            expected_format=None, description="Official GenerateContent v1beta image matrix cell.",
            tags=("banana", "generate_content", "diagnostic" if diagnostic else "parameter"),
            metadata=metadata,
        ))

    if suite == "smoke":
        append("1:1", None if model == "gemini-2.5-flash-image" else "1K")
        return cases
    baseline_tier = None if model == "gemini-2.5-flash-image" else "1K"
    append("1:1", baseline_tier)
    for tier in documented_image_sizes(model):
        for ratio in documented_aspect_ratios(model):
            if ratio == "1:1" and tier == baseline_tier:
                continue
            append(ratio, tier)
    if not include_negative:
        return cases
    boundary_tiers = {
        "gemini-2.5-flash-image": ("1K", "512", "2K", "4K"),
        "gemini-3.1-flash-lite-image": ("512", "2K", "4K"),
        "gemini-3.1-flash-image": (), "gemini-3-pro-image": ("512",),
    }[model]
    for tier in boundary_tiers:
        append("1:1", tier, suffix="image_size_boundaries", boundary=f"image_size_{tier.lower()}")
    if model in {"gemini-2.5-flash-image", "gemini-3-pro-image"}:
        for tier in documented_image_sizes(model):
            for ratio in EXTREME_RATIOS:
                append(ratio, tier, suffix="extreme_aspect_boundaries", boundary=f"aspect_{ratio.replace(':', '_')}_{str(tier or 'default').lower()}")
    append("1:1", "1k", suffix="lowercase_1k", boundary="lowercase_1k")
    append("7:5", None if model == "gemini-2.5-flash-image" else "1K", suffix="invalid_aspect_7_5", boundary="aspect_7_5")
    return cases


def parameter_rejection_attribution(case: ImageTestCase, error: Any) -> dict[str, Any]:
    """Attribute a rejection to the exact cell's parameter, never its baseline."""
    target = case.metadata.get("probe_field")
    markers = {"image_size": ("image_size", "image size", "imagesize"), "aspect_ratio": ("aspect_ratio", "aspect ratio", "aspectratio")}.get(target, ())
    error_text = json.dumps(error, ensure_ascii=False, sort_keys=True).casefold()
    matched = bool(markers) and any(marker in error_text for marker in markers)
    return {"required": bool(target), "matched": matched if target else None, "target": target}


def evaluate_candidate_case(case: ImageTestCase, *, status_code: int | None, images: list[Any], error: Any = None, usage: dict[str, Any] | None = None, latency_ms: float | None = None) -> dict[str, Any]:
    """Record candidate evidence without awarding a compatibility verdict."""
    attribution = parameter_rejection_attribution(case, error)
    accepted = status_code is not None and 200 <= status_code < 300
    rejected = status_code in {400, 422} and attribution["matched"] is True
    failures: list[str] = []
    if accepted and len(images) != 1:
        failures.append(f"output_count_mismatch:expected=1:actual={len(images)}")
    if accepted and error:
        failures.append("response_or_image_decode_error")
    geometry_failures = [
        f"dimension_mismatch:index={index}:expected={case.expected_size[0]}x{case.expected_size[1]}:actual={image.width}x{image.height}"
        for index, image in enumerate(images)
        if case.expected_size and (image.width, image.height) != case.expected_size
    ]
    aspect_ratio = case.metadata.get("expected_aspect_ratio")
    aspect_failures: list[str] = []
    if accepted and aspect_ratio and case.expected_size is None:
        numerator, denominator = (float(part) for part in str(aspect_ratio).split(":"))
        expected_ratio = numerator / denominator
        aspect_failures = [
            f"aspect_ratio_mismatch:index={index}:expected={aspect_ratio}:actual={image.width}x{image.height}"
            for index, image in enumerate(images)
            if abs((image.width / image.height) / expected_ratio - 1) > 0.005
        ]
    if not accepted and not rejected:
        failures.append("parameter_rejection_not_attributed" if status_code in {400, 422} else "request_failed")
    status = "observed_acceptance" if accepted and not failures else "observed_parameter_rejection" if rejected else "unresolved"
    return {
        "case": case.name, "pass": False, "compatibility_pass": False, "certified": False,
        "status": status, "expectation": "diagnostic", "diagnostic_pass": not failures,
        "verification_level": "diagnostic_observation" if not failures else "none",
        "status_code": status_code, "requested": dict(case.parameters),
        "actual_images": [image.public() for image in images], "usage": usage or {},
        "latency_ms": latency_ms, "error": error, "failures": failures,
        "dimension_validation_pass": not geometry_failures if accepted and images and case.expected_size else None,
        "dimension_validation_failures": geometry_failures,
        "aspect_ratio_validation_pass": not aspect_failures if accepted and images and aspect_ratio and case.expected_size is None else None,
        "aspect_ratio_validation_failures": aspect_failures,
        "parameter_rejection_attribution": attribution,
        "tags": list(case.tags), "metadata": dict(case.metadata),
    }
