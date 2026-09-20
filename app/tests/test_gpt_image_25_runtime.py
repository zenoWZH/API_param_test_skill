from __future__ import annotations

import base64
import copy
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from lib.gpt_image_25 import (
    GPT_IMAGE_25_MODELS, decode_image_stream, edit_fixtures,
    fixture_manifest, gpt_image_25_cases, is_gpt_image_25,
)
from lib.image_token_expectations import image_output_token_expectation
from lib.image_validation import evaluate_case, inspect_image_bytes
from scripts import image_param_test as runner

MODEL = "gpt-image-2.5-sunburst"


def case(suffix="baseline", operation="generation"):
    return next(case for case in gpt_image_25_cases(MODEL, "full", operation=operation, include_4k=True)
                if case.name == f"gpt_image_25_{operation}_{suffix}")


def encoded(*, alpha=255, size=(1024, 1024), image_format="PNG"):
    output = io.BytesIO()
    image = Image.new("RGBA", size, (255, 100, 0, alpha))
    if image_format == "JPEG":
        image = image.convert("RGB")
    image.save(output, format=image_format)
    return base64.b64encode(output.getvalue()).decode("ascii")


def payload(image=None, *, usage=True):
    result = {"data": [{"b64_json": image or encoded()}]}
    if usage:
        result["usage"] = {"input_tokens": 20, "output_tokens": 196, "total_tokens": 216}
    return result


def run(tmp_path, selected, response_payload, *, code=200, mutate=None):
    sent = []
    def post(url, **kwargs):
        sent.append(copy.deepcopy(kwargs))
        if mutate:
            mutate(kwargs)
        return SimpleNamespace(status_code=code, headers={}, text="", json=lambda: response_payload)
    operation = selected.metadata["operation"]
    result = runner.run_case(
        SimpleNamespace(post=post), f"https://image-provider.example/v1/images/{'edits' if operation == 'edit' else 'generations'}",
        MODEL, "An orange square.", selected, timeout=5, images_dir=tmp_path, visual_forensics=False,
        transport="images-edits" if operation == "edit" else "images-generations",
    )
    return result, sent


@pytest.mark.parametrize("model", sorted(GPT_IMAGE_25_MODELS))
@pytest.mark.parametrize("operation", ["generation", "edit"])
def test_matrix_has_distinct_qualities_and_source_scoped_facts(model, operation):
    cases = gpt_image_25_cases(model, "full", operation=operation, include_4k=True)
    assert len(cases) == len({case.name for case in cases})
    assert {case.parameters["quality"] for case in cases if case.expected_outcome == "success"} == {
        "low", "medium", "high", "xhigh", "max", "auto"}
    assert all(case.metadata["operation"] == operation for case in cases)
    assert all(case.metadata["test_profile"] == case.name for case in cases)
    if operation == "edit":
        assert not any("moderation" in case.parameters for case in cases)
        assert all(case.expected_outcome == "observation" for case in cases
                   if case.parameters.get("input_fidelity") in {"high", "low"})


def test_bare_or_fuzzy_model_ids_do_not_inherit_25_facts():
    for model in ("gpt-image-2.5", "gpt-image-2", MODEL + "-custom"):
        assert not is_gpt_image_25(model)
        with pytest.raises(ValueError):
            gpt_image_25_cases(model)


def test_cli_defaults_cannot_overwrite_authored_new_quality_and_format_probes():
    for suffix in ("quality_medium", "quality_high", "quality_xhigh", "quality_max", "format_webp", "reject_quality"):
        selected = case(suffix)
        assert runner._with_output_options(selected, "low", "jpeg") == selected


@pytest.mark.parametrize("operation", ["generation", "edit"])
def test_endpoint_normalization_and_model_list_are_operation_specific(operation):
    transport = "images-generations" if operation == "generation" else "images-edits"
    suffix = "generations" if operation == "generation" else "edits"
    endpoint = runner.normalize_image_endpoint("https://api.openai.com/v1", transport)
    assert endpoint == f"https://api.openai.com/v1/images/{suffix}"
    assert runner.models_endpoint(endpoint, transport) == "https://api.openai.com/v1/models"
    with pytest.raises(ValueError, match="does not match"):
        runner.normalize_image_endpoint(endpoint, "images-edits" if operation == "generation" else "images-generations")


