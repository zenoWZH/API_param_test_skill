"""GPT Image 2.5 Image API cases, independent of legacy GPT Image 2 facts.

Official sources fetched 2026-09-08. Request acceptance, decoded output,
completion, identity, and token quantity remain separate evidence gates.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from dataclasses import replace
from functools import lru_cache
from typing import Any

from .image_validation import ImageTestCase, evaluate_case, inspect_image_bytes, validate_gpt_image_2_size

GUIDE = "https://developers.openai.com/api/docs/guides/image-generation"
IMAGE_REFERENCE = "https://developers.openai.com/api/reference/resources/images"
GPT_IMAGE_25_MODELS = frozenset(
    f"gpt-image-2.5-{variant}{suffix}"
    for variant in ("sunburst", "flare")
    for suffix in ("", "-2026-09-08")
)
QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")


def is_gpt_image_25(model: str) -> bool:
    return model in GPT_IMAGE_25_MODELS


def gpt_image_25_cases(
    model: str,
    suite: str = "smoke",
    *,
    operation: str = "generation",
    include_4k: bool = False,
    include_negative: bool = True,
) -> list[ImageTestCase]:
    """Build independently attributed cases for one exact model and endpoint."""
    if not is_gpt_image_25(model):
        raise ValueError(f"Not an exact GPT Image 2.5 model: {model!r}")
    if suite not in {"smoke", "resolution", "full"} or operation not in {"generation", "edit"}:
        raise ValueError("Unsupported GPT Image 2.5 suite or operation")
    if suite == "full" and not include_4k:
        raise ValueError("The full suite requires include_4k=True")

    def case(name: str, parameters: dict[str, Any] | None = None, *,
             negative: str | None = None, observation: bool = False,
             metadata: dict[str, Any] | None = None) -> ImageTestCase:
        params = {"size": "1024x1024", "quality": "low", "output_format": "png", "n": 1,
                  **(parameters or {})}
        size = params["size"]
        dimensions = tuple(map(int, size.split("x"))) if size != "auto" else None
        name = f"gpt_image_25_{operation}_{name}"
        return ImageTestCase(
            name=name, parameters=params,
            expected_outcome="rejection" if negative else "observation" if observation else "success",
            expected_size=None if negative else dimensions,
            expected_format=None if negative else {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}.get(params["output_format"]),
            description=f"GPT Image 2.5 {operation}: {name.rsplit(operation + '_', 1)[-1]}.",
            tags=("gpt-image-2.5", operation, "negative" if negative else "parameter"),
            metadata={"gpt_image_25": True, "operation": operation,
                      "test_profile": name, "source": GUIDE, "source_checked_at": "2026-09-08",
                      "pin_parameters": True, "rejection_parameter": negative,
                      "token_model_policy": "exact_model_calculator_required",
                      **({"prompt": "Change the blue square in the reference image to orange. Preserve the square's position, the white background, and the TEST 1 label."}
                         if operation == "edit" else {}),
                      **({"prompt": "An isolated orange square on a fully transparent background. No shadows, no text, no checkerboard, and no opaque backdrop."}
                         if (metadata or {}).get("expected_transparency") is True else {}),
                      **(metadata or {})},
        )

    cases = [case("baseline")]
    if suite == "smoke":
        return cases
    cases.extend(case(f"quality_{quality}", {"quality": quality}) for quality in QUALITIES if quality != "low")
    cases.extend([
        case("size_auto", {"size": "auto"}),
        case("portrait", {"size": "1024x1536"}),
        case("landscape", {"size": "1536x1024"}),
        case("custom_landscape", {"size": "1536x864"}),
        case("minimum_pixels", {"size": "1024x640"}),
        case("aspect_3_to_1", {"size": "1536x512"}),
        case("square_2k", {"size": "2048x2048"}),
        case("background_auto", {"background": "auto"}),
        case("background_opaque", {"background": "opaque"}, metadata={"expected_transparency": False}),
        case("transparent_png", {"background": "transparent"}, metadata={"expected_transparency": True}),
        case("transparent_webp", {"background": "transparent", "output_format": "webp"}, metadata={"expected_transparency": True}),
        case("format_jpeg", {"output_format": "jpeg"}),
        case("format_webp", {"output_format": "webp"}),
        case("jpeg_compression_0", {"output_format": "jpeg", "output_compression": 0}),
        case("webp_compression_100", {"output_format": "webp", "output_compression": 100}),
        case("jpeg_compression_50", {"output_format": "jpeg", "output_compression": 50}),
        case("png_compression_observation", {"output_compression": 50}, observation=True,
             metadata={"documentation_conflict": "compression_documented_for_jpeg_webp_only"}),
        case("batch_n2", {"n": 2}),
        case("user", {"user": "gpt-image-25-parameter-matrix"}),
        case("stream_false", {"stream": False}),
        case("stream_final_only", {"stream": True, "partial_images": 0}, metadata={"documentation_conflict": "model_page_streaming_vs_image_api_guide"}),
        case("stream_partials", {"stream": True, "partial_images": 3}, metadata={"documentation_conflict": "model_page_streaming_vs_image_api_guide"}),
    ])
    if suite == "full":
        cases.append(case("batch_n10", {"n": 10}))
    if include_4k:
        cases.extend([case("landscape_4k", {"size": "3840x2160"}), case("portrait_4k", {"size": "2160x3840"})])
    if operation == "generation":
        cases.extend([case("moderation_auto", {"moderation": "auto"}), case("moderation_low", {"moderation": "low"})])
    if operation == "edit":
        cases.extend([
            case("multi_image", metadata={"input_image_count": 2}),
            case("input_jpeg", metadata={"input_image_format": "JPEG"}),
            case("input_webp", metadata={"input_image_format": "WEBP"}),
            case("max_16_inputs", metadata={"input_image_count": 16}),
            case("mask", metadata={"input_mask": True}),
            case("precision_recolor", metadata={"semantic_review": "Orange square; original placement, label, and background preserved."}),
            case("layout_translation", metadata={
                "prompt": "Replace TEST 1 with the French text ESSAI 1 in the reference image. Preserve the square, colors, location and text layout.",
                "semantic_review": "Exact ESSAI 1 text; original geometry, colors and layout preserved."}),
            case("reference_composition", metadata={"input_image_count": 2,
                "prompt": "Place the two reference squares side by side on the same white canvas, retaining their distinct colors and their TEST 1 and TEST 2 labels.",
                "semantic_review": "Both distinct reference colors and labels retained in a side-by-side composition."}),
            case("subject_preservation", metadata={
                "prompt": "Add a thin orange frame around the blue square in the reference image. Preserve the square itself, TEST 1 text and white background.",
                "semantic_review": "Orange frame added; blue square and its label remain recognizable and unchanged in content."}),
            # The guide omits 2.5 fidelity and the API reference uses a generic
            # '1.5 and later' claim. Keep unresolved cases observational until
            # the exact-model official reference is established by live proof.
            case("input_fidelity_high", {"input_fidelity": "high"}, observation=True,
                 metadata={"documentation_conflict": "exact_model_input_fidelity_unconfirmed"}),
            case("input_fidelity_low", {"input_fidelity": "low"}, observation=True,
                 metadata={"documentation_conflict": "exact_model_input_fidelity_unconfirmed"}),
        ])
    if include_negative:
        cases.extend([
            case("reject_size_alignment", {"size": "1537x864"}, negative="size"),
            case("reject_aspect_ratio", {"size": "3072x768"}, negative="size"),
            case("reject_minimum_pixels", {"size": "512x512"}, negative="size"),
            case("reject_maximum_pixels", {"size": "3072x3072"}, negative="size"),
            case("reject_maximum_edge", {"size": "4096x1920"}, negative="size"),
            case("reject_quality", {"quality": "ultra"}, negative="quality"),
            case("reject_output_format", {"output_format": "gif"}, negative="output_format"),
            case("reject_response_format", {"response_format": "url"}, negative="response_format"),
            case("reject_transparent_jpeg", {"background": "transparent", "output_format": "jpeg"}, negative="background"),
            case("reject_n0", {"n": 0}, negative="n"),
            case("reject_n11", {"n": 11}, negative="n"),
            case("reject_compression_low", {"output_format": "jpeg", "output_compression": -1}, negative="output_compression"),
            case("reject_compression_high", {"output_format": "webp", "output_compression": 101}, negative="output_compression"),
            case("reject_partial_images", {"stream": True, "partial_images": 4}, negative="partial_images"),
        ])
        if operation == "generation":
            cases.append(case("reject_moderation", {"moderation": "off"}, negative="moderation"))
        if operation == "edit":
            cases.extend([
                case("reject_17_inputs", negative="image", metadata={"input_image_count": 17}),
                case("reject_mask_size", negative="mask", metadata={"input_mask": True, "invalid_mask_size": True}),
                case("reject_input_fidelity", {"input_fidelity": "ultra"}, negative="input_fidelity"),
            ])
    return cases


@lru_cache(maxsize=20)
def _fixture_png(index: int = 0, *, mask: bool = False, invalid_size: bool = False) -> bytes:
    """Small reproducible test fixtures; no user image or external download."""
    from PIL import Image, ImageDraw
    size = (512, 512) if invalid_size else (1024, 1024)
    image = Image.new("RGBA", size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    if mask:
        draw.rectangle((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4), fill=(0, 0, 0, 0))
    else:
        draw.rectangle((256, 256, 768, 768), fill=((40 + index * 13) % 256, 80, 210, 255))
        draw.text((420, 460), f"TEST {index + 1}", fill=(255, 255, 255, 255), font_size=42)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def edit_fixtures(case: ImageTestCase) -> list[tuple[str, tuple[str, bytes, str]]]:
    count = int(case.metadata.get("input_image_count", 1))
    if not 1 <= count <= 17:
        raise ValueError("GPT Image 2.5 fixture count is outside the authored matrix")
    files = [("image[]", (f"fixture-{index + 1}.png", _fixture_png(index), "image/png")) for index in range(count)]
    input_format = str(case.metadata.get("input_image_format") or "PNG")
    if input_format != "PNG":
        from PIL import Image
        if input_format not in {"JPEG", "WEBP"}:
            raise ValueError("Unsupported authored input image format")
        converted = []
        for index, (field, value) in enumerate(files):
            output = io.BytesIO()
            with Image.open(io.BytesIO(value[1])) as source:
                source.convert("RGB").save(output, format=input_format)
            extension = "jpg" if input_format == "JPEG" else "webp"
            converted.append((field, (f"fixture-{index + 1}.{extension}", output.getvalue(), "image/" + input_format.lower())))
        files = converted
    if case.metadata.get("input_mask"):
        files.append(("mask", ("mask.png", _fixture_png(mask=True, invalid_size=bool(case.metadata.get("invalid_mask_size"))), "image/png")))
    return files


def fixture_manifest(case: ImageTestCase) -> list[dict[str, Any]]:
    return [{"field": field, "filename": value[0], "byte_length": len(value[1]),
             "sha256": hashlib.sha256(value[1]).hexdigest(), "mime_type": value[2]}
            for field, value in edit_fixtures(case)]


def rejection_attribution(case: ImageTestCase, error: Any) -> dict[str, Any]:
    target = case.metadata.get("rejection_parameter")
    if not target or case.expected_outcome != "rejection":
        return {"required": False, "matched": None, "target": None}
    payload = error.get("error", error) if isinstance(error, dict) else {}
    param = str(payload.get("param") or "") if isinstance(payload, dict) else ""
    serialized = json.dumps(error, sort_keys=True).casefold()
    # A one-character 'n' must not match every error message.
    markers = (f"'{target}'", f'"{target}"', f"parameter {target}", f"{target} must", f"{target} should")
    if target != "n":
        markers += (str(target), str(target).replace("_", " "))
    matched = param == target or param.startswith(str(target) + "[") or any(marker in serialized for marker in markers)
    return {"required": True, "matched": matched, "target": target}


def observation_outcome(case: ImageTestCase, result: dict[str, Any], images: list[Any]) -> dict[str, Any]:
    """Resolve exploratory acceptance without certifying a disputed contract."""
    if not case.metadata.get("gpt_image_25") or case.expected_outcome != "observation":
        return result
    status = result.get("status_code")
    if isinstance(status, int) and 200 <= status <= 299:
        checked = evaluate_case(replace(case, expected_outcome="success"),
                                status_code=status, images=images, usage=result.get("usage"),
                                latency_ms=result.get("latency_ms"), error=result.get("error"))
        observed = checked.get("pass") is True
        result = {**result, "failures": checked["failures"]}
        disposition = "observed_acceptance" if observed else "unresolved"
    else:
        parameter = "input_fidelity" if "input_fidelity" in case.parameters else "output_compression"
        attribution = rejection_attribution(replace(
            case, expected_outcome="rejection",
            metadata={**case.metadata, "rejection_parameter": parameter},
        ), result.get("error"))
        observed = status in {400, 422} and attribution["matched"] is True
        disposition = "observed_parameter_rejection" if observed else "unresolved"
        result = {**result, "parameter_rejection_attribution": attribution}
    return {**result, "pass": False, "status": disposition,
            "verification_level": "diagnostic_observation" if observed else "none",
            "diagnostic_pass": observed, "certified_route_contract_pass": False}


def response_parameter_evidence(case: ImageTestCase, payload: dict[str, Any], images: list[Any]) -> tuple[dict[str, Any], list[str]]:
    evidence: dict[str, Any] = {}
    failures: list[str] = []
    if not case.metadata.get("gpt_image_25"):
        return evidence, failures
    for parameter in ("quality", "size", "output_format", "background"):
        if parameter not in case.parameters:
            continue
        requested = case.parameters[parameter]
        returned = payload.get(parameter)
        status = "not_returned" if returned is None else "auto_resolved" if requested == "auto" else "match" if returned == requested else "mismatch"
        if requested == "auto" and returned is not None:
            if parameter == "quality" and returned not in QUALITIES[:-1]:
                status = "invalid_resolved_value"
            elif parameter == "background" and returned not in {"opaque", "transparent"}:
                status = "invalid_resolved_value"
            elif parameter == "size" and (not isinstance(returned, str) or validate_gpt_image_2_size(returned)):
                status = "invalid_resolved_value"
        evidence[parameter] = {"requested": requested, "returned": returned, "status": status}
        if status in {"mismatch", "invalid_resolved_value"}:
            failures.append("returned_parameter_mismatch:" + parameter)
    if case.parameters.get("size") == "auto":
        for index, image in enumerate(images):
            # Both 2.5 variants document these same numerical constraints.
            if validate_gpt_image_2_size(f"{image.width}x{image.height}"):
                failures.append(f"auto_size_outside_documented_bounds:index={index}")
    if payload.get("error"):
        failures.append("image_error_envelope")
    return evidence, failures


def token_expectation_request(body: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Use a returned explicit quality without changing the measured request."""
    result = copy.deepcopy(body)
    if body.get("quality") == "auto" and payload.get("quality") in QUALITIES[:-1]:
        result["quality"] = payload["quality"]
    return result


