from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib.banana_generate_content import (
    build_banana_generate_content_cases, contract_id_for_model, has_exact_banana_gc_reference,
)
from lib.image_validation import ImageInfo
from scripts import image_param_test


def _capability():
    model = "gemini-3.1-flash-lite-image"
    case = build_banana_generate_content_cases(model, diagnostic=True)[0]
    return model, {
        "source_id": "google_ai_studio", "route_profile": "dynamic_aggregator",
        "api_form": "gemini_generate_content", "default_reference_source": contract_id_for_model(model),
        "parameter_test_enabled": True, "test_policy_parameter_test_enabled": True,
        "image_case_expectations": {case.name: {"expectation": "supported", "expected_size": [1024, 1024],
                                               "documentation_match": True, "evidence_refs": ["official-beta"]}},
    }


def test_exact_factory_binds_source_contract_not_runtime_route_or_url_version():
    model, capability = _capability()
    assert has_exact_banana_gc_reference(model, capability)
    case = build_banana_generate_content_cases(model, capability_profile=capability)[0]
    assert case.metadata["reference_api_version"] == "v1beta"
    assert "api_version" not in case.metadata
    capability["source_id"] = "google_vertex"
    assert not has_exact_banana_gc_reference(model, capability)
    with pytest.raises(ValueError, match="contract mismatch"):
        build_banana_generate_content_cases(model, capability_profile=capability)


def test_gateway_success_compares_to_beta_without_certifying_google(monkeypatch, tmp_path):
    model, capability = _capability()
    case = build_banana_generate_content_cases(model, capability_profile=capability)[0]
    payload = {
        "modelVersion": model,
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}}
        ]}}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1120, "totalTokenCount": 1123},
    }
    response = SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                               text=json.dumps(payload), json=lambda: payload)
    calls = []
    session = SimpleNamespace(post=lambda url, **kwargs: (calls.append((url, kwargs)) or response))
    monkeypatch.setattr(image_param_test, "_image_bytes", lambda *a: (b"image", "b64_json"))
    monkeypatch.setattr(image_param_test, "inspect_image_bytes", lambda *a, **k: ImageInfo(
        format="PNG", width=1024, height=1024, byte_length=5, sha256="a" * 64,
    ))
    endpoint = f"https://gateway.example/v1/models/{model}:generateContent"
    result = image_param_test.run_case(
        session, endpoint, model, "draw", case, timeout=3, images_dir=tmp_path,
        visual_forensics=False, transport="gemini-generate-content", api_version="v1",
    )
    assert calls[0][0] == endpoint
    assert "generationConfig" in calls[0][1]["json"]
    assert "response_format" not in calls[0][1]["json"]
    assert result["api_version"] == "v1"
    assert result["reference_api_version"] == "v1beta"
    assert result["compatibility_pass"] is True
    assert result["comparison_scope"] == "adapter_only"
    assert result["certification_scope"] == "adapter_only"
    assert result["certified_route_contract_pass"] is False
