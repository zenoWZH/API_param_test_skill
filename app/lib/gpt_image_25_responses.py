"""Source-scoped GPT Image 2.5 Responses tool fixtures and evidence checks.

The main Responses model and the image tool model are separate identities.
Images are fully decoded; acceptance does not prove subjective editing quality
or exact token accuracy when the response omits image-tool token accounting.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from .image_validation import ImageTestCase, inspect_image_bytes, validate_gpt_image_2_size
from .image_token_expectations import IMAGE_TOKEN_ABSOLUTE_TOLERANCE, IMAGE_TOKEN_RELATIVE_TOLERANCE, image_output_token_expectation

MODELS = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare")
SNAPSHOTS = {model: model + "-2026-09-08" for model in MODELS}
MAINLINE_MODEL = "gpt-6-astra"
ENDPOINT = "https://api.openai.com/v1/responses"
DOCS = (
    "https://developers.openai.com/api/docs/guides/image-generation",
    "https://developers.openai.com/api/docs/guides/tools-image-generation",
    "https://developers.openai.com/api/reference/python/resources/responses/methods/create",
    *("https://developers.openai.com/api/docs/models/" + model for model in MODELS),
)
PROMPT = "Generate exactly one image: a simple blue ceramic mug centered on a plain white background, no text."
EDIT_PROMPT = "Edit this image: change the blue mug to red, preserving its shape and the white background. Return exactly one edited image."
TRANSPARENT_PROMPT = "Generate exactly one cutout of a blue ceramic mug, with an entirely transparent background outside the mug. No ground or shadow."
MAX_OUTPUT_TOKENS = 4096
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
FROZEN_EXPECTATION_POLICY = "frozen_20260908"
CURRENT_EXPECTATION_POLICY = "accepted_auto_and_required_fidelity_20260909"
_EXPECTATION_POLICIES = frozenset({FROZEN_EXPECTATION_POLICY, CURRENT_EXPECTATION_POLICY})
_FIDELITY_OBSERVATION_CASE_NAMES = frozenset({
    "input_fidelity_low_observation", "input_fidelity_high_observation",
})


def _require_expectation_policy(expectation_policy: str) -> None:
    if expectation_policy not in _EXPECTATION_POLICIES:
        raise ValueError("unknown GPT Image 2.5 expectation policy")


def effective_case_expectations(case: dict, *, expectation_policy: str = CURRENT_EXPECTATION_POLICY) -> dict:
    """Apply an explicit interpretation without changing frozen request cases.

    The original factory remains the immutable definition source. This overlay
    changes only expectations; callers must validate the original case before
    applying it. Reapplying a policy is safe and never mutates the caller's case.
    """
    _require_expectation_policy(expectation_policy)
    effective = copy.deepcopy(case)
    frozen = case.get("frozen_expected_outcome", case["expected_outcome"])
    effective["frozen_expected_outcome"] = frozen
    effective["expected_outcome"] = frozen
    effective["expectation_policy"] = expectation_policy
    if (expectation_policy == CURRENT_EXPECTATION_POLICY
            and case.get("name") in _FIDELITY_OBSERVATION_CASE_NAMES
            and case.get("parameters", {}).get("input_fidelity") in {"low", "high"}):
        effective["expected_outcome"] = "rejection"
        effective["expected_rejection_field"] = "input_fidelity"
    effective["effective_expected_outcome"] = effective["expected_outcome"]
    effective["auto_output_size_validation"] = (
        "completed_decoded_image" if expectation_policy == CURRENT_EXPECTATION_POLICY
        and case.get("parameters", {}).get("size") == "auto" else "documented_custom_dimension_constraints"
    )
    return effective


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fixture_png(*, mask: bool = False) -> bytes:
    """Public deterministic fixtures; no user or private image material."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (1024, 1024), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    if mask:
        draw.rectangle((240, 240, 760, 800), fill=(0, 0, 0, 0))
    else:
        draw.rounded_rectangle((300, 270, 680, 760), radius=50, fill=(30, 70, 220, 255))
        draw.ellipse((610, 350, 800, 650), outline=(30, 70, 220, 255), width=45)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def fixture_url(*, mask: bool = False) -> str:
    return "data:image/png;base64," + base64.b64encode(fixture_png(mask=mask)).decode("ascii")