def test_edit_sender_uses_multipart_and_records_exact_fixture_evidence(tmp_path):
    result, sent = run(tmp_path, case("mask", "edit"), payload())
    assert "json" not in sent[0]
    assert sent[0]["data"]["model"] == MODEL
    assert [name for name, _ in sent[0]["files"]] == ["image[]", "mask"]
    assert result["input_fixtures"] == fixture_manifest(case("mask", "edit"))
    assert result["request_input_integrity"]["status"] == "pass"
    assert result["api_form"] == "openai_images_edits"


def test_multipart_mutation_cannot_rewrite_audit_snapshot(tmp_path):
    result, _ = run(tmp_path, case(operation="edit"), payload(),
                    mutate=lambda wire: wire["data"].update({"prompt": "Different prompt"}))
    assert result["request_input_integrity"]["status"] == "fail"
    assert "image_request_mutated_during_send" in result["failures"]
    assert result["overall_pass"] is False


@pytest.mark.parametrize("suffix, expected_format", [("input_jpeg", "JPEG"), ("input_webp", "WEBP")])
def test_edit_input_formats_are_real_encoded_images(suffix, expected_format):
    file = edit_fixtures(case(suffix, "edit"))[0][1]
    assert inspect_image_bytes(file[1], visual_forensics=False).format == expected_format


def test_masks_have_real_transparent_pixels_and_matching_dimensions():
    files = edit_fixtures(case("mask", "edit"))
    info = [inspect_image_bytes(value[1], visual_forensics=False) for _, value in files]
    assert (info[0].width, info[0].height) == (info[1].width, info[1].height)
    assert info[1].has_transparent_pixels is True
    assert all(item.byte_length < 4 * 1024 * 1024 for item in info)


@pytest.mark.parametrize("alpha, expected", [(255, False), (0, False)])
def test_transparency_requires_nonopaque_pixels_not_rgba_channel(alpha, expected):
    image = inspect_image_bytes(base64.b64decode(encoded(alpha=alpha)), visual_forensics=False)
    assert image.has_alpha is True
    assert evaluate_case(case("transparent_png"), status_code=200, images=[image])["pass"] is expected


def test_semantic_workflows_require_manual_review_even_when_image_decodes(tmp_path):
    result, _ = run(tmp_path, case("layout_translation", "edit"), payload())
    assert result["semantic_validation"]["status"] == "manual_review_required"
    assert result["semantic_validation"]["pass"] is None


@pytest.mark.parametrize("code,error", [(403, {"message": "Organization must be verified", "type": "permission_error"}),
                                        (400, {"message": "API key invalid", "type": "invalid_request_error"})])
def test_auth_or_unrelated_errors_cannot_pass_negative_parameter_cases(tmp_path, code, error):
    result, _ = run(tmp_path, case("reject_quality"), {"error": error}, code=code)
    assert result["overall_pass"] is False
    assert result["pass"] is False


def test_attributed_negative_case_passes_without_synthetic_usage(tmp_path):
    result, _ = run(tmp_path, case("reject_quality"), {"error": {"message": "Invalid quality ultra", "param": "quality"}}, code=400)
    assert result["pass"] is True
    assert result["parameter_rejection_attribution"]["matched"] is True
    assert result["usage"] == {}


def test_missing_usage_still_fails_token_gate_for_new_model(tmp_path):
    result, _ = run(tmp_path, case(), payload(usage=False))
    assert result["compatibility_pass"] is True
    assert result["token_validation_pass"] is False
    assert result["overall_pass"] is False


def events_response(events):
    lines = []
    for event in events:
        lines.extend(["data: " + json.dumps(event), ""])
    return SimpleNamespace(headers={"content-type": "text/event-stream"},
                           iter_lines=lambda decode_unicode: iter(lines), close=lambda: None,
                           status_code=200)


@pytest.mark.parametrize("operation,prefix", [("generation", "image_generation"), ("edit", "image_edit")])
def test_stream_only_counts_completed_artifact_and_allows_fewer_partials(operation, prefix):
    selected = case("stream_partials", operation)
    final = {"type": prefix + ".completed", "b64_json": encoded(), "usage": {"input_tokens": 20, "output_tokens": 296, "total_tokens": 316}}
    partial = {"type": prefix + ".partial_image", "b64_json": encoded(), "partial_image_index": 0}
    response, metadata = decode_image_stream(events_response([partial, final]), selected)
    assert len(response["data"]) == 1
    assert metadata["partial_image_indices"] == [0]
    assert metadata["completion_events"] == 1
    assert metadata["failures"] == []


