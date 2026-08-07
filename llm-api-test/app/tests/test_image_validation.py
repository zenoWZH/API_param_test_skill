from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from lib.image_validation import (
    ImageInfo,
    banana_variant_cases,
    evaluate_case,
    gpt_image_2_cases,
    grok_imagine_cases,
    infer_postprocess_suspicion,
    infer_resolution_correspondence,
    inspect_image_bytes,
    validate_gpt_image_2_size,
)
from lib.credential_security import ProviderCredential
from scripts.image_param_test import (
    _image_bytes,
    _request_body,
    _response_image_items,
    _with_output_options,
    models_endpoint,
    normalize_image_endpoint,
    normalize_image_generation_endpoint,
    run_case,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, int]] = []
        self.request_headers: list[dict[str, str]] = []

    def post(
        self,
        endpoint: str,
        *,
        json: dict,
        headers: dict[str, str],
        timeout: int,
        allow_redirects: bool,
    ) -> FakeResponse:
        self.asserted_no_redirects = not allow_redirects
        self.calls.append((endpoint, json, timeout))
        self.request_headers.append(headers)
        return self.response


class ImageValidationTest(unittest.TestCase):
    def test_gpt_image_2_size_constraints(self) -> None:
        self.assertEqual(validate_gpt_image_2_size("1024x1024"), [])
        self.assertEqual(validate_gpt_image_2_size("1536x864"), [])
        self.assertEqual(validate_gpt_image_2_size("2048x2048"), [])
        self.assertEqual(validate_gpt_image_2_size("3840x2160"), [])

        self.assertIn("divisible by 16", " ".join(validate_gpt_image_2_size("1537x864")))
        self.assertIn("between 1:3 and 3:1", " ".join(validate_gpt_image_2_size("3072x768")))
        self.assertIn("at least 655360", " ".join(validate_gpt_image_2_size("512x512")))
        self.assertIn("not exceed 3840", " ".join(validate_gpt_image_2_size("4096x1920")))

    def test_case_matrix_requires_explicit_4k_acknowledgement(self) -> None:
        smoke = gpt_image_2_cases("smoke")
        self.assertEqual([case.name for case in smoke], ["baseline_1024_square"])

        resolution = gpt_image_2_cases("resolution")
        names = {case.name for case in resolution}
        self.assertIn("square_2k", names)
        self.assertIn("batch_n2_1024_square", names)
        self.assertIn("jpeg_compression_50", names)
        self.assertIn("background_auto", names)
        self.assertIn("moderation_low", names)
        self.assertIn("reject_non_multiple_of_16", names)
        self.assertIn("reject_transparent_background", names)
        self.assertNotIn("landscape_4k", names)
        by_name = {case.name: case for case in resolution}
        self.assertEqual(by_name["batch_n2_1024_square"].parameters["n"], 2)
        self.assertEqual(
            by_name["jpeg_compression_50"].parameters["output_compression"],
            50,
        )
        compressed = _with_output_options(
            by_name["jpeg_compression_50"],
            "medium",
            "png",
        )
        self.assertEqual(compressed.parameters["output_format"], "jpeg")
        self.assertEqual(compressed.expected_format, "JPEG")

        with self.assertRaisesRegex(ValueError, "include_4k"):
            gpt_image_2_cases("full")
        full = gpt_image_2_cases("full", include_4k=True)
        self.assertIn("landscape_4k", {case.name for case in full})

    def test_banana_matrix_uses_configurable_resolution_aliases(self) -> None:
        smoke = banana_variant_cases("smoke")
        self.assertEqual(smoke[0].model_override, "nano-banana-pro-1k")
        self.assertEqual(smoke[0].expected_size, (1024, 1024))

        resolution = banana_variant_cases(
            "resolution",
            model_template="provider-banana-{resolution}",
        )
        by_name = {case.name: case for case in resolution}
        self.assertEqual(by_name["banana_2k_aligned"].model_override, "provider-banana-2K")
        self.assertEqual(
            by_name["banana_model_1k_request_2k"].expected_outcome,
            "observation",
        )
        with self.assertRaisesRegex(ValueError, "include_4k"):
            banana_variant_cases("full")
        full = banana_variant_cases("full", include_4k=True)
        self.assertEqual(
            next(case for case in full if case.name == "banana_4k_aligned").expected_size,
            (4096, 4096),
        )

    def test_banana_fixed_model_supports_aligned_resolution_probes(self) -> None:
        cases = banana_variant_cases(
            "resolution",
            model_template="gemini-3-pro-image",
            include_cross_control=False,
        )
        self.assertEqual(
            [case.name for case in cases],
            ["banana_1k_aligned", "banana_2k_aligned"],
        )
        self.assertEqual({case.model_override for case in cases}, {"gemini-3-pro-image"})
        self.assertTrue(all(case.metadata["model_mode"] == "fixed" for case in cases))
        self.assertTrue(all(case.metadata["model_expected_size"] is None for case in cases))

        with self.assertRaisesRegex(ValueError, "fixed Banana model"):
            banana_variant_cases(
                "resolution",
                model_template="gemini-3-pro-image",
            )

    def test_latest_banana_contract_adds_native_resolution_and_aspect_probes(self) -> None:
        cases = banana_variant_cases(
            "resolution",
            model_template="gemini-3.1-flash-image",
            include_cross_control=False,
            transport="gemini-interactions",
        )
        by_name = {case.name: case for case in cases}
        self.assertEqual(
            list(by_name),
            [
                "banana_1k_aligned",
                "banana_2k_aligned",
                "banana_512_square",
                "banana_1k_landscape_16_9",
                "banana_reject_lowercase_1k",
                "banana_reject_aspect_ratio_7_5",
            ],
        )
        self.assertEqual(by_name["banana_512_square"].expected_size, (512, 512))
        self.assertEqual(
            by_name["banana_1k_landscape_16_9"].expected_size,
            (1376, 768),
        )
        self.assertEqual(
            by_name["banana_1k_landscape_16_9"].metadata["aspect_ratio"],
            "16:9",
        )
        self.assertEqual(
            by_name["banana_reject_lowercase_1k"].expected_outcome,
            "rejection",
        )
        self.assertEqual(
            by_name["banana_reject_aspect_ratio_7_5"].metadata["aspect_ratio"],
            "7:5",
        )

    def test_grok_matrix_uses_official_parameters_and_2k_billing_gate(self) -> None:
        smoke = grok_imagine_cases("smoke")
        self.assertEqual([case.name for case in smoke], ["grok_1k_square_b64"])
        self.assertEqual(
            smoke[0].parameters,
            {
                "n": 1,
                "aspect_ratio": "1:1",
                "resolution": "1k",
                "response_format": "b64_json",
            },
        )
        self.assertEqual(smoke[0].expected_size, (1024, 1024))
        self.assertIsNone(smoke[0].expected_format)

        resolution = grok_imagine_cases("resolution")
        by_name = {case.name: case for case in resolution}
        self.assertEqual(len(resolution), 7)
        landscape = by_name["grok_1k_landscape_16_9"]
        self.assertIsNone(landscape.expected_size)
        self.assertEqual(
            landscape.metadata["expected_aspect_ratio"],
            "16:9",
        )
        self.assertTrue(
            evaluate_case(
                landscape,
                status_code=200,
                images=[_info("JPEG", 1280, 720, byte_length=100_000)],
            )["pass"]
        )
        wrong_ratio = evaluate_case(
            landscape,
            status_code=200,
            images=[_info("JPEG", 1024, 1024, byte_length=100_000)],
        )
        self.assertFalse(wrong_ratio["pass"])
        self.assertIn("aspect_ratio_mismatch", " ".join(wrong_ratio["failures"]))
        self.assertEqual(by_name["grok_1k_batch_n2"].parameters["n"], 2)
        self.assertEqual(
            by_name["grok_1k_square_url"].parameters["response_format"],
            "url",
        )
        self.assertNotIn("grok_reject_n11", by_name)

        with self.assertRaisesRegex(ValueError, "include_2k"):
            grok_imagine_cases("full")
        full = grok_imagine_cases("full", include_2k=True)
        full_by_name = {case.name: case for case in full}
        self.assertEqual(len(full), 11)
        self.assertIsNone(full_by_name["grok_2k_portrait_9_16"].expected_size)
        self.assertEqual(
            full_by_name["grok_2k_square_b64"].expected_size,
            (2048, 2048),
        )
        self.assertEqual(
            full_by_name["grok_reject_n11"].expected_outcome,
            "rejection",
        )
        self.assertTrue(all("size" not in case.parameters for case in full))
        self.assertEqual(
            len(grok_imagine_cases("full", include_2k=True, include_negative=False)),
            8,
        )

    def test_png_is_structurally_validated_and_dimensions_are_read(self) -> None:
        raw = _png(32, 24)
        info = inspect_image_bytes(raw, visual_forensics=False)

        self.assertEqual(info.format, "PNG")
        self.assertEqual((info.width, info.height), (32, 24))
        self.assertFalse(info.has_alpha)
        self.assertEqual(info.pixel_count, 768)
        self.assertEqual(len(info.sha256), 64)

        damaged = bytearray(raw)
        damaged[-1] ^= 0x01
        with self.assertRaisesRegex(ValueError, "CRC"):
            inspect_image_bytes(bytes(damaged), visual_forensics=False)

    def test_positive_case_requires_actual_size_and_format(self) -> None:
        case = gpt_image_2_cases("smoke")[0]
        correct = _info("PNG", 1024, 1024, byte_length=800_000)
        passed = evaluate_case(case, status_code=200, images=[correct])
        self.assertTrue(passed["pass"])
        self.assertEqual(passed["verification_level"], "constraint_verified")

        wrong = _info("JPEG", 512, 512, byte_length=80_000)
        failed = evaluate_case(case, status_code=200, images=[wrong])
        self.assertFalse(failed["pass"])
        self.assertTrue(any("format_mismatch" in item for item in failed["failures"]))
        self.assertTrue(any("dimension_mismatch" in item for item in failed["failures"]))

    def test_negative_case_requires_parameter_error_not_server_error(self) -> None:
        case = next(
            case
            for case in gpt_image_2_cases("resolution")
            if case.name == "reject_non_multiple_of_16"
        )
        self.assertTrue(evaluate_case(case, status_code=400)["pass"])
        self.assertTrue(evaluate_case(case, status_code=422)["pass"])
        self.assertFalse(evaluate_case(case, status_code=200)["pass"])
        self.assertFalse(evaluate_case(case, status_code=500)["pass"])

    def test_postprocess_inference_is_never_confirmed(self) -> None:
        flat = [
            _result("baseline", 1024, 1024, tokens=272, latency_ms=30_000, image_bytes=800_000, residual=0.20),
            _result("2k", 2048, 2048, tokens=275, latency_ms=32_000, image_bytes=900_000, residual=0.10),
        ]
        suspicious = infer_postprocess_suspicion(flat)
        self.assertEqual(suspicious["verdict"], "strongly_suspected")
        self.assertFalse(suspicious["confirmed"])
        self.assertIn("output_tokens_nearly_flat", suspicious["comparisons"][0]["evidence"])

        scaled = [
            _result("baseline", 1024, 1024, tokens=200, latency_ms=10_000, image_bytes=500_000, residual=0.10),
            _result("2k", 2048, 2048, tokens=800, latency_ms=40_000, image_bytes=2_000_000, residual=0.12),
        ]
        inconclusive = infer_postprocess_suspicion(scaled)
        self.assertEqual(inconclusive["verdict"], "unknown")
        self.assertFalse(inconclusive["confirmed"])

        chat_usage = [
            _result("baseline", 1024, 1024, tokens=0, latency_ms=10_000, image_bytes=500_000, residual=0.10),
            _result("2k", 2048, 2048, tokens=0, latency_ms=40_000, image_bytes=2_000_000, residual=0.12),
        ]
        chat_usage[0]["usage"] = {
            "output_tokens": 0,
            "completion_tokens": 1400,
            "completion_tokens_details": {"image_tokens": 1120},
        }
        chat_usage[1]["usage"] = {
            "output_tokens": 0,
            "completion_tokens": 2100,
            "completion_tokens_details": {"image_tokens": 1680},
        }
        chat_inference = infer_postprocess_suspicion(chat_usage)
        self.assertEqual(
            chat_inference["comparisons"][0]["output_token_ratio"],
            1.5,
        )

        grok_cases = {
            case.name: case
            for case in grok_imagine_cases("full", include_2k=True)
        }
        grok_results = [
            evaluate_case(
                grok_cases["grok_1k_square_b64"],
                status_code=200,
                images=[_info("JPEG", 1024, 1024, byte_length=500_000)],
                latency_ms=5_000,
            ),
            evaluate_case(
                grok_cases["grok_2k_square_b64"],
                status_code=200,
                images=[_info("PNG", 2048, 2048, byte_length=2_000_000)],
                latency_ms=15_000,
            ),
        ]
        grok_inference = infer_postprocess_suspicion(grok_results)
        self.assertEqual(len(grok_inference["observations"]), 2)
        self.assertEqual(grok_inference["comparisons"][0]["pixel_ratio"], 4.0)
        self.assertFalse(grok_inference["confirmed"])

    def test_banana_crossed_cases_classify_resolution_control(self) -> None:
        crossed = [
            case
            for case in banana_variant_cases("resolution")
            if case.metadata.get("control_probe") == "crossed"
        ]
        results = []
        for case in crossed:
            requested_width, requested_height = map(
                int, str(case.parameters["size"]).split("x")
            )
            result = evaluate_case(
                case,
                status_code=200,
                images=[
                    _info(
                        "PNG",
                        requested_width,
                        requested_height,
                        byte_length=500_000,
                    )
                ],
            )
            result["model"] = case.model_override
            results.append(result)

        inference = infer_resolution_correspondence(results)
        self.assertEqual(inference["verdict"], "request_parameter_controls")
        self.assertTrue(inference["confirmed"])

        rejected = [evaluate_case(case, status_code=400) for case in crossed]
        rejection_inference = infer_resolution_correspondence(rejected)
        self.assertEqual(rejection_inference["verdict"], "conflict_rejected")

    def test_live_runner_saves_sanitized_artifact_and_passes_expected_rejection(self) -> None:
        raw = _png(1024, 1024)
        success_response = FakeResponse(
            200,
            {
                "created": 1,
                "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}],
                "usage": {"input_tokens": 10, "output_tokens": 272, "total_tokens": 282},
            },
            {"content-type": "application/json", "x-request-id": "request-1"},
        )
        success_session = FakeSession(success_response)
        positive_case = gpt_image_2_cases("smoke")[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_case(
                success_session,  # type: ignore[arg-type]
                "https://provider.example/v1/images/generations",
                "gpt-image-2",
                "test prompt",
                positive_case,
                timeout=300,
                images_dir=Path(tmpdir),
                visual_forensics=False,
            )
            self.assertTrue(result["pass"])
            self.assertEqual(result["response_headers"]["x-request-id"], "request-1")
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertEqual(result["token_audit"]["exchanges"][0]["reported"]["input_tokens"], 10)
            self.assertEqual(
                result["token_audit"]["exchanges"][0]["output_accuracy"]["status"],
                "not_available",
            )
            self.assertEqual(result["model_identity_audit"]["status"], "unverifiable")
            self.assertTrue((Path(tmpdir) / "baseline_1024_square_1.png").exists())
            sent_body = success_session.calls[0][1]
            self.assertEqual(sent_body["size"], "1024x1024")
            self.assertNotIn("api_key", sent_body)
            self.assertTrue(success_session.asserted_no_redirects)

        negative_case = next(
            case
            for case in gpt_image_2_cases("resolution")
            if case.name == "reject_non_multiple_of_16"
        )
        rejection_session = FakeSession(
            FakeResponse(400, {"error": {"type": "invalid_request_error", "message": "bad size"}})
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rejected = run_case(
                rejection_session,  # type: ignore[arg-type]
                "https://provider.example/v1/images/generations",
                "gpt-image-2",
                "test prompt",
                negative_case,
                timeout=300,
                images_dir=Path(tmpdir),
                visual_forensics=False,
            )
        self.assertTrue(rejected["pass"])
        self.assertEqual(rejected["status"], "expected_rejection")

    def test_grok_runner_sends_only_official_fields_and_accepts_actual_format(self) -> None:
        raw = _png(1024, 1024)
        session = FakeSession(
            FakeResponse(
                200,
                {
                    "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}],
                    "usage": {"cost_in_usd_ticks": 200000000},
                },
            )
        )
        case = grok_imagine_cases("smoke")[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_case(
                session,  # type: ignore[arg-type]
                "https://provider.example/v1/images/generations",
                "grok-imagine-image",
                "test prompt",
                case,
                timeout=300,
                images_dir=Path(tmpdir),
                visual_forensics=False,
            )

        self.assertTrue(result["pass"])
        self.assertEqual(result["actual_images"][0]["format"], "PNG")
        self.assertEqual(
            session.calls[0][1],
            {
                "model": "grok-imagine-image",
                "prompt": "test prompt",
                "n": 1,
                "aspect_ratio": "1:1",
                "resolution": "1k",
                "response_format": "b64_json",
            },
        )
        self.assertNotIn("size", session.calls[0][1])
        self.assertNotIn("quality", session.calls[0][1])
        self.assertNotIn("output_format", session.calls[0][1])

    def test_url_image_download_uses_safe_user_agent_without_provider_auth(self) -> None:
        response = Mock()
        response.content = _png(8, 8)
        response.raise_for_status.return_value = None
        with patch(
            "scripts.image_param_test.requests.get",
            return_value=response,
        ) as mocked_get:
            raw, delivery = _image_bytes(
                {"url": "https://imgen.example/output.jpg"},
                300,
            )

        self.assertEqual(raw, response.content)
        self.assertEqual(delivery, "url")
        headers = mocked_get.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept"], "image/*")
        self.assertIn("yibuapi-image-param-test", headers["User-Agent"])
        self.assertNotIn("Authorization", headers)

    def test_chat_image_runner_uses_image_config_and_decodes_markdown_data_url(self) -> None:
        raw = _png(1024, 1024)
        encoded = base64.b64encode(raw).decode("ascii")
        response = FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"![image](data:image/png;base64,{encoded})",
                        }
                    }
                ],
                "usage": {"completion_tokens": 1120},
            },
        )
        session = FakeSession(response)
        case = _with_output_options(
            banana_variant_cases(
                "smoke",
                model_template="gemini-3.1-flash-image",
            )[0],
            "low",
            "png",
            transport="chat-completions",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_case(
                session,  # type: ignore[arg-type]
                "https://provider.example/v1/chat/completions",
                "gemini-3.1-flash-image",
                "test prompt",
                case,
                timeout=300,
                images_dir=Path(tmpdir),
                visual_forensics=False,
                transport="chat-completions",
            )
            self.assertTrue(result["pass"])
            self.assertEqual(result["actual_images"][0]["width"], 1024)
            self.assertTrue((Path(tmpdir) / "banana_1k_aligned_1.png").exists())

        sent_body = session.calls[0][1]
        self.assertEqual(
            sent_body["extra_body"],
            {
                "google": {
                    "image_config": {
                        "aspect_ratio": "1:1",
                        "image_size": "1K",
                    }
                }
            },
        )
        self.assertNotIn("size", sent_body)
        self.assertNotIn("quality", sent_body)
        self.assertNotIn("output_format", sent_body)

    def test_gemini_interactions_runner_uses_native_contract_and_decodes_steps(self) -> None:
        raw = _jpeg(1024, 1024)
        encoded = base64.b64encode(raw).decode("ascii")
        response = FakeResponse(
            200,
            {
                "id": "interaction-1",
                "status": "completed",
                "model": "models/gemini-3.1-flash-image",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/jpeg",
                                "data": encoded,
                            }
                        ],
                    }
                ],
                "usage": {
                    "total_input_tokens": 8,
                    "total_output_tokens": 1120,
                    "total_thought_tokens": 0,
                    "total_tokens": 1128,
                },
            },
        )
        session = FakeSession(response)
        case = _with_output_options(
            banana_variant_cases(
                "smoke",
                model_template="gemini-3.1-flash-image",
            )[0],
            "low",
            "jpeg",
            transport="gemini-interactions",
        )
        endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
        credential = ProviderCredential.create(
            provider="gemini-image-test",
            secret="test-google-key",
            base_urls=[endpoint],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_case(
                session,  # type: ignore[arg-type]
                endpoint,
                "gemini-3.1-flash-image",
                "test prompt",
                case,
                timeout=300,
                images_dir=Path(tmpdir),
                visual_forensics=False,
                credential=credential,
                transport="gemini-interactions",
                auth_mode="google_api_key",
            )

        self.assertTrue(result["pass"])
        self.assertEqual(result["actual_images"][0]["width"], 1024)
        self.assertEqual(
            session.request_headers[0]["x-goog-api-key"],
            "test-google-key",
        )
        sent_body = session.calls[0][1]
        self.assertEqual(
            sent_body,
            {
                "model": "gemini-3.1-flash-image",
                "input": [{"type": "text", "text": "test prompt"}],
                "response_format": {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "aspect_ratio": "1:1",
                    "image_size": "1K",
                },
            },
        )
        self.assertEqual(result["model_identity_audit"]["status"], "match")
        self.assertEqual(
            result["token_audit"]["exchanges"][0]["usage_accounting"][
                "input_tokens"
            ],
            8,
        )

    def test_gemini_interactions_helpers_ignore_non_model_image_steps(self) -> None:
        payload = {
            "steps": [
                {
                    "type": "thought",
                    "content": [{"type": "image", "data": "not-final"}],
                },
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": "done"},
                        {"type": "image", "data": "aW1hZ2U="},
                        {"type": "image", "uri": "https://images.example/final.png"},
                    ],
                },
            ]
        }
        self.assertEqual(
            _response_image_items(payload, "gemini-interactions"),
            [
                {"b64_json": "aW1hZ2U="},
                {"url": "https://images.example/final.png"},
            ],
        )
        case = _with_output_options(
            banana_variant_cases(
                "smoke",
                model_template="gemini-3.1-flash-image",
            )[0],
            "low",
            "jpeg",
            transport="gemini-interactions",
        )
        self.assertEqual(
            _request_body(
                case,
                "gemini-3.1-flash-image",
                "prompt",
                "gemini-interactions",
            )["response_format"]["mime_type"],
            "image/jpeg",
        )
        with self.assertRaisesRegex(ValueError, "only jpeg"):
            _with_output_options(
                banana_variant_cases(
                    "smoke",
                    model_template="gemini-3.1-flash-image",
                )[0],
                "low",
                "png",
                transport="gemini-interactions",
            )

    def test_endpoint_normalization(self) -> None:
        endpoint = normalize_image_generation_endpoint("https://provider.example")
        self.assertEqual(endpoint, "https://provider.example/v1/images/generations")
        self.assertEqual(models_endpoint(endpoint), "https://provider.example/v1/models")
        self.assertEqual(
            models_endpoint(endpoint, family="grok-imagine"),
            "https://provider.example/v1/image-generation-models",
        )
        self.assertEqual(
            normalize_image_generation_endpoint("https://provider.example/v1"),
            endpoint,
        )
        with self.assertRaisesRegex(ValueError, "must not contain"):
            normalize_image_generation_endpoint("https://provider.example/v1?api_key=secret")
        self.assertEqual(normalize_image_generation_endpoint(endpoint), endpoint)
        chat_endpoint = normalize_image_endpoint(
            "https://provider.example/v1",
            "chat-completions",
        )
        self.assertEqual(chat_endpoint, "https://provider.example/v1/chat/completions")
        self.assertEqual(
            models_endpoint(chat_endpoint, "chat-completions"),
            "https://provider.example/v1/models",
        )
        interactions_endpoint = normalize_image_endpoint(
            "https://generativelanguage.googleapis.com",
            "gemini-interactions",
        )
        self.assertEqual(
            interactions_endpoint,
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        self.assertEqual(
            models_endpoint(interactions_endpoint, "gemini-interactions"),
            "https://generativelanguage.googleapis.com/v1beta/models",
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            normalize_image_endpoint(endpoint, "chat-completions")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_image_generation_endpoint("http://provider.example")


def _png(width: int, height: int) -> bytes:
    signature = bytes.fromhex("89504e470d0a1a0a")
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = bytes([0]) + bytes([32, 96, 160]) * width
    raw_pixels = row * height
    return b"".join(
        [
            signature,
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(raw_pixels, level=9)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _jpeg(width: int, height: int) -> bytes:
    return b"".join(
        [
            bytes.fromhex("ffd8ffc0"),
            struct.pack(">H", 11),
            bytes([8]),
            struct.pack(">HH", height, width),
            bytes([1, 1, 0x11, 0]),
            bytes.fromhex("ffd9"),
        ]
    )


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def _info(
    image_format: str,
    width: int,
    height: int,
    *,
    byte_length: int,
) -> ImageInfo:
    return ImageInfo(
        format=image_format,
        width=width,
        height=height,
        byte_length=byte_length,
        sha256="a" * 64,
        visual_metrics={"available": False},
    )


def _result(
    case: str,
    width: int,
    height: int,
    *,
    tokens: int,
    latency_ms: float,
    image_bytes: int,
    residual: float,
) -> dict:
    return {
        "case": case,
        "pass": True,
        "status": "pass",
        "requested": {"size": f"{width}x{height}"},
        "latency_ms": latency_ms,
        "usage": {"output_tokens": tokens},
        "actual_images": [
            {
                "width": width,
                "height": height,
                "byte_length": image_bytes,
                "visual_metrics": {"half_scale_residual_ratio": residual},
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
