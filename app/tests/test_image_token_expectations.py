from __future__ import annotations

import pytest

from lib.gpt_image_25_tokens import gpt_image_25_output_tokens
from lib.image_token_expectations import (
    GOOGLE_IMAGE_GUIDE,
    GOOGLE_PRICING,
    GOOGLE_VERTEX_PRICING,
    gpt_image_2_output_tokens,
    image_output_token_expectation,
)


@pytest.mark.parametrize("width,height,quality,tokens", [
    (1024, 1024, "low", 196),
    (1024, 1024, "medium", 1756),
    (1024, 1024, "high", 7024),
    (1536, 1024, "low", 158),
    (1024, 1536, "low", 158),
])
def test_official_gpt_image_2_calculator_examples(width, height, quality, tokens):
    assert gpt_image_2_output_tokens(width, height, quality) == tokens


@pytest.mark.parametrize("width,height", [
    (512, 512), (1537, 1024), (4096, 2048), (3072, 768), (True, 1024),
])
def test_invalid_gpt_image_2_size_cannot_produce_count_evidence(width, height):
    assert gpt_image_2_output_tokens(width, height, "low") is None


@pytest.mark.parametrize("model,width,height,expected", [
    ("gemini-2.5-flash-image", 1024, 1024, 1290),
    ("gemini-3.1-flash-image", 512, 512, 747),
    ("gemini-3.1-flash-image", 1376, 768, 1120),
    ("gemini-3.1-flash-image", 2048, 2048, 1680),
    ("gemini-3.1-flash-image", 4096, 4096, 2520),
    ("gemini-3-pro-image", 2048, 2048, 1120),
    ("gemini-3-pro-image", 4096, 4096, 2000),
    ("gemini-3.1-flash-lite-image", 1024, 1024, 1120),
])
def test_google_output_table_is_model_and_resolution_specific(model, width, height, expected):
    evidence = image_output_token_expectation(
        model, {}, [{"width": width, "height": height}],
        reference_source="google_ai_studio",
    )
    assert evidence["tokens"] == expected
    assert evidence["min"] <= expected <= evidence["max"]
    assert evidence["evidence_level"] == "official_range"
    assert evidence["source"] == GOOGLE_PRICING
    assert evidence["dimension_source"] == GOOGLE_IMAGE_GUIDE
    assert evidence["source_checked_at"] == "2026-09-09"
    assert evidence["estimate_basis"] == "official_resolution_table"
    assert evidence["quantity_is_estimate"] is True
    assert evidence["dimension_contract_support"] == "not_evaluated"
    assert evidence["max"] < expected * 1.2


def test_model_alias_or_wrong_source_is_not_assumed_to_have_official_counts():
    image = [{"width": 1024, "height": 1024}]
    assert image_output_token_expectation("nano-banana-pro-1k", {}, image) is None
    assert image_output_token_expectation("gpt-image-2", {}, image, reference_source="xai") is None
    assert image_output_token_expectation("gemini-3.1-flash-lite-image", {}, [{"width": 2048, "height": 2048}]) is None


def test_multiple_images_and_auto_quality_have_explicit_finite_ranges():
    images = [{"width": 1024, "height": 1024}] * 2
    evidence = image_output_token_expectation("gpt-image-2", {"quality": "low"}, images)
    assert evidence["tokens"] == 392
    assert evidence["image_count"] == 2
    auto = image_output_token_expectation("gpt-image-2", {"quality": "auto"}, images[:1])
    assert auto["nominal_min"] == 196
    assert auto["nominal_max"] == 7024
    assert auto["tokens"] is None
    assert [item["tokens"] for item in auto["ranges"]] == [196, 1756, 7024]
    assert not any(item["min"] <= 4000 <= item["max"] for item in auto["ranges"])


@pytest.mark.parametrize("source", ["openai_other", "openai.evil", "azure_openai_untrusted", "google_ai_studio_evil", "gemini_proxy"])
def test_formula_source_matching_requires_exact_known_source_ids(source):
    model = "gpt-image-2" if source.startswith(("openai", "azure")) else "gemini-3.1-flash-image"
    assert image_output_token_expectation(model, {}, [{"width": 1024, "height": 1024}], reference_source=source) is None


@pytest.mark.parametrize("count", [0, True, "2", 1, 3])
def test_requested_image_count_must_match_decoded_artifact_count(count):
    images = [{"width": 1024, "height": 1024}] * 2
    assert image_output_token_expectation("gpt-image-2", {"n": count, "quality": "low"}, images) is None


