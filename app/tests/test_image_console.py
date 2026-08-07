from __future__ import annotations

import base64
import copy
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lib.config import (
    get_image_auth_mode,
    get_image_endpoint,
    list_image_providers,
    list_public_providers,
    validate_provider_config,
)
from lib.job_spec import resolve_image_plan
import scripts.web_console as web_console
from scripts.web_console import Job, JobManager, _image_command_for_job


FAKE_KEY_ENV = "FAKE_IMAGE_CONSOLE_API_KEY"
FAKE_KEY = "fake-image-console-secret-123"


def _image_config(
    base_url: str = "https://image-provider.example/v1",
    *,
    image_model: str = "gpt-image-2",
    family: str = "gpt-image-2",
    transport: str = "images-generations",
    allowed_transports: list[str] | None = None,
) -> dict:
    image_model_config = {
        "id": image_model,
        "family": family,
        "transport": transport,
    }
    if allowed_transports is not None:
        image_model_config["allowed_transports"] = allowed_transports
    return {
        "active_provider": "fake",
        "api": {"timeout_sec": 12},
        "providers": {
            "fake": {
                "label": "Fake provider",
                "base_url": base_url,
                "api_key_env": FAKE_KEY_ENV,
                "backend": "openai_compatible",
                "route_profile": "dynamic_aggregator",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                    "images_generations": {
                        "path": "/images/generations",
                        "auth": "bearer",
                    },
                    "gemini_interactions": {
                        "base_url": base_url.removesuffix("/v1"),
                        "path": "/v1beta/interactions",
                        "auth": "google_api_key",
                    },
                },
                "models": {
                    "default": "gpt-4o",
                    "candidates": ["gpt-4o"],
                    "families": {"gpt-4o": "gpt"},
                    "transports": {"gpt-4o": "chat_completions"},
                    "api_forms": {
                        "gpt-4o": {
                            "openai_chat_completions": {
                                "route_profile": "dynamic_aggregator",
                            }
                        }
                    },
                    "default_api_forms": {
                        "gpt-4o": "openai_chat_completions",
                    },
                    "reference_sources": {},
                },
                "image": {
                    "enabled": True,
                    "default": image_model,
                    "models": [image_model_config],
                },
            }
        },
        "profile_weights": {"throughput": 1},
    }


def _png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + (b"\x7f\x7f\x7f" * width)
    return signature + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(scanline * height)
    ) + chunk(b"IEND", b"")