def responses_cases(model: str) -> list[dict]:
    if model not in MODELS and model not in SNAPSHOTS.values():
        raise ValueError("unrecognized exact GPT Image 2.5 tool model")
    cases: list[dict] = []

    def add(name: str, parameters: dict | None = None, *, input_kind: str = "text", outcome: str = "success",
            reject_field: str | None = None, stream: bool = False, description: str = "") -> None:
        tool = {"type": "image_generation", "model": model, "action": "generate", "size": "1024x1024",
                "quality": "low", "output_format": "png", "background": "opaque", **(parameters or {})}
        case = {"name": name, "case_id": model + "/responses/" + name, "tool_model": tool["model"],
                "mainline_model": MAINLINE_MODEL, "api_form": "openai_responses", "endpoint": ENDPOINT,
                "parameters": tool, "input_kind": input_kind, "expected_outcome": outcome,
                "stream": stream, "expected_count": 1, "description": description or name.replace("_", " "),
                "official_references": list(DOCS)}
        if reject_field:
            case["expected_rejection_field"] = reject_field
        if input_kind in {"previous_response", "image_call_id"}:
            case["depends_on"] = model + "/responses/multiturn_seed"
        cases.append(case)

    add("baseline_generate")
    if model in MODELS:
        add("dated_snapshot", {"model": SNAPSHOTS[model]})
    for quality in ("medium", "high", "xhigh", "max", "auto"):
        add("quality_" + quality, {"quality": quality})
    add("action_auto", {"action": "auto"})
    add("size_auto", {"size": "auto"})
    for label, size in (("landscape", "1536x1024"), ("portrait", "1024x1536"),
                        ("custom", "1536x864"), ("min_pixels", "1024x640"),
                        ("aspect_boundary", "1536x512"), ("4k_experimental", "3840x2160")):
        add("size_" + label, {"size": size})
    add("background_auto", {"background": "auto"})
    add("transparent_png", {"background": "transparent"})
    add("transparent_webp", {"background": "transparent", "output_format": "webp", "output_compression": 100})
    add("jpeg_compression_0", {"output_format": "jpeg", "output_compression": 0})
    add("jpeg_compression_50", {"output_format": "jpeg", "output_compression": 50})
    add("webp_compression_100", {"output_format": "webp", "output_compression": 100})
    add("moderation_low", {"moderation": "low"})
    add("edit_base64", {"action": "edit"}, input_kind="base64")
    add("edit_file_id", {"action": "edit"}, input_kind="file_id")
    add("edit_mask_base64", {"action": "edit", "input_image_mask": {"image_url": "fixture:mask"}}, input_kind="base64")
    add("edit_mask_file_id", {"action": "edit", "input_image_mask": {"file_id": "fixture:mask"}}, input_kind="file_id")
    add("multiturn_seed")
    add("edit_previous_response_id", {"action": "edit"}, input_kind="previous_response")
    add("edit_image_call_id", {"action": "edit"}, input_kind="image_call_id")
    for fidelity in ("low", "high"):
        add("input_fidelity_" + fidelity + "_observation", {"action": "edit", "input_fidelity": fidelity},
            input_kind="base64", outcome="observation", reject_field="input_fidelity",
            description="Observe model-specific fidelity behavior; generic schema does not establish a 2.5-specific contract.")
    for count in (0, 2):
        add("stream_partial_" + str(count), {"partial_images": count}, stream=True)
    for name, parameters, field in (
        ("quality", {"quality": "ultra"}, "quality"),
        ("action", {"action": "replace"}, "action"),
        ("size_multiple", {"size": "1537x864"}, "size"),
        ("size_aspect", {"size": "3072x768"}, "size"),
        ("size_min_pixels", {"size": "512x512"}, "size"),
        ("size_max_edge", {"size": "3856x2048"}, "size"),
        ("size_max_pixels", {"size": "3072x3072"}, "size"),
        ("transparent_jpeg", {"background": "transparent", "output_format": "jpeg"}, "background"),
        ("format", {"output_format": "gif"}, "output_format"),
        ("compression_low", {"output_format": "jpeg", "output_compression": -1}, "output_compression"),
        ("compression_high", {"output_format": "webp", "output_compression": 101}, "output_compression"),
        ("partial_images", {"partial_images": 4}, "partial_images"),
        ("moderation", {"moderation": "off"}, "moderation"),
        ("input_fidelity", {"input_fidelity": "medium", "action": "edit"}, "input_fidelity"),
    ):
        add("reject_" + name, parameters, outcome="rejection", reject_field=field,
            input_kind="base64" if name == "input_fidelity" else "text", stream=name == "partial_images")
    add("reject_edit_without_image", {"action": "edit"}, outcome="rejection", reject_field="action")
    return cases