def test_partial_only_stream_fails_completion():
    response, metadata = decode_image_stream(events_response([
        {"type": "image_generation.partial_image", "b64_json": encoded(), "partial_image_index": 0}]), case("stream_partials"))
    assert response["data"] == []
    assert "image_stream_completion_count_mismatch" in metadata["failures"]


def test_invalid_partial_bytes_and_duplicate_index_cannot_pass():
    events = [{"type": "image_generation.partial_image", "b64_json": "AAAA", "partial_image_index": 0}] * 2
    _, metadata = decode_image_stream(events_response(events), case("stream_partials"))
    assert "invalid_partial_image_index" in metadata["failures"]
    assert "image_stream_partial_decode_failed" in metadata["failures"]


def test_wrong_endpoint_completion_event_does_not_supply_final_image():
    response, metadata = decode_image_stream(events_response([
        {"type": "image_edit.completed", "b64_json": encoded()}]), case("stream_final_only"))
    assert response["data"] == []
    assert metadata["failures"]


def test_25_high_quality_does_not_use_legacy_image2_calculator():
    images = [{"width": 1024, "height": 1024}]
    new = image_output_token_expectation(MODEL, {"quality": "high"}, images)
    old = image_output_token_expectation("gpt-image-2", {"quality": "high"}, images)
    assert new["nominal_min"] == 1756
    assert old["nominal_min"] == 7024
    assert new["calculator_source"] != old["calculator_source"]


def test_conflicting_documentation_observation_does_not_certify_acceptance(tmp_path):
    result, _ = run(tmp_path, case("png_compression_observation"), payload())
    assert result["diagnostic_pass"] is True
    assert result["status"] == "observed_acceptance"
    assert result["pass"] is False
    assert result["overall_pass"] is False
    assert result["compatibility_status"] == "not_certified"


def test_accepted_observation_still_requires_exact_dimensions(tmp_path):
    result, _ = run(tmp_path, case("png_compression_observation"), payload(encoded(size=(512, 512))))
    assert result["diagnostic_pass"] is False
    assert result["status"] == "unresolved"
    assert any("dimension_mismatch" in value for value in result["failures"])


def test_observation_rejection_must_attribute_the_probed_parameter(tmp_path):
    result, _ = run(tmp_path, case("input_fidelity_high", "edit"),
                    {"error": {"message": "Account configuration invalid"}}, code=400)
    assert result["diagnostic_pass"] is False
    assert result["status"] == "unresolved"


def test_echoed_quality_cannot_silently_downgrade_the_request(tmp_path):
    result, _ = run(tmp_path, case(), {**payload(), "quality": "medium"})
    assert result["overall_pass"] is False
    assert "returned_parameter_mismatch:quality" in result["failures"]


def test_auto_size_output_must_stay_within_documented_bounds(tmp_path):
    result, _ = run(tmp_path, case("size_auto"), payload(encoded(size=(512, 512))))
    assert result["compatibility_pass"] is False
    assert "auto_size_outside_documented_bounds:index=0" in result["failures"]


def test_stream_runner_records_terminal_usage_and_observed_partial_count(tmp_path):
    image = encoded()
    stream_response = events_response([
        {"type": "image_generation.partial_image", "b64_json": image, "partial_image_index": 0},
        {"type": "image_generation.completed", "b64_json": image,
         "usage": {"input_tokens": 20, "output_tokens": 296, "total_tokens": 316}},
    ])
    result = runner.run_case(SimpleNamespace(post=lambda *args, **kwargs: stream_response),
                             "https://api.openai.com/v1/images/generations", MODEL, "An orange square.",
                             case("stream_partials"), timeout=5, images_dir=tmp_path, visual_forensics=False)
    assert result["image_stream"]["completion_events"] == 1
    assert result["usage"]["output_tokens"] == 296
    assert result["token_validation_pass"] is True


@pytest.mark.skipif(not hasattr(runner, "_execute_image_cases"), reason="root-only bounded executor; app retains its existing loop")
def test_access_block_stops_remaining_model_cases_and_preserves_plan(tmp_path):
    cases = gpt_image_25_cases(MODEL, "resolution")
    calls = []
    def execute(selected):
        calls.append(selected.name)
        return {"case": selected.name, "status_code": 403, "pass": False, "status": "fail"}
    rows, budget = runner._execute_image_cases(cases, execute, report_dir=tmp_path, reference_observation=False)
    assert len(calls) == len(rows) == 1
    assert budget["stop_reason"] == "model_access_blocked"
    assert budget["not_executed_count"] == len(cases) - 1


