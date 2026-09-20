from __future__ import annotations

import io
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib.config import get_image_model_config, load_config
from banana_gc_fixtures import historical_image_capability
from lib.banana_generate_content import build_banana_generate_content_cases, contract_id_for_model
from lib.image_reference_candidate import separate_image_observations
from lib.reference_specs import load_model_capability_profile
from lib.job_spec import resolve_image_plan
from lib.image_validation import (
    ImageInfo,
    gemini_flash_31_lite_image_profile_cases,
)
from scripts.image_param_test import (
    _request_body,
    _response_image_items,
    _with_output_options,
    main,
    models_endpoint,
    normalize_image_endpoint,
    run_case,
    validate_configured_image_endpoint,
)
from scripts.web_console import _image_command_for_job


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post(
        self,
        endpoint: str,
        *,
        json: dict,
        headers: dict[str, str],
        timeout: int,
        allow_redirects: bool,
    ) -> FakeResponse:
        self.calls.append((endpoint, json))
        return self.response


def _case(*, suite: str = "smoke", name_suffix: str | None = None):
    cases = gemini_flash_31_lite_image_profile_cases(
        suite,
        api_form="gemini_generate_content",
        capability_profile=historical_image_capability("gemini-3.1-flash-lite-image"),
    )
    selected = (
        next(case for case in cases if case.name.endswith(str(name_suffix)))
        if name_suffix
        else cases[0]
    )
    return _with_output_options(
        selected,
        "low",
        "png",
        transport="gemini-generate-content",
    )


def _image_info() -> ImageInfo:
    return ImageInfo(
        format="PNG",
        width=1024,
        height=1024,
        byte_length=1024,
        sha256="a" * 64,
        visual_metrics={"available": False},
    )


def _success_payload(*, include_usage: bool = True, finish_reason: str = "STOP") -> dict:
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": "aW1hZ2U=",
                            }
                        }
                    ],
                },
                "finishReason": finish_reason,
            }
        ],
        "modelVersion": "models/gemini-3.1-flash-lite-image",
    }
    if include_usage:
        payload["usageMetadata"] = {
            "promptTokenCount": 3,
            "candidatesTokenCount": 1120,
            "totalTokenCount": 1123,
        }
    return payload


def _promoted_smoke_fixture() -> tuple[dict, dict]:
    """Model a published beta cell in memory, keeping real snapshot validation."""
    model = "gemini-3.1-flash-lite-image"
    capability = copy.deepcopy(load_model_capability_profile(
        "image", "banana", model, route_profile="google_ai_studio",
        api_form="gemini_generate_content",
    ))
    case = build_banana_generate_content_cases(model, diagnostic=True)[0]
    expectations = {
        case.name: {"expectation": "supported", "expected_size": [1024, 1024],
                    "documentation_match": True, "evidence_refs": ["unit-test-beta-evidence"]}
    }
    capability.update({"parameter_test_enabled": True,
                       "test_policy_parameter_test_enabled": True,
                       "image_case_expectations": expectations})
    snapshot = copy.deepcopy(capability["model_profile_database"])
    policy = snapshot["test_binding"]
    policy.update({"parameter_test_enabled": True, "image_case_expectations": expectations})
    for row in snapshot["interface"].get("test_bindings") or []:
        if row.get("test_binding_id") == policy["test_binding_id"]:
            row.update(copy.deepcopy(policy))
    parameter = snapshot["parameter_test_binding"]
    parameter.update({"parameter_test_enabled": True, "runner_enabled": True,
                      "enabled": True, "executable": True})
    snapshot.pop("snapshot_digest", None)
    snapshot["snapshot_digest"] = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    capability["model_profile_database"] = snapshot
    return capability, {"test_binding": policy, "parameter_test_binding": parameter,
                        "model_profile_database": snapshot}