def validate_case_binding(binding: dict[str, Any], model: str, operation: str) -> None:
    """Never execute today's cases under an older frozen request definition."""
    parameter = binding.get("parameter_test_binding") or {}
    interface = binding.get("interface") or {}
    expected_api = "openai_images_edits" if operation == "edit" else "openai_images_generations"
    reference_model = binding.get("reference_model_id") or model
    expected = [case.public() for case in gpt_image_25_cases(
        str(reference_model), "full", operation=operation, include_4k=True, include_negative=True,
    )]
    ids = interface.get("request_model_ids") or (binding.get("profile") or {}).get("request_model_ids") or []
    if ((binding.get("reference_source_id") or binding.get("source_id")) != "openai"
        or interface.get("api_form") != expected_api or model not in ids):
        raise ValueError("GPT Image 2.5 case binding source/model/API mismatch")
    if (parameter.get("case_definitions") != expected
        or parameter.get("test_cases") != [case["name"] for case in expected]):
        raise ValueError("immutable GPT Image 2.5 case definitions differ from the executable factory")


def summarize_observations(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Count completed ordinary observations without certifying disputed facts.

    A mixed matrix can pass its declared checks when every contractual case and
    every exploratory observation completes. Explicit candidate execution keeps
    its separate non-certification gate in its caller.
    """
    observations = [row for row in results if (row.get("metadata") or {}).get("gpt_image_25") and "diagnostic_pass" in row]
    if not observations:
        return
    contractual = [row for row in results if row not in observations]
    failures = [row for row in contractual if row.get("overall_pass") is not True]
    unresolved = [row for row in observations if row.get("diagnostic_pass") is not True]
    planned = summary.get("planned_case_count", summary.get("case_count", len(results)))
    case_ids = [row.get("case") for row in results]
    complete = (type(planned) is int and planned == len(results) and bool(results)
                and len(case_ids) == len(set(case_ids)) and all(case_ids)
                and not summary.get("not_executed_count"))
    passed = (bool(contractual) and complete and not failures and not unresolved
              and summary.get("token_validation_pass", True) is not False
              and summary.get("model_identity_pass", True) is not False)
    summary.update({
        "pass": passed, "overall_pass": passed, "certified_route_contract_pass": False,
        "observed_case_count": len(observations), "contractual_case_count": len(contractual),
        "diagnostic_pass": not unresolved, "observation_execution_complete": complete and not unresolved,
        "observed_acceptance_count": sum(row.get("status") == "observed_acceptance" for row in observations),
        "observed_parameter_rejection_count": sum(row.get("status") == "observed_parameter_rejection" for row in observations),
        "unresolved_observation_count": len(unresolved),
        "compatibility_pass": bool(contractual) and all(row.get("compatibility_pass") is True for row in contractual),
        "pass_count": len(contractual) - len(failures),
        "failure_count": len(failures) + len(unresolved),
        "failed_cases": [row.get("case") for row in failures + unresolved],
        "compatibility_failure_count": sum(row.get("compatibility_pass") is not True for row in contractual),
        "compatibility_failed_cases": [row.get("case") for row in contractual if row.get("compatibility_pass") is not True],
    })


def decode_image_stream(response: Any, case: ImageTestCase) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only completed image events supply final artifacts; partials never do."""
    operation = "image_edit" if case.metadata.get("operation") == "edit" else "image_generation"
    expected_final = operation + ".completed"
    expected_partial = operation + ".partial_image"
    metadata: dict[str, Any] = {"event_types": [], "partial_image_indices": [], "completion_events": 0, "failures": []}
    final_items, final_events, partial_data = [], [], []
    event_lines: list[str] = []
    event_header: str | None = None
    byte_count = 0

    def consume() -> None:
        nonlocal event_header
        if not event_lines:
            event_header = None
            return
        encoded = "\n".join(event_lines)
        event_lines.clear()
        if encoded == "[DONE]":
            event_header = None
            return
        event = json.loads(encoded)
        if not isinstance(event, dict):
            raise ValueError("image SSE data is not an object")
        event_type = str(event.get("type") or "")
        if event_header is not None and event_header != event_type:
            metadata["failures"].append("image_stream_event_type_mismatch")
        event_header = None
        if final_events:
            metadata["failures"].append("image_stream_event_after_completion")
        metadata["event_types"].append(event_type)
        if len(metadata["event_types"]) > 1000:
            raise ValueError("image SSE event limit exceeded")
        if event_type == expected_final:
            final_items.append({"b64_json": event.get("b64_json")})
            final_events.append(event)
        elif event_type == expected_partial:
            index = event.get("partial_image_index")
            if (not isinstance(index, int) or isinstance(index, bool)
                or index in metadata["partial_image_indices"] or not 0 <= index < int(case.parameters.get("partial_images", 0))):
                metadata["failures"].append("invalid_partial_image_index")
            metadata["partial_image_indices"].append(index)
            partial_data.append(event.get("b64_json"))
        elif event_type in {"error", operation + ".failed"} or event.get("error"):
            metadata["failures"].append("image_stream_error")
        else:
            metadata["failures"].append("image_stream_unexpected_event")

    if "text/event-stream" not in str(response.headers.get("content-type", response.headers.get("Content-Type", ""))).lower():
        metadata["failures"].append("image_stream_content_type_invalid")
    for line in response.iter_lines(decode_unicode=True):
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="strict")
        byte_count += len(line.encode("utf-8"))
        if byte_count > 120 * 1024 * 1024:
            raise ValueError("image SSE payload limit exceeded")
        if not line:
            consume()
        elif line.startswith("data:"):
            event_lines.append(line[5:].lstrip())
        elif line.startswith("event:"):
            event_header = line[6:].strip()
    if event_lines or event_header:
        metadata["failures"].append("image_stream_truncated_event")
    metadata["completion_events"] = len(final_events)
    if len(final_events) != int(case.parameters.get("n", 1)):
        metadata["failures"].append("image_stream_completion_count_mismatch")
    for encoded in partial_data:
        try:
            if not isinstance(encoded, str) or len(encoded) > 28 * 1024 * 1024:
                raise ValueError("invalid partial base64")
            inspect_image_bytes(base64.b64decode(encoded, validate=True), visual_forensics=False)
        except (ValueError, TypeError):
            metadata["failures"].append("image_stream_partial_decode_failed")
    payload = {"data": final_items}
    # The matrix streams one image per request. Multiple final usage objects
    # must not be guessed into a single billable total.
    if len(final_events) == 1:
        payload.update({key: copy.deepcopy(value) for key, value in final_events[0].items()
                        if key not in {"b64_json", "type"}})
    return payload, metadata
