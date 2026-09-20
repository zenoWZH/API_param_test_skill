from __future__ import annotations

import pytest

from lib.banana_generate_content import (
    BANANA_GC_MODELS, build_banana_generate_content_cases, contract_id_for_model,
)
from lib.image_validation import ImageInfo, ImageTestCase, evaluate_case
from lib.job_spec import _versioned_gemini_image_endpoint


def _cases(model: str, **kwargs):
    return build_banana_generate_content_cases(model, "resolution", diagnostic=True, include_4k=True, **kwargs)


def test_cartesian_main_and_boundary_counts_and_omission():
    totals = {
        "gemini-2.5-flash-image": (10, 10),
        "gemini-3.1-flash-lite-image": (14, 5),
        "gemini-3.1-flash-image": (56, 2),
        "gemini-3-pro-image": (30, 15),
    }
    for model, (main, boundary) in totals.items():
        cases = _cases(model)
        assert len(cases) == main + boundary
        assert len({case.name for case in cases}) == len(cases)
        assert cases[0] == build_banana_generate_content_cases(model, diagnostic=True)[0]
        assert sum(case.metadata["matrix_group"] == "documented" for case in cases) == main
        assert all(case.expected_outcome == "observation" for case in cases)
        documented = _cases(model, include_negative=False)
        assert len(documented) == main
        if model == "gemini-2.5-flash-image":
            assert all("imageSize" not in case.parameters["generationConfig"]["imageConfig"] for case in documented)
            explicit_1k = next(case for case in cases if case.name.endswith("boundary_image_size_1k"))
            assert explicit_1k.parameters["generationConfig"]["imageConfig"]["imageSize"] == "1K"
    assert sum(sum(pair) for pair in totals.values()) == 142


def test_pro_extremes_cover_every_tier_and_flash_conflict_has_no_guessed_size():
    pro = [case for case in _cases("gemini-3-pro-image") if case.metadata["matrix_group"] == "boundary" and case.metadata["probe_field"] == "aspect_ratio" and case.metadata["aspect_ratio"] != "7:5"]
    assert len(pro) == 12
    assert {case.metadata["requested_resolution"] for case in pro} == {"1K", "2K", "4K"}
    flash = _cases("gemini-3.1-flash-image")
    conflict = next(case for case in flash if case.metadata["aspect_ratio"] == "21:9" and case.metadata["requested_resolution"] == "512")
    assert conflict.expected_size is None
    assert conflict.metadata["dimension_source"] == "document_conflict"
    assert conflict.metadata["documented_dimensions_conflict"] == [792, 168]
    square_4k = next(case for case in flash if case.metadata["aspect_ratio"] == "1:1" and case.metadata["requested_resolution"] == "4K")
    assert square_4k.expected_size == (4096, 4096)


def test_exact_dimensions_stay_model_scoped_despite_nominal_ratio_rounding():
    flash25 = next(case for case in _cases("gemini-2.5-flash-image") if case.metadata["matrix_group"] == "documented" and case.metadata["aspect_ratio"] == "3:4")
    flash31 = next(case for case in _cases("gemini-3.1-flash-image") if case.metadata["requested_resolution"] == "1K" and case.metadata["aspect_ratio"] == "3:4")
    assert flash25.expected_size == (864, 1184)
    assert flash31.expected_size == (896, 1200)
    result = evaluate_case(flash25, status_code=200, images=[_image(864, 1184)])
    assert result["dimension_validation_pass"] is True
    wrong_model_table = evaluate_case(flash25, status_code=200, images=[_image(896, 1200)])
    assert wrong_model_table["dimension_validation_pass"] is False