class _FakeImageHandler(BaseHTTPRequestHandler):
    image_bytes = _png(1024, 1024)
    authorizations: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.authorizations.append(str(self.headers.get("Authorization") or ""))
        if self.path != "/v1/models":
            self._send(404, {"error": {"message": "not found"}})
            return
        self._send(200, {"data": [{"id": "gpt-image-2"}]})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self.authorizations.append(str(self.headers.get("Authorization") or ""))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/images/generations":
            self._send(404, {"error": {"message": "not found"}})
            return
        if body.get("model") != "gpt-image-2" or body.get("size") != "1024x1024":
            self._send(400, {"error": {"message": "unexpected request"}})
            return
        self._send(
            200,
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(self.image_bytes).decode("ascii"),
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 272,
                    "total_tokens": 282,
                },
            },
        )

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class ImageProviderConfigTest(unittest.TestCase):
    def test_chat_provider_can_add_image_capability_without_changing_public_chat_shape(self) -> None:
        config = _image_config()
        validate_provider_config(config)

        chat_provider = list_public_providers(config)[0]
        image_provider = list_image_providers(config)[0]
        self.assertNotIn("image", chat_provider)
        self.assertEqual(chat_provider["models"]["default"], "gpt-4o")
        self.assertEqual(image_provider["default_model"], "gpt-image-2")
        self.assertEqual(image_provider["models"][0]["family"], "gpt-image-2")
        self.assertEqual(
            get_image_endpoint(config, "fake", "images-generations"),
            "https://image-provider.example/v1/images/generations",
        )

        chat_only = copy.deepcopy(config)
        chat_only["providers"]["fake"].pop("image")
        chat_only["providers"]["fake"]["api_interfaces"].pop("images_generations")
        validate_provider_config(chat_only)
        self.assertEqual(list_image_providers(chat_only), [])

    def test_image_config_rejects_invalid_default_interface_auth_and_transport(self) -> None:
        invalid: list[tuple[dict, str]] = []

        bad_default = _image_config()
        bad_default["providers"]["fake"]["image"]["default"] = "missing"
        invalid.append((bad_default, "image.default"))

        bad_auth = _image_config()
        bad_auth["providers"]["fake"]["api_interfaces"]["images_generations"]["auth"] = "anthropic"
        invalid.append((bad_auth, "auth must be bearer"))

        bad_family_transport = _image_config(
            allowed_transports=["images-generations", "chat-completions"]
        )
        invalid.append((bad_family_transport, "non-Banana"))

        duplicate_transport = _image_config(
            allowed_transports=["images-generations", "images-generations"]
        )
        invalid.append((duplicate_transport, "must not contain duplicates"))

        duplicate_model = _image_config()
        duplicate_model["providers"]["fake"]["image"]["models"].append(
            copy.deepcopy(duplicate_model["providers"]["fake"]["image"]["models"][0])
        )
        invalid.append((duplicate_model, "duplicate model"))

        for config, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_provider_config(config)

    def test_native_gemini_interactions_image_interface_uses_google_auth(self) -> None:
        config = _image_config(
            image_model="gemini-3.1-flash-image",
            family="banana",
            transport="gemini-interactions",
        )
        validate_provider_config(config)

        self.assertEqual(
            get_image_endpoint(config, "fake", "gemini-interactions"),
            "https://image-provider.example/v1beta/interactions",
        )
        self.assertEqual(
            get_image_auth_mode(config, "fake", "gemini-interactions"),
            "google_api_key",
        )
        public_model = list_image_providers(config)[0]["models"][0]
        self.assertEqual(public_model["transport"], "gemini-interactions")

        wrong_auth = copy.deepcopy(config)
        wrong_auth["providers"]["fake"]["api_interfaces"]["gemini_interactions"][
            "auth"
        ] = "anthropic"
        with self.assertRaisesRegex(ValueError, "google_api_key"):
            validate_provider_config(wrong_auth)


