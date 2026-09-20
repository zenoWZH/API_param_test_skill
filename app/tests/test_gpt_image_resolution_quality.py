"""Offline proof for the independent size x quality reference factory."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest

from lib.gpt_image_resolution_quality import (
    CANONICAL_MODELS, MATRIX_ID, SNAPSHOTS,
    gpt_image_resolution_quality_cases,
    gpt_image_resolution_quality_responses_cases, supported_api_forms,
)
from lib.image_validation import validate_gpt_image_2_size


def _image_parameters(case):
    if case.metadata["api_form"] == "openai_responses":
        return case.parameters["tools"][0]
    return case.parameters


def test_exact_six_model_scope_matches_current_catalog_request_ids():
    root = Path(__file__).resolve().parents[1]
    if root.name == "app":
        root = root.parent
    catalog = json.loads((root / "packages/model-profile-db/model_profile_db/data/catalog.json").read_text())
    profiles = [row for key, row in catalog["profiles"].items() if key.startswith("image/openai/gpt-image-2/")]
    assert {row["model_slug"] for row in profiles} == set(CANONICAL_MODELS)
    assert {model for row in profiles for model in row["request_model_ids"]} == set(CANONICAL_MODELS) | set(SNAPSHOTS)


def test_all_ten_interfaces_have_independent_names_and_exact_outcome_counts():
    counts = {}
    all_names = []
    all_ids = []
    for model in CANONICAL_MODELS:
        for form in supported_api_forms(model):
            cases = gpt_image_resolution_quality_cases(model, api_form=form)
            counts[(model, form)] = len(cases)
            expected = ((9, 7, 6) if model in CANONICAL_MODELS[:3]
                        else (48, 20, 8) if model == "gpt-image-2" else (80, 22, 6))
            assert Counter(case.expected_outcome for case in cases) == dict(zip(("success", "observation", "rejection"), expected))
            all_names.extend(case.name for case in cases)
            all_ids.extend(case.metadata["case_id"] for case in cases)
            assert json.loads(json.dumps([case.public() for case in cases]))
    assert len(counts) == 10 and sum(counts.values()) == 790
    assert len(all_names) == len(set(all_names)) == len(set(all_ids))


@pytest.mark.parametrize("model", CANONICAL_MODELS)
def test_every_fixed_dimension_crosses_every_supported_quality(model):
    cases = gpt_image_resolution_quality_cases(model, include_auto=False, include_negative=False)
    by_size = {}
    for case in cases:
        params = _image_parameters(case)
        by_size.setdefault(params["size"], set()).add(params["quality"])
        assert not validate_gpt_image_2_size(params["size"])
        assert "resolution" not in params and "aspect_ratio" not in params
        assert case.metadata["pin_parameters"] is True
        assert case.metadata["resolution_tier_is_official_enum"] is False
        assert case.metadata["token_expectation"]["live_verified"] is False
        assert case.metadata["token_expectation"]["expectation"]["tokens"] > 0
    qualities = {"low", "medium", "high"}
    if model.startswith("gpt-image-2.5-"):
        qualities |= {"xhigh", "max"}
    assert all(actual == qualities for actual in by_size.values())
    if model in CANONICAL_MODELS[:3]:
        assert set(by_size) == {"1024x1024", "1024x1536", "1536x1024"}
    else:
        assert len(by_size) == 16
        assert {"2048x2048", "3840x2160", "2160x3840", "1024x640", "1536x512"} <= by_size.keys()
        assert "3840x3840" not in by_size and "4096x4096" not in by_size


def test_each_flexible_negative_size_violates_only_its_intended_constraint():
    cases = gpt_image_resolution_quality_cases("gpt-image-2")
    invalid = [case for case in cases if case.metadata["rejection_parameter"] == "size"]
    assert len(invalid) == 5
    for case in invalid:
        width, height = map(int, case.parameters["size"].split("x"))
        violations = {
            "grid": bool(width % 16 or height % 16),
            "aspect": max(width, height) > 3 * min(width, height),
            "min_pixels": width * height < 655_360,
            "max_pixels": width * height > 8_294_400,
            "max_edge": max(width, height) > 3840,
        }
        assert sum(violations.values()) == 1
        assert next(key for key, value in violations.items() if value) in case.name
        assert case.expected_size is None and case.expected_format is None
        assert case.metadata["token_expectation"]["expectation"] is None


def test_auto_is_observation_with_discrete_candidates_or_deferred_size():
    cases = gpt_image_resolution_quality_cases("gpt-image-2.5-flare")
    fixed = next(case for case in cases if case.expected_size == (1024, 1024) and case.parameters["quality"] == "auto")
    assert fixed.expected_outcome == "observation"
    ref = fixed.metadata["token_expectation"]
    assert ref["status"] == "documented_auto_quality_candidates"
    assert [row["tokens"] for row in ref["expectation"]["ranges"]] == [196, 439, 1756, 3122, 7024]
    automatic = [case for case in cases if case.parameters["size"] == "auto"]
    assert len(automatic) == 6
    for case in automatic:
        assert case.expected_size is None and case.expected_outcome == "observation"
        assert case.metadata["acceptance_required"] and case.metadata["decoded_image_required"]
        assert case.metadata["token_expectation"]["status"] == "deferred_until_decoded_size"


def test_legacy_portrait_and_landscape_preserve_distinct_token_references():
    cases = gpt_image_resolution_quality_cases("gpt-image-1-mini", include_auto=False, include_negative=False)
    low = {case.parameters["size"]: case.metadata["token_expectation"]["expectation"]["tokens"]
           for case in cases if case.parameters["quality"] == "low"}
    assert low == {"1024x1024": 272, "1536x1024": 400, "1024x1536": 408}


def test_responses_adapter_preserves_exact_image_model_and_native_tool_fields():
    from lib.gpt_image_25_responses import build_request

    model = "gpt-image-2.5-sunburst-2026-09-08"
    wrappers = gpt_image_resolution_quality_cases(model, api_form="openai_responses")
    raw = gpt_image_resolution_quality_responses_cases(model)
    assert len(raw) == len(wrappers) == 108
    for case, row in zip(wrappers, raw):
        assert row["name"] == case.name and row["case_id"] == case.metadata["case_id"]
        assert row["case_id"].startswith(model + "/responses/" + MATRIX_ID + "/")
        assert row["parameters"] == case.parameters["tools"][0]
        assert build_request(row) == case.parameters
        assert row["tool_model"] == row["parameters"]["model"] == model
        assert row["mainline_model"] == case.parameters["model"] == "gpt-6-astra"
        assert case.parameters["max_output_tokens"] >= 256
        assert row["input_kind"] == "text" and row["stream"] is False
        assert "n" not in row["parameters"] and "resolution" not in row["parameters"]
        assert not case.metadata.get("gpt_image_25_responses")
    raw[0]["parameters"]["quality"] = "max"
    assert wrappers[0].parameters["tools"][0]["quality"] == "low"
    assert gpt_image_resolution_quality_responses_cases(model)[0]["parameters"]["quality"] == "low"


def test_snapshot_identity_and_token_calculator_remain_separate():
    case = gpt_image_resolution_quality_cases("gpt-image-2-2026-04-21")[0]
    assert case.request_body("provider_alias", "prompt")["model"] == "gpt-image-2-2026-04-21"
    ref = case.metadata["token_expectation"]
    assert ref["request_model"] == "gpt-image-2-2026-04-21"
    assert ref["reference_model"] == "gpt-image-2" and ref["expectation"]["tokens"] == 196
    assert ref["requires_model_identity_evidence"] is True


@pytest.mark.parametrize("model,form", [
    ("gpt-image-2-req", "openai_images_generations"),
    ("gpt-image-2.5", "openai_images_generations"),
    ("gpt-image-1-2026-01-01", "openai_images_generations"),
    ("gpt-image-2", "openai_responses"),
    ("gpt-image-1.5", "openai_images_edits"),
    ("gpt-image-2.5-flare", "openai_chat_completions"),
])
def test_unrecognized_model_and_unbound_forms_fail_closed(model, form):
    with pytest.raises(ValueError):
        gpt_image_resolution_quality_cases(model, api_form=form)