def test_document_conflict_records_nominal_ratio_and_requires_three_live_facts():
    model = "gemini-3.1-flash-image"
    conflict = next(case for case in _cases(model) if case.metadata["dimension_source"] == "document_conflict")
    correct_ratio = evaluate_case(conflict, status_code=200, images=[_image(784, 336)])
    assert correct_ratio["aspect_ratio_validation_pass"] is True
    assert correct_ratio["dimension_validation_pass"] is None
    assert correct_ratio["certified"] is False
    printed_doc_row = evaluate_case(conflict, status_code=200, images=[_image(792, 168)])
    assert printed_doc_row["aspect_ratio_validation_pass"] is False
    evidence = {"expectation": "supported", "expected_size": [784, 336],
                "documentation_match": False, "evidence_refs": ["fact1", "fact2"]}
    capability = {
        "parameter_test_enabled": True, "test_policy_parameter_test_enabled": True,
        "default_reference_source": contract_id_for_model(model),
        "api_form": "gemini_generate_content", "route_profile": "google_ai_studio", "source_id": "google_ai_studio",
        "image_case_expectations": {conflict.name: evidence},
    }
    with pytest.raises(ValueError, match="three distinct"):
        build_banana_generate_content_cases(model, "resolution", capability_profile=capability)
    evidence["evidence_refs"].append("fact3")
    promoted = next(case for case in build_banana_generate_content_cases(model, "resolution", capability_profile=capability) if case.name == conflict.name)
    assert promoted.expected_outcome == "success"
    assert promoted.expected_size == (784, 336)
    assert promoted.metadata["documented_expected_size"] == [792, 168]
    assert evaluate_case(promoted, status_code=200, images=[_image(784, 336)])["pass"] is True


def test_billing_gate_exact_identity_and_no_implicit_certification():
    for model in BANANA_GC_MODELS:
        with pytest.raises(ValueError, match="include_4k"):
            build_banana_generate_content_cases(model, "full", diagnostic=True)
        without_4k = build_banana_generate_content_cases(model, "resolution", diagnostic=True)
        assert all(case.metadata["requested_resolution"] != "4K" for case in without_4k)
        with pytest.raises(ValueError, match="not certified"):
            build_banana_generate_content_cases(model)
        with pytest.raises(ValueError, match="mismatch"):
            build_banana_generate_content_cases(model, capability_profile={"parameter_test_enabled": True, "test_policy_parameter_test_enabled": True})
    with pytest.raises(ValueError, match="No exact"):
        build_banana_generate_content_cases("gemini-3-pro-image-preview", diagnostic=True)


def test_enabled_exact_contract_still_cannot_certify_unverified_boundaries():
    model = "gemini-3-pro-image"
    capability = {
        "parameter_test_enabled": True, "test_policy_parameter_test_enabled": True,
        "default_reference_source": contract_id_for_model(model),
        "api_form": "gemini_generate_content", "route_profile": "google_ai_studio", "source_id": "google_ai_studio",
    }
    cases = build_banana_generate_content_cases(model, "resolution", capability_profile=capability)
    assert all(case.expected_outcome == "observation" for case in cases)
    capability["image_case_expectations"] = {
        cases[0].name: {
            "expectation": "supported", "expected_size": [1024, 1024],
            "documentation_match": True, "evidence_refs": ["official-beta-evidence"],
        }
    }
    cases = build_banana_generate_content_cases(model, "resolution", capability_profile=capability)
    assert cases[0].expected_outcome == "success"
    assert evaluate_case(cases[0], status_code=200, images=[_image()])["pass"] is True
    boundary = next(case for case in cases if case.name.endswith("boundary_image_size_512"))
    result = evaluate_case(boundary, status_code=400, error={"message": "imageSize 512 unsupported"})
    assert result["status"] == "observed_parameter_rejection"
    assert result["compatibility_pass"] is False
    capability["image_case_expectations"][boundary.name] = {
        "expectation": "unsupported", "documentation_match": False,
        "evidence_refs": ["official-beta-rejection-evidence"],
    }
    promoted = next(case for case in build_banana_generate_content_cases(model, "resolution", capability_profile=capability) if case.name == boundary.name)
    assert promoted.expected_outcome == "rejection"
    assert evaluate_case(promoted, status_code=400, error={"message": "imageSize 512 unsupported"})["pass"] is True
    assert evaluate_case(promoted, status_code=400, error={"message": "Invalid API version"})["pass"] is False
    capability["default_reference_source"] = contract_id_for_model("gemini-3.1-flash-image")
    with pytest.raises(ValueError, match="mismatch"):
        build_banana_generate_content_cases(model, capability_profile=capability)