class ImagePlanTest(unittest.TestCase):
    def test_image_plan_resolves_route_before_api_form(self) -> None:
        config = _image_config(
            image_model="gemini-3.1-flash-image",
            family="banana",
            transport="chat-completions",
        )
        model_cfg = config["providers"]["fake"]["image"]["models"][0]
        model_cfg.pop("transport")
        model_cfg["routes"] = {
            "provider_compat": {
                "api_forms": {
                    "openai_chat_completions": {
                        "transport": "chat-completions"
                    }
                }
            },
            "google_ai_studio": {
                "api_forms": {
                    "gemini_interactions": {
                        "transport": "gemini-interactions"
                    }
                }
            },
        }
        model_cfg["default_route_profile"] = "provider_compat"
        model_cfg["default_api_forms"] = {
            "provider_compat": "openai_chat_completions",
            "google_ai_studio": "gemini_interactions",
        }

        with self.assertRaisesRegex(ValueError, "on route 'provider_compat'"):
            resolve_image_plan(
                config,
                {"image_plan": {"api_form": "gemini_interactions"}},
                "fake",
                "gemini-3.1-flash-image",
                60,
            )

        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "route_profile": "google_ai_studio",
                    "api_form": "gemini_interactions",
                    "no_cross_control": True,
                    "output_format": "jpeg",
                }
            },
            "fake",
            "gemini-3.1-flash-image",
            60,
        )
        self.assertEqual(plan["route_profile"], "google_ai_studio")
        self.assertEqual(plan["api_form"], "gemini_interactions")

    def test_image_plan_rejects_unregistered_route_before_request_build(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not expose route profile"):
            resolve_image_plan(
                _image_config(),
                {
                    "image_plan": {
                        "route_profile": "unregistered-route",
                        "suite": "smoke",
                    }
                },
                "fake",
                "gpt-image-2",
                90,
            )

    def test_gpt_plan_validates_billing_cases_and_command_contract(self) -> None:
        config = _image_config()
        with self.assertRaisesRegex(ValueError, "image_plan must be an object"):
            resolve_image_plan(
                config,
                {"image_plan": []},
                "fake",
                "gpt-image-2",
                90,
            )
        with self.assertRaisesRegex(ValueError, "requires include_4k"):
            resolve_image_plan(
                config,
                {"image_plan": {"suite": "full"}},
                "fake",
                "gpt-image-2",
                90,
            )
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            resolve_image_plan(
                config,
                {"image_plan": {"include_4k": "false"}},
                "fake",
                "gpt-image-2",
                90,
            )
        with self.assertRaisesRegex(ValueError, "is not allowed"):
            resolve_image_plan(
                config,
                {"image_plan": {"transport": "chat-completions"}},
                "fake",
                "gpt-image-2",
                90,
            )
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            resolve_image_plan(
                config,
                {
                    "image_plan": {
                        "cases": ["baseline_1024_square", "baseline_1024_square"]
                    }
                },
                "fake",
                "gpt-image-2",
                90,
            )
        with self.assertRaisesRegex(ValueError, "Unknown image test case"):
            resolve_image_plan(
                config,
                {"image_plan": {"cases": ["not-a-case"]}},
                "fake",
                "gpt-image-2",
                90,
            )

        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "suite": "resolution",
                    "include_4k": True,
                    "quality": "high",
                    "output_format": "webp",
                    "no_negative": True,
                    "visual_forensics": False,
                    "cases": ["baseline_1024_square", "landscape_4k"],
                }
            },
            "fake",
            "gpt-image-2",
            90,
        )
        self.assertEqual(plan["family"], "gpt-image-2")
        self.assertEqual(plan["estimated_case_count"], 2)
        command = _image_command_for_job(Path("/tmp/image-report"), plan)
        self.assertEqual(command[command.index("--model") + 1], "gpt-image-2")
        self.assertEqual(
            command[command.index("--route-profile") + 1],
            "dynamic_aggregator",
        )
        self.assertEqual(
            command[command.index("--api-form") + 1],
            "openai_images_generations",
        )
        self.assertEqual(command[command.index("--quality") + 1], "high")
        self.assertEqual(command[command.index("--output-format") + 1], "webp")
        self.assertEqual(command[command.index("--timeout") + 1], "90")
        self.assertEqual(command.count("--case"), 2)
        self.assertIn("--include-4k", command)
        self.assertIn("--no-negative", command)
        self.assertIn("--no-visual-forensics", command)
        self.assertEqual(
            command[command.index("--api-key-env") + 1],
            "LOADTEST_SELECTED_API_KEY",
        )
        self.assertNotIn(FAKE_KEY, command)

    def test_grok_plan_uses_2k_gate_and_omits_gpt_output_options(self) -> None:
        config = _image_config(
            image_model="grok-imagine-image",
            family="grok-imagine",
        )
        validate_provider_config(config)
        with self.assertRaisesRegex(ValueError, "include_2k"):
            resolve_image_plan(
                config,
                {"image_plan": {"suite": "full"}},
                "fake",
                "grok-imagine-image",
                90,
            )
        with self.assertRaisesRegex(ValueError, "supports 1K/2K"):
            resolve_image_plan(
                config,
                {"image_plan": {"suite": "resolution", "include_4k": True}},
                "fake",
                "grok-imagine-image",
                90,
            )

        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "suite": "full",
                    "include_2k": True,
                    "quality": "high",
                    "output_format": "webp",
                    "visual_forensics": False,
                }
            },
            "fake",
            "grok-imagine-image",
            90,
        )
        self.assertEqual(plan["family"], "grok-imagine")
        self.assertTrue(plan["include_2k"])
        self.assertFalse(plan["include_4k"])
        self.assertIsNone(plan["quality"])
        self.assertIsNone(plan["output_format"])
        self.assertEqual(plan["estimated_case_count"], 11)

        command = _image_command_for_job(Path("/tmp/grok-report"), plan)
        self.assertIn("--include-2k", command)
        self.assertNotIn("--include-4k", command)
        self.assertNotIn("--quality", command)
        self.assertNotIn("--output-format", command)

    def test_banana_fixed_model_requires_cross_control_skip_and_normalizes_chat_options(self) -> None:
        config = _image_config(
            image_model="gemini-3-pro-image",
            family="banana",
            transport="chat-completions",
        )
        validate_provider_config(config)
        with self.assertRaisesRegex(ValueError, "fixed Banana model"):
            resolve_image_plan(
                config,
                {"image_plan": {"suite": "resolution"}},
                "fake",
                "gemini-3-pro-image",
                60,
            )

        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "suite": "resolution",
                    "no_cross_control": True,
                    "quality": "high",
                    "output_format": "jpeg",
                }
            },
            "fake",
            "gemini-3-pro-image",
            60,
        )
        self.assertIsNone(plan["quality"])
        self.assertIsNone(plan["output_format"])
        self.assertEqual(plan["estimated_case_count"], 2)
        command = _image_command_for_job(Path("/tmp/banana-report"), plan)
        self.assertNotIn("--quality", command)
        self.assertNotIn("--output-format", command)
        self.assertIn("--no-cross-control", command)

    def test_native_gemini_plan_preserves_format_and_passes_auth_mode(self) -> None:
        config = _image_config(
            image_model="gemini-3.1-flash-image",
            family="banana",
            transport="gemini-interactions",
        )
        validate_provider_config(config)
        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "suite": "resolution",
                    "no_cross_control": True,
                    "output_format": "jpeg",
                }
            },
            "fake",
            "gemini-3.1-flash-image",
            60,
        )

        self.assertEqual(plan["auth_mode"], "google_api_key")
        self.assertIsNone(plan["quality"])
        self.assertIsNone(plan["output_format"])
        self.assertEqual(plan["estimated_case_count"], 6)
        self.assertIn(
            "banana_1k_landscape_16_9",
            plan["model_capability_profile"]["supported_profiles"],
        )
        self.assertIn(
            "banana_reject_lowercase_1k",
            plan["model_capability_profile"]["unsupported_profiles"],
        )
        command = _image_command_for_job(Path("/tmp/gemini-image-report"), plan)
        self.assertEqual(
            command[command.index("--auth-mode") + 1],
            "google_api_key",
        )
        self.assertNotIn("--output-format", command)
        self.assertNotIn("--quality", command)

        with self.assertRaisesRegex(ValueError, "only jpeg"):
            resolve_image_plan(
                config,
                {
                    "image_plan": {
                        "suite": "resolution",
                        "no_cross_control": True,
                        "output_format": "png",
                    }
                },
                "fake",
                "gemini-3.1-flash-image",
                60,
            )