def test_auto_batch_quality_ranges_sum_actual_image_choices():
    images = [{"width": 1024, "height": 1024}] * 2
    expectation = image_output_token_expectation("gpt-image-2", {"n": 2, "quality": "auto"}, images)
    assert [item["tokens"] for item in expectation["ranges"]] == [392, 1952, 3512, 7220, 8780, 14048]
    assert not any(item["min"] <= 5000 <= item["max"] for item in expectation["ranges"])


def test_latest_alias_does_not_imply_the_legacy_formula():
    assert image_output_token_expectation("chatgpt-image-latest", {"quality": "low"}, [{"width": 1024, "height": 1024}], reference_source="openai") is None


@pytest.mark.parametrize("width,height", [(384, 3072), (3072, 384)])
def test_lite_unlisted_dimensions_outside_nominal_area_tolerance_remain_unverified(width, height):
    assert image_output_token_expectation("gemini-3.1-flash-lite-image", {}, [{"width": width, "height": height}], reference_source="google_ai_studio") is None


@pytest.mark.parametrize("model,width,height,tier,edge,tokens", [
    ("gemini-3.1-flash-image", 784, 336, "512", 512, 747),
    ("gemini-3.1-flash-image", 352, 2928, "1K", 1024, 1120),
    ("gemini-3.1-flash-lite-image", 352, 2928, "1K", 1024, 1120),
    ("gemini-3.1-flash-lite-image", 512, 2048, "1K", 1024, 1120),
    ("gemini-3.1-flash-lite-image", 2048, 512, "1K", 1024, 1120),
    ("gemini-2.5-flash-image", 1000, 1048, "1K", 1024, 1290),
    ("gemini-3-pro-image", 2048, 2050, "2K", 2048, 1120),
    ("gemini-3.1-flash-image-preview", 4095, 4097, "4K", 4096, 2520),
])
def test_decoded_area_estimates_quantity_without_claiming_dimension_support(model, width, height, tier, edge, tokens):
    evidence = image_output_token_expectation(model, {}, [{"width": width, "height": height}], reference_source="google_ai_studio")
    assert evidence["tokens"] == tokens
    assert evidence["estimate_basis"] == "decoded_pixel_area"
    assert evidence["quantity_is_estimate"] is True
    assert evidence["dimension_contract_support"] == "not_evaluated"
    item = evidence["image_estimates"][0]
    assert item["actual_dimensions"] == {"width": width, "height": height}
    assert item["reference_dimensions"] == {"width": edge, "height": edge}
    assert item["nominal_resolution"] == tier
    assert item["relative_pixel_area_difference"] <= 0.10
    assert item["pixel_area_relative_tolerance"] == 0.10
    assert "reference_dimension_source" not in item


@pytest.mark.parametrize("model,width,height", [
    ("gemini-2.5-flash-image", 512, 512),
    ("gemini-2.5-flash-image", 2048, 2048),
    ("gemini-3.1-flash-lite-image", 784, 336),
    ("gemini-3.1-flash-lite-image", 2048, 2050),
    ("gemini-3-pro-image", 512, 512),
    ("gemini-3.1-flash-image", 800, 800),
    ("gemini-3.1-flash-image", 1024, 1200),
    ("gemini-3.1-flash-image", True, 1024),
    ("gemini-3.1-flash-image", 1024.0, 1024),
])
def test_decoded_area_does_not_grant_unsupported_or_out_of_tolerance_tiers(model, width, height):
    assert image_output_token_expectation(model, {}, [{"width": width, "height": height}]) is None


def test_exact_documented_table_rows_take_precedence_over_area_estimates():
    evidence = image_output_token_expectation("gemini-3.1-flash-image", {}, [{"width": 792, "height": 168}])
    assert evidence["tokens"] == 747
    assert evidence["estimate_basis"] == "official_resolution_table"
    assert evidence["image_estimates"][0]["reference_dimensions"] == {"width": 792, "height": 168}


def test_ambiguous_area_tiers_fail_closed_if_tolerance_is_broadened(monkeypatch):
    monkeypatch.setattr("lib.image_token_expectations.IMAGE_TOKEN_RELATIVE_TOLERANCE", 4.0)
    assert image_output_token_expectation("gemini-3.1-flash-image", {}, [{"width": 768, "height": 1024}]) is None