def test_stable_live_dimension_override_keeps_documented_pixels_and_diagnostic_hash():
    model = "gemini-3.1-flash-lite-image"
    original = next(case for case in _cases(model) if case.metadata["aspect_ratio"] == "1:8")
    capability = {
        "parameter_test_enabled": True, "test_policy_parameter_test_enabled": True,
        "default_reference_source": contract_id_for_model(model),
        "api_form": "gemini_generate_content", "route_profile": "google_ai_studio", "source_id": "google_ai_studio",
        "image_case_expectations": {
            original.name: {"expectation": "supported", "expected_size": [352, 2928],
                            "documentation_match": False, "evidence_refs": ["repeat1", "repeat2", "repeat3"]}
        },
    }
    promoted = next(case for case in build_banana_generate_content_cases(model, "resolution", capability_profile=capability) if case.name == original.name)
    assert promoted.expected_size == (352, 2928)
    assert promoted.metadata["documented_expected_size"] == [384, 3072]
    assert promoted.metadata["dimension_source"] == "official_live_documentation_deviation"
    unchanged = next(case for case in _cases(model, capability_profile=capability) if case.name == original.name)
    assert unchanged.public() == original.public()
    capability["image_case_expectations"][original.name]["documentation_match"] = True
    with pytest.raises(ValueError, match="documentation_match"):
        build_banana_generate_content_cases(model, "resolution", capability_profile=capability)


def _image(width=1024, height=1024):
    return ImageInfo(format="PNG", width=width, height=height, byte_length=20, sha256="test", has_alpha=False)


def test_candidate_acceptance_retains_geometry_without_certifying():
    case = build_banana_generate_content_cases("gemini-3.1-flash-image", diagnostic=True)[0]
    for image, geometry in [(_image(), True), (_image(300, 100), False)]:
        result = evaluate_case(case, status_code=200, images=[image])
        assert result["status"] == "observed_acceptance"
        assert result["dimension_validation_pass"] is geometry
        assert result["pass"] is result["compatibility_pass"] is result["certified"] is False
    malformed = evaluate_case(case, status_code=200, error="image_decode_failed")
    assert malformed["status"] == "unresolved"
    assert malformed["diagnostic_pass"] is False


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, None])
def test_infrastructure_failures_never_prove_parameter_rejection(status):
    case = next(case for case in _cases("gemini-3-pro-image") if case.name.endswith("boundary_image_size_512"))
    result = evaluate_case(case, status_code=status, error={"message": "imageSize not supported"})
    assert result["status"] == "unresolved"
    assert result["diagnostic_pass"] is False


def test_candidate_rejection_requires_probed_field_and_keeps_legacy_observation():
    case = next(case for case in _cases("gemini-3-pro-image") if case.name.endswith("boundary_image_size_512"))
    unrelated = evaluate_case(case, status_code=400, error={"message": "Invalid API version"})
    assert unrelated["status"] == "unresolved"
    attributed = evaluate_case(case, status_code=400, error={"message": "imageSize 512 is not supported"})
    assert attributed["status"] == "observed_parameter_rejection"
    assert attributed["diagnostic_pass"] is True
    assert attributed["pass"] is False
    documented = next(case for case in _cases("gemini-3-pro-image") if case.metadata["matrix_group"] == "documented" and case.metadata["aspect_ratio"] == "2:3")
    assert evaluate_case(documented, status_code=400, error={"message": "aspectRatio unsupported"})["status"] == "observed_parameter_rejection"
    baseline_25 = build_banana_generate_content_cases("gemini-2.5-flash-image", diagnostic=True)[0]
    assert baseline_25.metadata["probe_field"] is None
    assert evaluate_case(baseline_25, status_code=400, error={"message": "imageSize unsupported"})["status"] == "unresolved"
    legacy = ImageTestCase(name="cross_control", parameters={}, expected_outcome="observation")
    assert evaluate_case(legacy, status_code=400)["status"] == "observed"


def test_job_endpoint_refuses_historical_official_v1_but_keeps_gateway():
    arguments = {"transport": "gemini-generate-content", "model": "gemini-3-pro-image", "api_version": "v1beta"}
    with pytest.raises(ValueError, match="v1beta"):
        _versioned_gemini_image_endpoint("https://generativelanguage.googleapis.com/v1/models/old:generateContent", **arguments)
    assert _versioned_gemini_image_endpoint("https://generativelanguage.googleapis.com/v1beta/models/old:generateContent", **arguments).endswith("/v1beta/models/gemini-3-pro-image:generateContent")
    assert _versioned_gemini_image_endpoint("https://gateway.example/v1/models/old:generateContent", **arguments).endswith("/v1beta/models/gemini-3-pro-image:generateContent")