def build_request(case: dict, *, dependency: dict | None = None, file_ids: dict | None = None) -> dict:
    tool = copy.deepcopy(case["parameters"])
    kind = case["input_kind"]
    prompt = EDIT_PROMPT if kind != "text" else TRANSPARENT_PROMPT if tool.get("background") == "transparent" else PROMPT
    body: dict = {"model": MAINLINE_MODEL, "input": prompt, "tools": [tool],
                  "tool_choice": {"type": "image_generation"}, "max_output_tokens": MAX_OUTPUT_TOKENS,
                  "reasoning": {"effort": "low"}, "store": case["name"] == "multiturn_seed", "stream": case["stream"]}
    if kind in {"base64", "file_id"}:
        image = {"type": "input_image", "detail": "auto"}
        if kind == "base64":
            image["image_url"] = fixture_url()
        else:
            image["file_id"] = require_id((file_ids or {}).get("image"), "file")
        body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": prompt}, image]}]
    if tool.get("input_image_mask", {}).get("image_url") == "fixture:mask":
        tool["input_image_mask"]["image_url"] = fixture_url(mask=True)
    if tool.get("input_image_mask", {}).get("file_id") == "fixture:mask":
        tool["input_image_mask"]["file_id"] = require_id((file_ids or {}).get("mask"), "file")
    if kind in {"previous_response", "image_call_id"}:
        if not dependency or dependency.get("verdict", {}).get("pass") is not True:
            raise ValueError("stateful case requires a verified seed from this exact package")
        if dependency.get("case_id") != case.get("depends_on"):
            raise ValueError("stateful dependency belongs to another model or case")
        if kind == "previous_response":
            body["previous_response_id"] = require_id(dependency.get("response", {}).get("id"), "resp")
        else:
            calls = [item for item in dependency.get("response", {}).get("output", []) if item.get("type") == "image_generation_call"]
            if len(calls) != 1:
                raise ValueError("seed must contain exactly one image tool call")
            body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                             {"type": "image_generation_call", "id": require_id(calls[0].get("id"), "ig")}]
    return body


def require_id(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"[-_][A-Za-z0-9_-]{1,200}", value):
        raise ValueError("invalid " + prefix + " identifier")
    return value