def test_transparent_object_requires_both_visible_and_nonopaque_pixels():
    output = io.BytesIO()
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    image.putpixel((512, 512), (255, 100, 0, 255))
    image.save(output, format="PNG")
    info = inspect_image_bytes(output.getvalue(), visual_forensics=False)
    assert info.has_transparent_pixels is True
    assert info.has_visible_pixels is True
    assert evaluate_case(case("transparent_png"), status_code=200, images=[info])["pass"] is True


def test_semantic_edit_pending_prevents_overall_pass_and_rejects_unchanged_fixture(tmp_path):
    selected = case("precision_recolor", "edit")
    image = base64.b64encode(edit_fixtures(selected)[0][1][1]).decode("ascii")
    result, _ = run(tmp_path, selected, payload(image))
    assert result["overall_pass"] is False
    assert result["semantic_validation"]["unchanged_input_detected"] is True
    assert "edit_input_unchanged" in result["failures"]


def test_edit_prepared_http_request_has_multipart_boundary_even_with_json_session_default(tmp_path):
    import requests
    session = requests.Session()
    session.headers["Content-Type"] = "application/json"
    prepared_requests = []
    def send(prepared, **kwargs):
        prepared_requests.append(prepared)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload()).encode("utf-8")
        return response
    session.send = send
    runner.run_case(session, "https://api.openai.com/v1/images/edits", MODEL, "Edit the square.",
                    case("mask", "edit"), timeout=5, images_dir=tmp_path, visual_forensics=False,
                    transport="images-edits")
    request = prepared_requests[0]
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    boundary = request.headers["Content-Type"].split("boundary=", 1)[1].encode("ascii")
    assert request.body.startswith(b"--" + boundary)
    assert b'name="image[]"' in request.body
    assert b'name="mask"' in request.body
    session.close()


def test_auto_quality_count_uses_resolved_quality_without_mutating_request(tmp_path):
    result, sent = run(tmp_path, case("quality_auto"), {**payload(), "quality": "high"})
    assert sent[0]["json"]["quality"] == "auto"
    assert result["request_input_integrity"]["status"] == "pass"
    assert result["response_parameter_evidence"]["quality"]["status"] == "auto_resolved"
    assert result["token_validation_pass"] is False


def test_invalid_auto_quality_echo_cannot_pass(tmp_path):
    result, _ = run(tmp_path, case("quality_auto"), {**payload(), "quality": "ultra"})
    assert result["compatibility_pass"] is False
    assert "returned_parameter_mismatch:quality" in result["failures"]


def binding_fixture():
    cases = gpt_image_25_cases(MODEL, "full", include_4k=True)
    return {"source_id": "openai", "reference_model_id": MODEL,
            "interface": {"api_form": "openai_images_generations", "request_model_ids": [MODEL]},
            "parameter_test_binding": {"test_cases": [case.name for case in cases],
                                       "case_definitions": [case.public() for case in cases]}}


def test_frozen_case_definitions_are_required_and_cannot_drift():
    from lib.gpt_image_25 import validate_case_binding
    binding = binding_fixture()
    validate_case_binding(binding, MODEL, "generation")
    binding["parameter_test_binding"]["case_definitions"][0]["parameters"]["quality"] = "high"
    with pytest.raises(ValueError, match="immutable"):
        validate_case_binding(binding, MODEL, "generation")
    binding = binding_fixture()
    del binding["parameter_test_binding"]["case_definitions"]
    with pytest.raises(ValueError, match="immutable"):
        validate_case_binding(binding, MODEL, "generation")


def test_stream_cannot_deliver_partial_after_completed():
    _, metadata = decode_image_stream(events_response([
        {"type": "image_generation.completed", "b64_json": encoded()},
        {"type": "image_generation.partial_image", "b64_json": encoded(), "partial_image_index": 0},
    ]), case("stream_partials"))
    assert "image_stream_event_after_completion" in metadata["failures"]


def test_stream_without_final_blank_event_delimiter_is_incomplete():
    event = {"type": "image_generation.completed", "b64_json": encoded()}
    response = SimpleNamespace(headers={"content-type": "text/event-stream"},
                               iter_lines=lambda decode_unicode: iter(["data: " + json.dumps(event)]))
    payload, metadata = decode_image_stream(response, case("stream_final_only"))
    assert payload["data"] == []
    assert "image_stream_truncated_event" in metadata["failures"]