class GeminiGenerateContentImageTest(unittest.TestCase):
    def test_app_config_exposes_four_exact_gc_routes_and_enforces_each_policy(self) -> None:
        config = load_config()
        lite = get_image_model_config(
            config,
            "gemini",
            "gemini-3.1-flash-lite-image",
            route_profile="google_ai_studio",
            api_form="gemini_generate_content",
        )
        self.assertEqual(lite["transport"], "gemini-generate-content")
        self.assertEqual(lite["api_form"], "gemini_generate_content")
        lite_model = next(
            model
            for model in config["providers"]["gemini"]["image"]["models"]
            if model["id"] == "gemini-3.1-flash-lite-image"
        )
        self.assertEqual(
            set(lite_model["routes"]["google_ai_studio"]["api_forms"]),
            {"gemini_interactions", "gemini_generate_content"},
        )
        self.assertEqual(
            lite_model["default_api_forms"]["google_ai_studio"],
            "gemini_generate_content",
        )
        with self.assertRaisesRegex(
            ValueError,
            "Official MPDB binding does not match|No approved official MPDB binding",
        ):
            resolve_image_plan(
                config,
                {
                    "image_plan": {
                        "route_profile": "google_ai_studio",
                        "api_form": "gemini_interactions",
                        "suite": "smoke",
                        "no_cross_control": True,
                    }
                },
                "gemini",
                "gemini-3.1-flash-lite-image",
                60,
            )
        for model in (
            "gemini-3.1-flash-lite-image",
            "gemini-3.1-flash-image",
            "gemini-3-pro-image",
            "gemini-2.5-flash-image",
        ):
            with self.subTest(model=model):
                selected = get_image_model_config(
                    config,
                    "gemini",
                    model,
                    route_profile="google_ai_studio",
                    api_form="gemini_generate_content",
                )
                self.assertEqual(selected["transport"], "gemini-generate-content")
                capability = load_model_capability_profile(
                    "image", "banana", model, route_profile="google_ai_studio",
                    api_form="gemini_generate_content",
                )
                self.assertEqual(capability["default_reference_source"], contract_id_for_model(model))
                request = {"image_plan": {"route_profile": "google_ai_studio",
                                           "api_form": "gemini_generate_content",
                                           "suite": "smoke", "no_cross_control": True}}
                arguments = [
                    "--provider", "gemini", "--base-url", "https://generativelanguage.googleapis.com",
                    "--family", "banana", "--model", model, "--transport", "gemini-generate-content",
                    "--api-form", "gemini_generate_content", "--route-profile", "google_ai_studio",
                    "--api-version", "v1beta", "--suite", "smoke", "--no-cross-control", "--dry-run",
                ]
                if capability["parameter_test_enabled"] is True:
                    plan = resolve_image_plan(config, request, "gemini", model, 60)
                    self.assertEqual(plan["api_version"], "v1beta")
                    self.assertEqual(plan["estimated_case_count"], 1)
                    self.assertTrue(plan["cases"][0].startswith(contract_id_for_model(model)))
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main(arguments), 0)
                    self.assertEqual(json.loads(output.getvalue())["cases"][0]["expected_outcome"], "success")
                else:
                    with self.assertRaisesRegex(ValueError, "disabled"):
                        resolve_image_plan(config, request, "gemini", model, 60)
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                        main(arguments)
                    self.assertEqual(failure.exception.code, 2)

    def test_mpdb_driven_dry_run_has_exact_binding_and_native_body(self) -> None:
        capability, parameter_config = _promoted_smoke_fixture()
        output = io.StringIO()
        with redirect_stdout(output), patch(
            "scripts.image_param_test.load_model_capability_profile", return_value=capability
        ), patch(
            "scripts.image_param_test.resolve_runtime_parameter_config", return_value=parameter_config
        ):
            status = main(
                [
                    "--provider",
                    "gemini",
                    "--base-url",
                    "https://generativelanguage.googleapis.com",
                    "--family",
                    "banana",
                    "--model",
                    "gemini-3.1-flash-lite-image",
                    "--transport",
                    "gemini-generate-content",
                    "--api-form",
                    "gemini_generate_content",
                    "--api-version",
                    "v1beta",
                    "--route-profile",
                    "google_ai_studio",
                    "--suite",
                    "smoke",
                    "--dry-run",
                ]
            )
        plan = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(plan["api_form"], "gemini_generate_content")
        self.assertEqual(plan["api_version"], "v1beta")
        self.assertEqual(
            plan["endpoint"],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite-image:generateContent",
        )
        binding = plan["model_profile_database"]
        self.assertTrue(binding["catalog_resolved"])
        self.assertEqual(
            binding["interface_id"],
            "image/google_ai_studio/banana/gemini-3.1-flash-lite-image"
            "#gemini-generate-content-default",
        )
        self.assertEqual(
            binding["parameter_test_binding_id"],
            "parameter/gemini_3_1_flash_lite_image_generate_content_v1beta",
        )
        self.assertEqual(
            plan["cases"][0]["metadata"]["test_profile"],
            "gemini_3_1_flash_lite_image_generate_content_v1beta_documented_matrix",
        )
        self.assertEqual(plan["cases"][0]["expected_size"], [1024, 1024])
        self.assertEqual(plan["cases"][0]["expected_outcome"], "success")
        self.assertFalse(plan["cases"][0]["metadata"]["banana_gc_candidate"])

    def test_web_plan_and_command_preserve_native_version_and_profiles(self) -> None:
        capability, _ = _promoted_smoke_fixture()
        with patch("lib.job_spec.load_model_capability_profile", return_value=capability), patch(
            "lib.job_spec.capability_profile_snapshot", return_value=capability
        ):
            plan = resolve_image_plan(
                load_config(),
                {
                    "image_plan": {
                        "route_profile": "google_ai_studio",
                        "api_form": "gemini_generate_content",
                        "suite": "smoke",
                        "no_cross_control": True,
                    }
                },
                "gemini",
                "gemini-3.1-flash-lite-image",
                60,
            )
        self.assertEqual(plan["transport"], "gemini-generate-content")
        self.assertEqual(plan["api_form"], "gemini_generate_content")
        self.assertEqual(plan["api_version"], "v1beta")
        self.assertEqual(plan["auth_mode"], "google_api_key")
        self.assertEqual(
            plan["endpoint"],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite-image:generateContent",
        )
        self.assertEqual(
            plan["test_profiles"],
            ["gemini_3_1_flash_lite_image_generate_content_v1beta_documented_matrix"],
        )
        command = _image_command_for_job(Path("/tmp/gemini-image-report"), plan)
        version_index = command.index("--api-version")
        self.assertEqual(command[version_index + 1], "v1beta")
        self.assertIn("gemini-generate-content", command)

    def test_endpoint_body_and_inline_data_follow_generate_content_contract(self) -> None:
        endpoint = normalize_image_endpoint(
            "https://generativelanguage.googleapis.com/v1beta/models/stale:generateContent",
            "gemini-generate-content",
            api_version="v1beta",
            model="models/gemini-3.1-flash-lite-image",
        )
        self.assertEqual(
            endpoint,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite-image:generateContent",
        )
        self.assertEqual(
            models_endpoint(endpoint, "gemini-generate-content"),
            "https://generativelanguage.googleapis.com/v1beta/models",
        )
        case = _case()
        body = _request_body(
            case,
            "gemini-3.1-flash-lite-image",
            "test prompt",
            "gemini-generate-content",
        )
        self.assertEqual(
            body,
            {
                "contents": [
                    {"role": "user", "parts": [{"text": "test prompt"}]}
                ],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
                },
            },
        )
        payload = _success_payload()
        payload["candidates"][0]["content"]["parts"].append(
            payload["candidates"][0]["content"]["parts"][0]
        )
        self.assertEqual(
            _response_image_items(payload, "gemini-generate-content"),
            [
                {
                    "b64_json": "aW1hZ2U=",
                    "mime_type": "image/png",
                }
            ],
        )

    def test_historical_v1_factory_preserves_21_cases_and_thinking_requests(self) -> None:
        with self.assertRaisesRegex(ValueError, "not executable|does not match"):
            gemini_flash_31_lite_image_profile_cases(
                "full",
                api_form="gemini_interactions",
                include_4k=True,
            )
        cases = gemini_flash_31_lite_image_profile_cases(
            "full",
            api_form="gemini_generate_content",
            include_4k=True,
            capability_profile=historical_image_capability("gemini-3.1-flash-lite-image"),
        )
        self.assertEqual(len(cases), 21)
        self.assertEqual(
            len({case.metadata["test_profile"] for case in cases}),
            8,
        )
        thinking = {
            case.metadata.get("thinking_level"): _request_body(
                _with_output_options(
                    case,
                    "low",
                    "png",
                    transport="gemini-generate-content",
                ),
                "gemini-3.1-flash-lite-image",
                "test prompt",
                "gemini-generate-content",
            )
            for case in cases
            if case.metadata.get("thinking_level")
        }
        self.assertEqual(set(thinking), {"MINIMAL", "HIGH"})
        for level, body in thinking.items():
            self.assertEqual(
                body["generationConfig"]["thinkingConfig"],
                {"thinkingLevel": level},
            )

    def test_success_uses_usage_metadata_for_schema4_token_validation(self) -> None:
        session = FakeSession(FakeResponse(200, _success_payload()))
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "scripts.image_param_test._image_bytes",
            return_value=(b"image", "b64_json"),
        ), patch(
            "scripts.image_param_test.inspect_image_bytes",
            return_value=_image_info(),
        ):
            result = run_case(
                session,  # type: ignore[arg-type]
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.1-flash-lite-image:generateContent",
                "gemini-3.1-flash-lite-image",
                "test prompt",
                _case(),
                timeout=30,
                images_dir=Path(tmpdir),
                visual_forensics=False,
                transport="gemini-generate-content",
                auth_mode="google_api_key",
                api_version="v1beta",
            )
        self.assertTrue(result["compatibility_pass"])
        self.assertTrue(result["token_validation_pass"])
        self.assertTrue(result["overall_pass"])
        exchange = result["token_audit"]["exchanges"][0]
        self.assertEqual(exchange["schema_version"], 4)
        accounting = exchange["usage_accounting"]
        self.assertEqual(accounting["input_tokens"], 3)
        self.assertEqual(accounting["output_tokens"], 1120)
        self.assertEqual(accounting["total_tokens"], 1123)
        self.assertEqual(result["model_identity_audit"]["status"], "match")
        self.assertEqual(
            session.calls[0][0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite-image:generateContent",
        )

    def test_missing_usage_or_non_stop_finish_reason_fails_closed(self) -> None:
        for payload, expected_failure in (
            (_success_payload(include_usage=False), "usage_missing"),
            (
                _success_payload(finish_reason="MAX_TOKENS"),
                "generate_content_finish_reason_not_stop",
            ),
        ):
            with self.subTest(expected_failure=expected_failure):
                with tempfile.TemporaryDirectory() as tmpdir, patch(
                    "scripts.image_param_test._image_bytes",
                    return_value=(b"image", "b64_json"),
                ), patch(
                    "scripts.image_param_test.inspect_image_bytes",
                    return_value=_image_info(),
                ):
                    result = run_case(
                        FakeSession(FakeResponse(200, payload)),  # type: ignore[arg-type]
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-3.1-flash-lite-image:generateContent",
                        "gemini-3.1-flash-lite-image",
                        "test prompt",
                        _case(),
                        timeout=30,
                        images_dir=Path(tmpdir),
                        visual_forensics=False,
                        transport="gemini-generate-content",
                        api_version="v1beta",
                    )
                self.assertFalse(result["overall_pass"])
                if expected_failure == "usage_missing":
                    self.assertTrue(result["compatibility_pass"])
                    self.assertFalse(result["token_validation_pass"])
                    self.assertTrue(
                        any(
                            "usage" in failure
                            for failure in result["token_validation_failures"]
                        )
                    )
                else:
                    self.assertFalse(result["compatibility_pass"])
                    self.assertIn(expected_failure, result["failures"])

    def test_inconsistent_usage_metadata_fails_token_gate_only(self) -> None:
        payload = _success_payload()
        payload["usageMetadata"]["totalTokenCount"] = 9999
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "scripts.image_param_test._image_bytes",
            return_value=(b"image", "b64_json"),
        ), patch(
            "scripts.image_param_test.inspect_image_bytes",
            return_value=_image_info(),
        ):
            result = run_case(
                FakeSession(FakeResponse(200, payload)),  # type: ignore[arg-type]
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.1-flash-lite-image:generateContent",
                "gemini-3.1-flash-lite-image",
                "test prompt",
                _case(),
                timeout=30,
                images_dir=Path(tmpdir),
                visual_forensics=False,
                transport="gemini-generate-content",
                api_version="v1beta",
            )
        self.assertTrue(result["compatibility_pass"])
        self.assertFalse(result["token_validation_pass"])
        self.assertFalse(result["overall_pass"])
        self.assertEqual(
            result["token_audit"]["exchanges"][0]["usage_arithmetic"]["status"],
            "fail",
        )

    def test_expected_rejection_must_identify_the_probed_parameter(self) -> None:
        case = _case(suite="resolution", name_suffix="reject_2k")
        for message, expected_pass in (
            ("invalid imageSize value", True),
            ("request rejected", False),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmpdir:
                result = run_case(
                    FakeSession(FakeResponse(400, {"error": {"message": message}})),  # type: ignore[arg-type]
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-3.1-flash-lite-image:generateContent",
                    "gemini-3.1-flash-lite-image",
                    "test prompt",
                    case,
                    timeout=30,
                    images_dir=Path(tmpdir),
                    visual_forensics=False,
                    transport="gemini-generate-content",
                    api_version="v1beta",
                )
            self.assertEqual(result["compatibility_pass"], expected_pass)
            self.assertEqual(result["overall_pass"], expected_pass)
            if not expected_pass:
                self.assertIn("parameter_rejection_not_attributed", result["failures"])

    def test_embedded_diagnostic_case_never_enters_app_certification_counts(self) -> None:
        candidate = build_banana_generate_content_cases(
            "gemini-3.1-flash-lite-image", diagnostic=True
        )[0]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "scripts.image_param_test._image_bytes", return_value=(b"image", "b64_json")
        ), patch("scripts.image_param_test.inspect_image_bytes", return_value=_image_info()):
            result = run_case(
                FakeSession(FakeResponse(200, _success_payload())),
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-image:generateContent",
                "gemini-3.1-flash-lite-image", "test prompt", candidate,
                timeout=30, images_dir=Path(tmpdir), visual_forensics=False,
                transport="gemini-generate-content", auth_mode="google_api_key", api_version="v1beta",
            )
        self.assertEqual(result["status"], "observed_acceptance")
        self.assertTrue(result["diagnostic_pass"])
        self.assertFalse(result["overall_pass"])
        self.assertFalse(result["compatibility_pass"])
        self.assertFalse(result["certified_route_contract_pass"])
        summary = {"pass": True, "pass_count": 1}
        separate_image_observations(summary, [result])
        self.assertEqual(summary["pass_count"], 0)
        self.assertEqual(summary["observed_case_count"], 1)
        self.assertEqual(summary["contractual_case_count"], 0)
        self.assertFalse(summary["pass"])
        self.assertFalse(summary["certified_route_contract_pass"])

    def test_official_endpoint_validation_rejects_host_substitution(self) -> None:
        config = load_config()
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite-image:generateContent"
        )
        validate_configured_image_endpoint(
            config,
            provider="gemini",
            endpoint=endpoint,
            transport="gemini-generate-content",
            model="gemini-3.1-flash-lite-image",
            api_version="v1beta",
        )
        with self.assertRaisesRegex(ValueError, "does not match|non-official"):
            validate_configured_image_endpoint(
                config,
                provider="gemini",
                endpoint=endpoint.replace(
                    "generativelanguage.googleapis.com", "attacker.example"
                ),
                transport="gemini-generate-content",
                model="gemini-3.1-flash-lite-image",
                api_version="v1beta",
            )


if __name__ == "__main__":
    unittest.main()