def expand_responses_case_dependencies(cases: list[dict], selected_case_ids: list[str] | None = None) -> list[dict]:
    """Select in matrix order, including prerequisites before their consumers.

    The supplied matrix is the allowed suite, so an excluded prerequisite is
    an error instead of permission to reach outside the previewed selection.
    Rows are returned unchanged to preserve frozen case definitions.
    """
    by_id = {case["case_id"]: case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError("duplicate Responses image case identifiers")
    selected = set(by_id if selected_case_ids is None else selected_case_ids)
    unknown = selected - by_id.keys()
    if unknown:
        raise ValueError("unknown Responses image case identifiers: " + ", ".join(sorted(unknown)))
    result: list[dict] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(case_id: str) -> None:
        if case_id in visiting:
            raise ValueError("cyclic Responses image case dependency: " + case_id)
        if case_id in visited:
            return
        if case_id not in by_id:
            raise ValueError("Responses image dependency unavailable in selected suite: " + case_id)
        visiting.add(case_id)
        dependency = by_id[case_id].get("depends_on")
        if dependency is not None:
            if not isinstance(dependency, str) or not dependency:
                raise ValueError("invalid Responses image case dependency")
            visit(dependency)
        visiting.remove(case_id)
        visited.add(case_id)
        result.append(by_id[case_id])

    for case in cases:
        if case["case_id"] in selected:
            visit(case["case_id"])
    return result


def select_responses_image_cases(model: str, cases: list[ImageTestCase],
                                selected_names: list[str] | None = None) -> list[ImageTestCase]:
    """Apply the raw matrix dependency graph to the console's allowed cases."""
    by_name = {case.name: case for case in cases}
    if len(by_name) != len(cases):
        raise ValueError("duplicate Responses image case names")
    selected = list(by_name) if selected_names is None else selected_names
    unknown = set(selected) - by_name.keys()
    if unknown:
        raise ValueError("case not in selected Responses image suite: " + ", ".join(sorted(unknown)))
    by_id = {case.metadata["case_id"]: case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError("duplicate Responses image case identifiers")
    raw_matrix = responses_cases(model)
    if set(by_id) - {case["case_id"] for case in raw_matrix}:
        raise ValueError("Responses image case belongs to another model or matrix")
    raw_selected = expand_responses_case_dependencies(
        [case for case in raw_matrix if case["case_id"] in by_id],
        [by_name[name].metadata["case_id"] for name in selected],
    )
    return [by_id[case["case_id"]] for case in raw_selected]


def responses_case_request_cap(cases: list[dict]) -> int:
    """Count generations plus fixture uploads and deletes, without retries."""
    return sum(1 + (4 if case["name"] == "edit_mask_file_id" else 2 if case["input_kind"] == "file_id" else 0)
               for case in cases)


def responses_image_request_cap(model: str, cases: list[ImageTestCase]) -> int:
    selected_ids = {case.metadata["case_id"] for case in cases}
    rows = expand_responses_case_dependencies(responses_cases(model), list(selected_ids))
    if selected_ids != {case["case_id"] for case in rows}:
        raise ValueError("Responses image request budget requires all dependencies in the plan")
    return responses_case_request_cap(rows)


def responses_image_cases(model: str, suite: str = "full", *, include_4k: bool = True,
                          include_negative: bool = True,
                          expectation_policy: str = FROZEN_EXPECTATION_POLICY) -> list[ImageTestCase]:
    """Image console/MPDB adapter; stateful IDs remain explicit templates."""
    if suite not in {"smoke", "resolution", "full"}:
        raise ValueError("invalid image suite")
    _require_expectation_policy(expectation_policy)
    rows = responses_cases(model)
    if suite == "smoke":
        rows = [case for case in rows if case["name"] == "baseline_generate"]
    rows = [case for case in rows if (include_4k or case["name"] != "size_4k_experimental")
            and (include_negative or effective_case_expectations(case, expectation_policy=expectation_policy)["expected_outcome"] != "rejection")]
    result = []
    for case in rows:
        dependency = {"case_id": case.get("depends_on"), "verdict": {"pass": True},
                      "response": {"id": "resp_fixture_seed", "output": [{"type": "image_generation_call", "id": "ig_fixture_seed"}]}}
        parameters = build_request(case, dependency=dependency, file_ids={"image": "file_fixture_image", "mask": "file_fixture_mask"})
        size = case["parameters"].get("size", "auto")
        expected_size = tuple(map(int, size.split("x"))) if size != "auto" and case["expected_outcome"] != "rejection" else None
        effective = effective_case_expectations(case, expectation_policy=expectation_policy)
        policy_metadata = ({key: effective[key] for key in (
            "expectation_policy", "frozen_expected_outcome", "effective_expected_outcome", "auto_output_size_validation"
        )} if expectation_policy != FROZEN_EXPECTATION_POLICY else {})
        result.append(ImageTestCase(
            name="gpt_image_25_responses_" + case["name"], parameters=parameters, expected_outcome=effective["expected_outcome"],
            expected_size=expected_size, expected_format=case["parameters"]["output_format"].upper(),
            description=case["description"], tags=("gpt_image_25", "responses", case["input_kind"]),
            metadata={"operation": "responses", "gpt_image_25_responses": True, "request_template_only": True,
                      "case_id": case["case_id"], "tool_model": case["tool_model"], "mainline_model": MAINLINE_MODEL,
                      "test_profile": "gpt_image_25_responses_" + case["name"], "expected_count": 1,
                      "depends_on": case.get("depends_on"), "official_references": list(DOCS)},
        ))
        if policy_metadata:
            result[-1].metadata.update(policy_metadata)
    return result


def validate_case(case: dict) -> None:
    # Exact reconstruction prevents reviewed packages adding arbitrary requests.
    source = case["case_id"].split("/responses/", 1)[0]
    expected = next((row for row in responses_cases(source) if row["case_id"] == case["case_id"]), None)
    if expected != case:
        raise ValueError("case differs from source-scoped GPT Image 2.5 matrix")


def audit_usage(usage: Any) -> dict:
    data = usage if isinstance(usage, dict) else {}
    errors = []
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if type(data.get(key)) is not int or data[key] < 0:
            errors.append(key + "_missing_or_invalid")
    if not errors and data["input_tokens"] + data["output_tokens"] != data["total_tokens"]:
        errors.append("total_tokens_arithmetic")
    if type(data.get("input_tokens")) is int and data["input_tokens"] == 0:
        errors.append("input_tokens_zero")
    for side in ("input", "output"):
        detail = data.get(side + "_tokens_details")
        if not isinstance(detail, dict):
            errors.append(side + "_tokens_details_missing")
            continue
        for key, value in detail.items():
            if key.endswith("tokens") and (type(value) is not int or value < 0):
                errors.append(side + "." + key + "_invalid")
            elif key.endswith("tokens") and type(data.get(side + "_tokens")) is int and value > data[side + "_tokens"]:
                errors.append(side + "." + key + "_exceeds_total")
        required = "cached_tokens" if side == "input" else "reasoning_tokens"
        if type(detail.get(required)) is not int or detail.get(required, -1) < 0:
            errors.append(side + "." + required + "_missing_or_invalid")
        if all(type(detail.get(key)) is int for key in ("text_tokens", "image_tokens")):
            if detail["text_tokens"] + detail["image_tokens"] != data.get(side + "_tokens"):
                errors.append(side + "_modality_sum")
    return {"pass": not errors, "errors": errors, "reported_usage": data,
            "scope": "mainline_responses_usage_schema_and_arithmetic",
            "image_tool_token_accuracy": "unverified_without_separate_image_tool_usage"}


def decode_artifact(encoded: Any, path: Path, *, expected_size: str | None, expected_format: str | None,
                    background: str | None, partial: bool = False,
                    expectation_policy: str = FROZEN_EXPECTATION_POLICY) -> dict:
    _require_expectation_policy(expectation_policy)
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("missing or oversized image base64")
    raw = base64.b64decode(encoded, validate=True)
    info = inspect_image_bytes(raw, visual_forensics=False)
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        alpha_extrema = source.getchannel("A").getextrema() if "A" in source.getbands() else None
        if source.mode == "P" and "transparency" in source.info:
            alpha_extrema = source.convert("RGBA").getchannel("A").getextrema()
    errors = []
    if expected_format and info.format != expected_format.upper():
        errors.append("decoded_format_mismatch")
    if not partial:
        if expected_size and expected_size != "auto" and f"{info.width}x{info.height}" != expected_size:
            errors.append("decoded_size_mismatch")
        if expectation_policy != CURRENT_EXPECTATION_POLICY or expected_size != "auto":
            errors.extend(validate_gpt_image_2_size(f"{info.width}x{info.height}"))
        if background == "transparent" and (alpha_extrema is None or alpha_extrema[0] == 255):
            errors.append("no_transparent_pixels")
        if background == "transparent" and alpha_extrema is not None and alpha_extrema[1] == 0:
            errors.append("fully_transparent_empty_output")
        if background == "opaque" and alpha_extrema is not None and alpha_extrema[0] != 255:
            errors.append("unexpected_transparent_pixels")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)
    return {**info.public(), "path": str(path), "alpha_extrema": list(alpha_extrema) if alpha_extrema else None,
            "decoded": True, "pass": not errors, "errors": errors}


def audit_image_tool_usage(usage: Any, expected_output: int | None, *, require_output_details: bool = False) -> dict:
    """Only a separate image-tool usage object can verify image token counts."""
    if not isinstance(usage, dict):
        return {"pass": None, "image_output_token_accuracy_pass": None, "status": "unverified_missing_image_tool_usage"}
    errors = []
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            errors.append(key + "_missing_or_invalid")
    if not errors and usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        errors.append("total_tokens_arithmetic")
    detail = usage.get("input_tokens_details", {})
    if (not isinstance(detail, dict) or any(type(detail.get(key)) is not int or detail[key] < 0 for key in ("text_tokens", "image_tokens"))
            or detail.get("text_tokens", 0) + detail.get("image_tokens", 0) != usage.get("input_tokens")):
        errors.append("input_modality_arithmetic")
    output_detail = usage.get("output_tokens_details")
    if require_output_details and output_detail is None:
        errors.append("output_tokens_details_missing")
    if output_detail is not None:
        if (not isinstance(output_detail, dict) or type(output_detail.get("image_tokens")) is not int
                or type(output_detail.get("text_tokens")) is not int
                or output_detail["image_tokens"] != usage.get("output_tokens")
                or output_detail.get("text_tokens") != 0):
            errors.append("image_only_output_arithmetic")
    for label, details in (("input", detail), ("output", output_detail)):
        if isinstance(details, dict):
            for key, count in details.items():
                if key.endswith("tokens") and (type(count) is not int or count < 0):
                    errors.append(label + "." + key + "_invalid")
                elif key.endswith("tokens") and type(usage.get(label + "_tokens")) is int and count > usage[label + "_tokens"]:
                    errors.append(label + "." + key + "_exceeds_total")
    reported_output = usage.get("output_tokens")
    comparable = expected_output is not None and type(reported_output) is int and reported_output >= 0
    allowed_delta = (max(IMAGE_TOKEN_ABSOLUTE_TOLERANCE, math.ceil(expected_output * IMAGE_TOKEN_RELATIVE_TOLERANCE))
                     if expected_output is not None else None)
    difference = reported_output - expected_output if comparable else None
    exact_match = reported_output == expected_output if comparable else None
    minimum = max(1, expected_output - allowed_delta) if allowed_delta is not None else None
    maximum = expected_output + allowed_delta if allowed_delta is not None else None
    accuracy = None if expected_output is None else not errors and comparable and minimum <= reported_output <= maximum
    return {"pass": not errors, "errors": errors, "reported_usage": usage,
            "expected_image_output_tokens": expected_output, "image_output_token_accuracy_pass": accuracy,
            "exact_match": exact_match, "allowed_delta": allowed_delta, "diff": difference,
            "absolute_diff": abs(difference) if difference is not None else None,
            "relative_diff": difference / expected_output if difference is not None and expected_output else None,
            "comparison_min": minimum, "comparison_max": maximum,
            "comparison_policy_source": "lib/image_token_expectations.py",
            "validation_scope": "official_image_token_estimate",
            "exact_output_count_verified": False,
            "relative_tolerance": IMAGE_TOKEN_RELATIVE_TOLERANCE,
            "absolute_tolerance": IMAGE_TOKEN_ABSOLUTE_TOLERANCE,
            "comparison_kind": ("exact_match" if exact_match is True else "within_existing_project_tolerance" if accuracy is True
                                else "outside_existing_project_tolerance" if accuracy is False else "unverified_without_formula"),
            "status": "pass" if accuracy is True else "fail" if accuracy is False else "unverified_without_formula",
            "image_input_token_accuracy": "unverified_without_independent_input_counter"}


def evaluate(case: dict, status: int | None, payload: Any, artifact_dir: Path, *, partial_count: int = 0,
             partial_counts_by_item_id: dict[str, int] | None = None,
             expectation_policy: str = FROZEN_EXPECTATION_POLICY) -> dict:
    case = effective_case_expectations(case, expectation_policy=expectation_policy)
    data = payload if isinstance(payload, dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    field = case.get("expected_rejection_field")
    param, message = str(error.get("param") or ""), str(error.get("message") or "")
    field_attributed = bool(field and (field in param or field in message))
    if expectation_policy == CURRENT_EXPECTATION_POLICY and case.get("name") in _FIDELITY_OBSERVATION_CASE_NAMES:
        field_attributed = (bool(re.search(r"(?<![A-Za-z0-9_])input_fidelity(?![A-Za-z0-9_])", param + " " + message))
                            or error.get("code") == "invalid_input_fidelity_model")
    # The API may explain an image-less edit using the image context rather than action.
    if case["name"] == "reject_edit_without_image":
        field_attributed = field_attributed or ("edit" in message.lower() and "image" in message.lower())
    if case["name"] == "reject_transparent_jpeg":
        field_attributed = field_attributed or ("transparent" in message.lower() and "jpeg" in message.lower())
    rejected = status in {400, 422} and field_attributed
    result: dict = {"pass": False, "expected_outcome": case["expected_outcome"], "http_status": status,
                   "frozen_expected_outcome": case["frozen_expected_outcome"],
                   "effective_expected_outcome": case["effective_expected_outcome"],
                   "expectation_policy": expectation_policy,
                   "auto_output_size_validation": case["auto_output_size_validation"],
                   "parameter_rejection_proven": rejected, "error_param": param, "error_code": error.get("code"),
                   "error_type": error.get("type"), "moderation_details": error.get("moderation_details"),
                   "image_tool_identity": "requested_exact_model_on_official_endpoint",
                   "image_tool_returned_identity": "unverified_unless_explicitly_returned",
                   "image_tool_token_accuracy": "unverified_without_separate_image_tool_usage",
                   "semantic_edit_quality": "unverified_requires_visual_comparison"}
    if status != 200:
        result["assessment"] = "expected_parameter_rejection" if rejected else (
            "account_or_model_access_unavailable" if status in {401, 403, 404} or error.get("code") == "model_not_found"
            else "moderation_blocked" if error.get("code") == "moderation_blocked" else "unexpected_error")
        result["pass"] = rejected and case["expected_outcome"] == "rejection"
        if type(status) is int and 200 <= status < 300 and case["expected_outcome"] == "rejection":
            result["assessment"] = "unexpected_acceptance"
        if case["expected_outcome"] == "observation" and rejected:
            result["assessment"] = "observed_parameter_rejection"
        return result
    raw_output = data.get("output") if isinstance(data.get("output"), list) else []
    output = [item for item in raw_output if isinstance(item, dict)]
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call"]
    envelope_ok = (len(output) == len(raw_output) and data.get("object") == "response" and data.get("status") == "completed"
                   and not data.get("error") and not data.get("incomplete_details")
                   and isinstance(data.get("id"), str) and data["id"].startswith("resp_")
                   and all(isinstance(item, dict) and item.get("type") in {"image_generation_call", "reasoning", "message"}
                           for item in output)
                   and all(item.get("status") == "completed" for item in output if item.get("type") == "message")
                   and all(block.get("type") != "refusal" for item in output if item.get("type") == "message"
                           for block in item.get("content", []) if isinstance(block, dict)))
    tool = case["parameters"]
    mainline_identity = data.get("model") == MAINLINE_MODEL
    usage = audit_usage(data.get("usage"))
    artifacts, metadata_errors, image_usage = [], [], []
    returned_tools = data.get("tools")
    returned_image_tools = [item for item in returned_tools if isinstance(item, dict) and item.get("type") == "image_generation"] if isinstance(returned_tools, list) else []
    result["image_tool_configuration_echo"] = "not_returned"
    if returned_tools is not None:
        allowed = {tool["model"], SNAPSHOTS.get(tool["model"], tool["model"])}
        echo_matches = len(returned_image_tools) == 1 and returned_image_tools[0].get("model") in allowed
        result["image_tool_configuration_echo"] = "matched_requested_configuration" if echo_matches else "mismatch"
        result["echoed_image_tool_models"] = [item.get("model") for item in returned_image_tools]
        result["image_tool_configuration_echo_is_backend_identity"] = False
        if not echo_matches:
            metadata_errors.append("image_tool_configuration_echo_mismatch")
    for index, item in enumerate(calls):
        if item.get("status") != "completed" or not item.get("id"):
            metadata_errors.append("image_call_not_completed")
        for key in ("quality", "size", "output_format", "background", "action"):
            if key in item and tool.get(key) not in {None, "auto"} and item[key] != tool[key]:
                metadata_errors.append("image_call_" + key + "_mismatch")
        if item.get("model") is not None:
            allowed = {tool["model"], SNAPSHOTS.get(tool["model"], tool["model"])}
            result["image_tool_returned_identity"] = "verified" if item["model"] in allowed else "mismatch"
            if item["model"] not in allowed:
                metadata_errors.append("image_tool_returned_model_mismatch")
        try:
            path = artifact_dir / (f"image_{index:02d}." + str(tool["output_format"]))
            artifacts.append(decode_artifact(item.get("result"), path, expected_size=tool.get("size"),
                                             expected_format=tool.get("output_format"), background=tool.get("background"),
                                             expectation_policy=expectation_policy))
            # Reuse the shared source-bound estimate, including explicitly
            # labelled adjacent-grid estimates for auto-sized images. Geometry
            # validation remains independent in decode_artifact above.
            expectation = image_output_token_expectation(
                tool["model"],
                {**tool, "n": 1, "stream": False,
                 "quality": item.get("quality") if tool.get("quality") == "auto" else tool.get("quality")},
                [artifacts[-1]], reference_source="openai",
            )
            artifacts[-1]["image_output_token_expectation"] = expectation
            artifacts[-1]["expected_final_image_output_tokens"] = (expectation or {}).get("tokens")
            artifacts[-1]["image_output_token_accuracy"] = "unverified_without_separate_image_tool_usage"
        except (ValueError, OSError, TypeError) as exc:
            artifacts.append({"pass": False, "decoded": False, "error_type": type(exc).__name__, "reason": str(exc)[:200]})
    partial_count_valid = type(partial_count) is int and partial_count >= 0
    if partial_counts_by_item_id is not None:
        partial_count_valid = (partial_count_valid and all(key in {item.get("id") for item in calls} for key in partial_counts_by_item_id)
                               and all(type(count) is int and count >= 0 for count in partial_counts_by_item_id.values())
                               and sum(partial_counts_by_item_id.values()) == partial_count)
    if not partial_count_valid:
        metadata_errors.append("invalid_observed_partial_count")
    expected_per_image = [row.get("expected_final_image_output_tokens") if row.get("decoded") else None for row in artifacts]
    for index, item in enumerate(calls):
        expected = expected_per_image[index] if index < len(expected_per_image) else None
        call_partials = (partial_counts_by_item_id.get(item.get("id"), 0) if partial_counts_by_item_id is not None
                         else partial_count if len(calls) == 1 else 0 if partial_count == 0 else None)
        expected_with_partial = expected + 100 * call_partials if expected is not None and call_partials is not None and partial_count_valid else None
        counted = audit_image_tool_usage(item.get("usage"), expected_with_partial)
        counted.update({"source_path": f"output[{output.index(item)}].usage", "scope": "individual_image_tool_call", "item_id": item.get("id")})
        image_usage.append(counted)
        if index < len(artifacts):
            artifacts[index]["image_output_token_accuracy"] = counted["status"]
        if counted["pass"] is False or counted["image_output_token_accuracy_pass"] is False:
            metadata_errors.append("separate_image_tool_usage_invalid")
    tool_usage = data.get("tool_usage")
    aggregate_present = isinstance(tool_usage, dict) and "image_gen" in tool_usage
    aggregate = None
    if aggregate_present:
        expected_sum = (sum(expected_per_image) + 100 * partial_count if calls and len(expected_per_image) == len(calls)
                        and all(type(count) is int for count in expected_per_image) and partial_count_valid else None)
        aggregate = audit_image_tool_usage(tool_usage["image_gen"], expected_sum, require_output_details=True)
        aggregate.update({"source_path": "tool_usage.image_gen", "scope": "aggregate_of_all_image_generation_calls",
                          "image_count": len(calls), "observed_partial_images": partial_count,
                          "partial_image_output_tokens": 100 * partial_count if partial_count_valid else None,
                          "per_call_allocation": "not_inferred_from_aggregate"})
        if not isinstance(tool_usage["image_gen"], dict):
            aggregate.update({"pass": False, "status": "fail", "errors": ["image_gen_must_be_object"]})
        if aggregate["pass"] is False or aggregate["image_output_token_accuracy_pass"] is False:
            metadata_errors.append("aggregate_image_tool_usage_invalid")
        actual_per_call = [item.get("usage") for item in calls]
        if isinstance(tool_usage["image_gen"], dict) and actual_per_call and all(isinstance(item, dict) and type(item.get("output_tokens")) is int for item in actual_per_call):
            if sum(item["output_tokens"] for item in actual_per_call) != tool_usage["image_gen"].get("output_tokens"):
                metadata_errors.append("aggregate_and_individual_image_usage_disagree")
        # An aggregate is an independently countable total. It does not expose
        # individual-call consumption, even when every expected image is equal.
        for artifact in artifacts:
            artifact["aggregate_image_output_token_accuracy"] = aggregate["status"]
    elif tool_usage is not None and not isinstance(tool_usage, dict):
        metadata_errors.append("tool_usage_must_be_object")
    image_ok = len(calls) == case["expected_count"] and all(row["pass"] for row in artifacts) and not metadata_errors
    accepted = envelope_ok and mainline_identity and usage["pass"] and image_ok
    result.update({"pass": accepted and case["expected_outcome"] == "success",
                   "assessment": "accepted_and_decoded" if accepted else "response_or_image_validation_failed",
                   "envelope_pass": envelope_ok, "mainline_identity_pass": mainline_identity,
                   "returned_mainline_model": data.get("model"), "usage": usage,
                   "image_count": len(calls), "artifacts": artifacts, "image_metadata_errors": metadata_errors,
                   "accepted_and_decoded": accepted})
    result["image_tool_usage"] = image_usage
    result["aggregate_image_tool_usage"] = aggregate
    result["image_output_token_exact_match"] = aggregate.get("exact_match") if aggregate is not None else (
        all(row.get("exact_match") is True for row in image_usage)
        if image_usage and all(row.get("exact_match") is not None for row in image_usage) else None)
    result["image_output_token_accuracy_pass"] = aggregate["image_output_token_accuracy_pass"] if aggregate is not None else (all(row["image_output_token_accuracy_pass"] is True for row in image_usage)
                                                  if image_usage and all(row["image_output_token_accuracy_pass"] is not None for row in image_usage) else None)
    if result["image_output_token_accuracy_pass"] is not None:
        result["image_tool_token_accuracy"] = "output_verified_input_unverified" if result["image_output_token_accuracy_pass"] else "image_output_token_mismatch"
    elif aggregate is not None:
        result["image_tool_token_accuracy"] = "output_formula_unverified_input_unverified"
    if case["expected_outcome"] == "rejection":
        result["assessment"] = "unexpected_acceptance"
    elif case["expected_outcome"] == "observation" and accepted:
        result["assessment"] = "observed_acceptance"
    return result


def public_payload(value: Any) -> Any:
    """Keep IDs, usage, errors and image digests, while removing large bytes."""
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key in {"result", "partial_image_b64", "image_url"} and isinstance(item, str) and (
                    key != "image_url" or item.startswith("data:")):
                output[key + "_sha256"] = hashlib.sha256(item.encode()).hexdigest()
                output[key + "_encoded_length"] = len(item)
            else:
                output[key] = public_payload(item)
        return output
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    return value


def validate_stream(events: list[dict], case: dict, artifact_dir: Path, *,
                    expectation_policy: str = FROZEN_EXPECTATION_POLICY) -> tuple[dict, dict]:
    """Require final completed envelope; partial images are never final proof."""
    _require_expectation_policy(expectation_policy)
    completions = [event for event in events if event.get("type") == "response.completed"]
    errors = []
    sequence = [event.get("sequence_number") for event in events]
    if any(type(number) is not int for number in sequence) or any(a >= b for a, b in zip(sequence, sequence[1:]) if type(a) is int and type(b) is int):
        errors.append("invalid_stream_sequence")
    if len(completions) != 1 or not events or events[-1].get("type") != "response.completed":
        errors.append("missing_or_nonterminal_completed_event")
    if any(event.get("type") in {"error", "response.failed", "response.incomplete"} for event in events):
        errors.append("stream_failure_event")
    payload = completions[0].get("response", {}) if len(completions) == 1 else {}
    calls = [item for item in payload.get("output", []) if isinstance(item, dict) and item.get("type") == "image_generation_call"]
    call_ids = {item.get("id") for item in calls}
    partials = [event for event in events if event.get("type") == "response.image_generation_call.partial_image"]
    cap = case["parameters"].get("partial_images", 0)
    indices = [event.get("partial_image_index") for event in partials]
    if len(partials) > cap or any(type(number) is not int for number in indices) or indices != list(range(len(partials))):
        errors.append("invalid_partial_count_or_order")
    decoded = []
    for index, event in enumerate(partials):
        if event.get("item_id") not in call_ids:
            errors.append("partial_image_item_id_mismatch")
        try:
            decoded.append(decode_artifact(event.get("partial_image_b64"), artifact_dir / f"partial_{index:02d}.png",
                                           expected_size=None, expected_format=None, background=None, partial=True,
                                           expectation_policy=expectation_policy))
        except (ValueError, OSError, TypeError) as exc:
            errors.append("partial_decode_" + type(exc).__name__)
    by_item = {}
    for event in partials:
        item_id = event.get("item_id")
        if isinstance(item_id, str):
            by_item[item_id] = by_item.get(item_id, 0) + 1
    return payload, {"pass": not errors, "errors": errors, "partial_count": len(partials), "partial_images": decoded,
                     "partial_counts_by_item_id": by_item,
                     "requested_partial_cap": cap, "partial_count_is_a_maximum": True}