class ImageConsoleJobTest(unittest.TestCase):
    def test_image_create_branch_is_secret_free_and_skips_chat_preflight(self) -> None:
        config = _image_config()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {FAKE_KEY_ENV: FAKE_KEY}
        ), patch.object(
            web_console, "JOBS_ROOT", Path(temp_dir)
        ), patch.object(
            web_console, "load_config", return_value=config
        ), patch.object(
            JobManager, "_load_finished_jobs", return_value=None
        ), patch.object(
            JobManager, "_start_locked", return_value=None
        ), patch.object(
            web_console, "get_model_family", side_effect=AssertionError("chat family called")
        ), patch.object(
            web_console, "_validate_model", side_effect=AssertionError("chat model validation called")
        ), patch.object(
            web_console, "_preflight_job", side_effect=AssertionError("chat preflight called")
        ):
            manager = JobManager()
            with self.assertRaisesRegex(ValueError, "timeout_sec must be positive"):
                manager.create(
                    {
                        "type": "image_param_test",
                        "provider": "fake",
                        "model": "gpt-image-2",
                        "timeout_sec": 0,
                    }
                )
            job = manager.create(
                {
                    "type": "image_param_test",
                    "provider": "fake",
                    "model": "gpt-image-2",
                    "timeout_sec": 45,
                    "image_plan": {
                        "suite": "smoke",
                        "visual_forensics": False,
                    },
                }
            )

            self.assertEqual(job.type, "image_param_test")
            self.assertEqual(job.workload, "image_param")
            self.assertIsNone(job.reference_source)
            self.assertEqual(job.image_plan["estimated_case_count"], 1)
            serialized = json.dumps(job.job_spec)
            self.assertNotIn(FAKE_KEY, serialized)
            self.assertNotIn(FAKE_KEY, " ".join(job.command))
            self.assertIn("--api-key-env", job.command)
            self.assertTrue((job.report_dir / "job_spec.json").exists())
            with self.assertRaisesRegex(ValueError, "is still queued"):
                manager.create(
                    {
                        "type": "image_param_test",
                        "provider": "fake",
                        "model": "gpt-image-2",
                    }
                )

    def test_public_artifact_urls_reject_traversal_and_list_omits_case_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir)
            report_dir = reports_root / "jobs" / "image-job"
            images_dir = report_dir / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "ok.png").write_bytes(_png(1, 1))
            (reports_root / "outside.png").write_bytes(_png(1, 1))
            (report_dir / "case_results.json").write_text(
                json.dumps(
                    [
                        {
                            "case": "baseline",
                            "pass": True,
                            "artifacts": [
                                "images/ok.png",
                                "../outside.png",
                                str(reports_root / "outside.png"),
                                "images/missing.png",
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            job = Job(
                id="image-job",
                type="image_param_test",
                provider="fake",
                provider_label="Fake provider",
                model="gpt-image-2",
                model_family="gpt-image-2",
                workload="image_param",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=report_dir,
                command=[],
                status="completed",
                image_plan={"cases": ["baseline"], "estimated_case_count": 1},
            )
            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {job.id: job}

            with patch.object(web_console, "REPORTS_ROOT", reports_root):
                detail = manager.public(job, include_detail=True)
                listing = manager.list()[0]

            self.assertEqual(
                detail["image_results"][0]["artifact_urls"],
                ["/reports/jobs/image-job/images/ok.png"],
            )
            self.assertNotIn("image_results", listing)

    def test_progress_survives_partial_or_temporarily_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir)
            report_dir = reports_root / "jobs" / "partial-image"
            report_dir.mkdir(parents=True)
            job = Job(
                id="partial-image",
                type="image_param_test",
                provider="fake",
                provider_label="Fake provider",
                model="gpt-image-2",
                model_family="gpt-image-2",
                workload="image_param",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=report_dir,
                command=[],
                status="running",
                image_plan={
                    "cases": ["baseline_1024_square", "square_2k"],
                    "estimated_case_count": 2,
                },
            )
            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {job.id: job}
            (report_dir / "plan.json").write_text("{still-writing", encoding="utf-8")
            (report_dir / "case_results.json").write_text(
                json.dumps([{"case": "baseline_1024_square", "pass": True, "latency_ms": 12}]),
                encoding="utf-8",
            )

            with patch.object(web_console, "REPORTS_ROOT", reports_root):
                partial = manager.public(job, include_detail=True)
            self.assertEqual(partial["progress"]["percent"], 50)
            self.assertEqual(partial["progress"]["current_case"], "square_2k")

            (report_dir / "case_results.json").write_text("[still-writing", encoding="utf-8")
            (report_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "pass": True,
                        "case_count": 2,
                        "pass_count": 2,
                        "failure_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            job.status = "completed"
            with patch.object(web_console, "REPORTS_ROOT", reports_root):
                completed = manager.public(job, include_detail=True)
            self.assertEqual(completed["image_results"], [])
            self.assertEqual(completed["progress"]["percent"], 100)
            self.assertEqual(completed["progress"]["pass_count"], 2)

    def test_recovery_uses_job_spec_type_and_keeps_incomplete_cases(self) -> None:
        config = _image_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_root = Path(temp_dir)
            jobs_root = reports_root / "jobs"
            report_dir = jobs_root / "legacy-name-without-type"
            report_dir.mkdir(parents=True)
            image_plan = resolve_image_plan(
                config,
                {"image_plan": {"suite": "smoke", "visual_forensics": False}},
                "fake",
                "gpt-image-2",
                30,
            )
            (report_dir / "job_spec.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "type": "image_param_test",
                        "provider": "fake",
                        "model": "gpt-image-2",
                        "image_plan": image_plan,
                    }
                ),
                encoding="utf-8",
            )
            (report_dir / "plan.json").write_text(
                json.dumps({"cases": [{"name": "baseline_1024_square"}]}),
                encoding="utf-8",
            )
            (report_dir / "case_results.json").write_text(
                json.dumps([{"case": "baseline_1024_square", "pass": True}]),
                encoding="utf-8",
            )

            with patch.object(web_console, "REPORTS_ROOT", reports_root), patch.object(
                web_console, "JOBS_ROOT", jobs_root
            ), patch.object(web_console, "load_config", return_value=config):
                manager = JobManager()
                restored = manager.get(report_dir.name)

            self.assertEqual(restored["type"], "image_param_test")
            self.assertEqual(restored["status"], "failed")
            self.assertEqual(restored["image_results"][0]["case"], "baseline_1024_square")
            self.assertEqual(restored["progress"]["completed_cases"], 1)

    def test_flask_job_runs_fake_api_and_recovers_completed_result(self) -> None:
        _FakeImageHandler.authorizations = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeImageHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        config = _image_config(f"http://127.0.0.1:{server.server_port}/v1")

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                reports_root = Path(temp_dir)
                jobs_root = reports_root / "jobs"
                jobs_root.mkdir()
                with patch.dict(
                    os.environ, {FAKE_KEY_ENV: FAKE_KEY}
                ), patch.object(
                    web_console, "REPORTS_ROOT", reports_root
                ), patch.object(
                    web_console, "JOBS_ROOT", jobs_root
                ), patch.object(
                    web_console, "load_config", return_value=config
                ):
                    manager = JobManager()
                    with patch.object(web_console, "JOB_MANAGER", manager):
                        client = web_console.app.test_client()

                        rejected = client.post(
                            "/api/jobs",
                            json={
                                "type": "image_param_test",
                                "provider": "fake",
                                "model": "gpt-image-2",
                                "image_plan": {"suite": "full"},
                            },
                        )
                        self.assertEqual(rejected.status_code, 400)

                        created = client.post(
                            "/api/jobs",
                            json={
                                "type": "image_param_test",
                                "provider": "fake",
                                "model": "gpt-image-2",
                                "timeout_sec": 10,
                                "image_plan": {
                                    "suite": "smoke",
                                    "quality": "low",
                                    "output_format": "png",
                                    "visual_forensics": False,
                                },
                            },
                        )
                        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
                        job_id = created.get_json()["id"]

                        deadline = time.time() + 15
                        detail = None
                        while time.time() < deadline:
                            response = client.get(f"/api/jobs/{job_id}")
                            self.assertEqual(response.status_code, 200)
                            detail = response.get_json()
                            if detail["status"] in {"completed", "failed"}:
                                break
                            time.sleep(0.05)

                        self.assertIsNotNone(detail)
                        assert detail is not None
                        self.assertEqual(detail["status"], "completed", detail.get("log_tail"))
                        self.assertTrue(detail["image_summary"]["pass"])
                        self.assertEqual(detail["progress"]["percent"], 100)
                        self.assertEqual(detail["progress"]["completed_cases"], 1)
                        artifact_url = detail["image_results"][0]["artifact_urls"][0]
                        artifact = client.get(artifact_url)
                        self.assertEqual(artifact.status_code, 200)
                        self.assertEqual(artifact.data, _FakeImageHandler.image_bytes)
                        artifact.close()

                        list_payload = client.get("/api/jobs").get_json()["jobs"]
                        self.assertNotIn("image_results", list_payload[0])
                        public_text = json.dumps(detail)
                        self.assertNotIn(FAKE_KEY, public_text)
                        job_spec_text = (jobs_root / job_id / "job_spec.json").read_text(
                            encoding="utf-8"
                        )
                        log_text = (jobs_root / job_id / "job.log").read_text(
                            encoding="utf-8"
                        )
                        self.assertNotIn(FAKE_KEY, job_spec_text)
                        self.assertNotIn(FAKE_KEY, log_text)

                    recovered = JobManager()
                    restored = recovered.latest_image_result(
                        "fake",
                        "gpt-image-2",
                        "dynamic_aggregator",
                        "openai_images_generations",
                        "gpt-image-2/gpt-image-2@dynamic_aggregator/openai_images_generations",
                    )
                    self.assertIsNotNone(restored)
                    self.assertIsNone(
                        recovered.latest_image_result(
                            "fake",
                            "gpt-image-2",
                            "vendor_direct",
                            "openai_images_generations",
                            "gpt-image-2/gpt-image-2@vendor_direct/openai_images_generations",
                        )
                    )
                    assert restored is not None
                    self.assertEqual(restored["id"], job_id)
                    self.assertEqual(restored["status"], "completed")
                    self.assertEqual(restored["image_results"][0]["artifact_urls"], [artifact_url])

            self.assertTrue(_FakeImageHandler.authorizations)
            self.assertEqual(
                set(_FakeImageHandler.authorizations),
                {f"Bearer {FAKE_KEY}"},
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


class ImageConsoleFrontendContractTest(unittest.TestCase):
    def test_template_javascript_and_config_expose_image_console_contract(self) -> None:
        config = _image_config()
        with patch.dict(os.environ, {FAKE_KEY_ENV: FAKE_KEY}), patch.object(
            web_console, "load_config", return_value=config
        ):
            client = web_console.app.test_client()
            page = client.get("/")
            api_config = client.get("/api/config")

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for element_id in (
            "tabImage",
            "imageView",
            "imageProvider",
            "imageModel",
            "imageRouteProfile",
            "imageApiForm",
            "imageInclude2k",
            "startImage",
            "stopImage",
            "imageResults",
            "imageLightbox",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertLess(
            html.index('id="imageRouteProfile"'),
            html.index('id="imageApiForm"'),
        )
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "static"
            / "web_console.js"
        ).read_text(encoding="utf-8")
        image_route_handler = script.split(
            '$("imageRouteProfile").addEventListener("change"', 1
        )[1].split('$("imageApiForm").addEventListener("change"', 1)[0]
        self.assertIn('form.apiForm = "";', image_route_handler)
        self.assertIn('appState.imageHistoryResult = null;', image_route_handler)

        self.assertEqual(api_config.status_code, 200)
        payload = api_config.get_json()
        self.assertEqual(payload["image_providers"][0]["name"], "fake")
        self.assertTrue(payload["image_providers"][0]["has_key"])
        self.assertEqual(payload["image_defaults"]["timeout_sec"], 12)
        self.assertFalse(payload["image_defaults"]["include_2k"])
        image_capability = payload["image_model_capabilities"]["fake"]["gpt-image-2"]
        self.assertIn("dynamic_aggregator", image_capability["routes"])
        self.assertEqual(
            image_capability["routes"]["dynamic_aggregator"][
                "certification_scope"
            ],
            "adapter_only",
        )
        self.assertIn(
            "openai_images_generations",
            image_capability["routes"]["dynamic_aggregator"]["api_forms"],
        )
        self.assertNotIn(FAKE_KEY, json.dumps(payload))

        script = (web_console.PROJECT_ROOT / "scripts/static/web_console.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('if (type === "image_param_test") return "image";', script)
        self.assertIn('!["param_test", "image_param_test", "cache_suite"]', script)
        self.assertIn('"startParam", "startImage", "startQuickLoad"', script)
        self.assertIn('"globalStop", "stopParam", "stopImage"', script)
        self.assertIn("|| !imageProvider", script)
        self.assertIn("|| !imageProvider.has_key", script)
        self.assertIn('model.family === "grok-imagine"', script)
        self.assertIn('form.outputFormat = "jpeg"', script)
        self.assertIn("form.noNegative ? 8 : 13", script)
        self.assertIn('$("imageNoNegativeWrap").hidden = !model;', script)


if __name__ == "__main__":
    unittest.main()