@pytest.mark.parametrize("model,model_path", [
    ("gemini-3.1-flash-image", "3-1-flash-image"),
    ("gemini-3.1-flash-lite-image", "3-1-flash-lite-image"),
    ("gemini-3-pro-image", "3-pro-image"),
])
def test_vertex_provenance_preserves_distinct_tier_and_pixel_table_sources(model, model_path):
    evidence = image_output_token_expectation(model, {}, [{"width": 1024, "height": 1024}], reference_source="google_vertex")
    assert evidence["source"] == GOOGLE_VERTEX_PRICING
    assert evidence["dimension_source"] == f"https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/{model_path}"
    assert evidence["image_estimates"][0]["reference_dimension_source"] == GOOGLE_IMAGE_GUIDE
    assert evidence["dimension_contract_support"] == "not_evaluated"


@pytest.mark.parametrize("model,width,height,reference_width,reference_height,expected", [
    ("gpt-image-2", 1312, 1199, 1312, 1200, [215, 1888, 7550]),
    ("gpt-image-2", 1254, 1254, 1248, 1248, [228, 2050, 8197]),
    ("gpt-image-2.5-sunburst", 1312, 1199, 1312, 1200, [215, 472, 1888, 3375, 7550]),
    ("gpt-image-2.5-flare", 1254, 1254, 1248, 1248, [228, 513, 2050, 3643, 8197]),
    # The ordinarily nearest 656x992 grid falls below the minimum pixel area.
    ("gpt-image-2", 657, 998, 656, 1008, [107, 990, 3960]),
])
def test_auto_size_uses_nearest_valid_grid_and_keeps_quality_options_discrete(model, width, height, reference_width, reference_height, expected):
    evidence = image_output_token_expectation(model, {"size": "auto", "quality": "auto"}, [{"width": width, "height": height}], reference_source="openai")
    assert [item["tokens"] for item in evidence["ranges"]] == expected
    assert evidence["estimate_basis"] == "decoded_auto_size_nearest_16px_grid"
    assert evidence["dimension_contract_support"] == "not_evaluated"
    assert evidence["quantity_is_estimate"] is True
    item = evidence["image_estimates"][0]
    assert item["actual_dimensions"] == {"width": width, "height": height}
    assert item["reference_dimensions"] == {"width": reference_width, "height": reference_height}
    assert gpt_image_2_output_tokens(width, height, "low") is None
    assert gpt_image_25_output_tokens(width, height, "low") is None


@pytest.mark.parametrize("model", ["gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"])
@pytest.mark.parametrize("size,width,height", [
    (None, 1254, 1254),
    ("1254x1254", 1254, 1254),
    ("1254x1254", 1248, 1248),
    ("1024x1024", 1254, 1254),
    ("auto", 511, 513),
    ("auto", 3855, 1024),
    ("auto", 3001, 999),
    ("auto", 3840, 2161),
    ("AUTO", 1254, 1254),
])
def test_auto_grid_estimate_requires_explicit_auto_and_otherwise_valid_geometry(model, size, width, height):
    body = {"quality": "low"}
    if size is not None:
        body["size"] = size
    assert image_output_token_expectation(model, body, [{"width": width, "height": height}]) is None


def test_auto_grid_does_not_extend_legacy_models_or_unknown_sources_or_quality():
    body = {"size": "auto", "quality": "low"}
    images = [{"width": 1254, "height": 1254}]
    assert image_output_token_expectation("gpt-image-1", body, images) is None
    assert image_output_token_expectation("gpt-image-2", body, images, reference_source="google_vertex") is None
    assert image_output_token_expectation("gpt-image-2", {**body, "quality": "max"}, images) is None
    assert image_output_token_expectation("gpt-image-2.5-flare", {**body, "quality": "unknown"}, images) is None


def test_mixed_exact_and_estimated_images_keep_actual_count_and_provenance():
    images = [{"width": 1024, "height": 1024}, {"width": 1254, "height": 1254}]
    evidence = image_output_token_expectation("gpt-image-2", {"size": "auto", "quality": "low", "n": 2}, images)
    assert evidence["tokens"] == 424
    assert evidence["estimate_basis"] == "mixed"
    assert [item["basis"] for item in evidence["image_estimates"]] == ["official_calculator", "decoded_auto_size_nearest_16px_grid"]
    assert image_output_token_expectation("gpt-image-2", {"size": "auto", "quality": "low", "n": 1}, images) is None
