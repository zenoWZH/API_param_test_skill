from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.web_console as web_console
from scripts.web_console import Job, JobManager, _resolve_cache_measured_requests


class ParamHistoryTest(unittest.TestCase):
    def test_route_selector_precedes_form_and_resets_dependent_state(self) -> None:
        client = web_console.app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="paramRouteProfile"'), html.index('id="paramApiForm"'))
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "static" / "web_console.js"
        ).read_text(encoding="utf-8")
        route_handler = script.split(
            '$("paramRouteProfile").addEventListener("change"', 1
        )[1].split('$("paramApiForm").addEventListener("change"', 1)[0]
        self.assertIn('form.apiForm = "";', route_handler)
        self.assertIn('form.referenceSource = "";', route_handler)
        self.assertIn('appState.paramHistoryResult = null;', route_handler)

    def test_family_only_param_specs_keeps_family_reference_view(self) -> None:
        client = web_console.app.test_client()

        for family, expected_source in (
            ("gpt", "openai_chat_base"),
            ("qwen", "qwen_openai_compat"),
            ("gemini", "gemini_openai_compat"),
            ("claude", "claude_native_messages"),
        ):
            with self.subTest(family=family):
                response = client.get(f"/api/param-specs?family={family}")
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["reference_source"], expected_source)
                self.assertNotIn("model_capability_profile", payload)

        legacy_family = client.get("/api/param-specs?family=openai")
        self.assertEqual(legacy_family.status_code, 400)
        self.assertIn("No reference source", legacy_family.get_json()["error"])

    def test_cache_console_uses_progressive_defaults_and_conditional_diagnostics(self) -> None:
        client = web_console.app.test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for element_id in (
            "cacheSessions",
            "cacheRounds",
            "cacheContentProfile",
            "cacheToolStage",
            "cacheDiagnosticScenario",
            "cacheStageRows",
            "cacheTrustMetrics",
            "cacheEffectivePlan",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertNotIn('id="cacheScenario"', html)

        config_response = client.get("/api/config")
        self.assertEqual(config_response.status_code, 200)
        cache_config = config_response.get_json()["cache_test"]
        self.assertEqual(cache_config["scenario"], "progressive_customer_session")
        self.assertEqual(cache_config["sessions"], 10)
        self.assertEqual(cache_config["rounds_per_session"], 4)
        self.assertIn("kilocode_agent_session", cache_config["diagnostic_defaults"])

        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "static" / "web_console.js"
        ).read_text(encoding="utf-8")
        self.assertIn('scenario: "progressive_customer_session"', script)
        self.assertIn("function cacheRequestEstimate()", script)
        self.assertIn("structural_hit_rate_ceiling", script)
        self.assertIn("actual_cache_hit_rate", script)
        self.assertIn("cache_efficiency", script)
        self.assertIn("progressive_prefix_reuse_rate", script)
        self.assertIn("cache_stage_metrics", script)

    def test_load_result_scan_excludes_step_results_covered_by_root_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs_root = Path(temp_dir)
            report_dir = jobs_root / "finished-staircase"
            nested_dir = report_dir / "step_10" / "measure"
            nested_dir.mkdir(parents=True)
            (nested_dir / "request_records.jsonl").touch()

            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {
                "finished-staircase": Job(
                    id="finished-staircase",
                    type="staircase",
                    provider="provider-a",
                    provider_label="Provider A",
                    model="model-a",
                    model_family="gpt",
                    workload="throughput",
                    users=300,
                    spawn_rate=5,
                    duration="5m",
                    report_dir=report_dir,
                    command=[],
                    created_at=10,
                    finished_at=20,
                    status="completed",
                    returncode=0,
                )
            }
            root_result = {
                "id": "jobs/finished-staircase",
                "created_at": 10,
                "summary": {},
            }

            with (
                patch.object(web_console, "JOBS_ROOT", jobs_root),
                patch.object(web_console, "JOB_MANAGER", manager),
                patch.object(web_console, "_ensure_load_result", return_value=root_result),
                patch.object(web_console, "_ensure_load_result_for_dir") as ensure_nested,
            ):
                results = web_console._list_load_results()

            self.assertEqual([item["id"] for item in results], ["jobs/finished-staircase"])
            ensure_nested.assert_not_called()

    def test_cache_measured_requests_defaults_and_validates_range(self) -> None:
        config = {"cache_test": {"measured_requests": 50}}

        self.assertEqual(_resolve_cache_measured_requests(config, None), 50)
        self.assertEqual(_resolve_cache_measured_requests(config, "75"), 75)
        with self.assertRaises(ValueError):
            _resolve_cache_measured_requests(config, 0)
        with self.assertRaises(ValueError):
            _resolve_cache_measured_requests(config, 10001)

    def test_current_refs_prefers_active_job_and_reports_newest_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {}
            root = Path(temp_dir)

            def add_job(job_id: str, *, status: str, created_at: float) -> None:
                report_dir = root / job_id
                report_dir.mkdir()
                manager._jobs[job_id] = Job(
                    id=job_id,
                    type="quick_load",
                    provider="provider-a",
                    provider_label="Provider A",
                    model="model-a",
                    model_family="gpt",
                    workload="throughput",
                    users=10,
                    spawn_rate=2,
                    duration="2m",
                    report_dir=report_dir,
                    command=[],
                    created_at=created_at,
                    status=status,
                )

            add_job("older-completed", status="completed", created_at=10)
            add_job("active", status="running", created_at=20)
            add_job("newest-failed", status="failed", created_at=30)

            refs = manager.current_refs()

            self.assertEqual(refs["active"]["id"], "active")
            self.assertEqual(refs["newest"]["id"], "newest-failed")

    def test_latest_finished_matching_result_excludes_running_and_other_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {}
            root = Path(temp_dir)

            def add_job(
                job_id: str,
                *,
                model: str = "model-a",
                status: str,
                finished_at: float | None,
                created_at: float,
                api_form: str = "openai_chat_completions",
                route_profile: str = "vendor_direct",
            ) -> None:
                report_dir = root / job_id
                report_dir.mkdir()
                manager._jobs[job_id] = Job(
                    id=job_id,
                    type="param_test",
                    provider="provider-a",
                    provider_label="Provider A",
                    model=model,
                    model_family="gpt",
                    workload="throughput",
                    users=None,
                    spawn_rate=None,
                    duration=None,
                    report_dir=report_dir,
                    command=[],
                    reference_source="reference-a",
                    reference_label="Reference A",
                    api_form=api_form,
                    route_profile=route_profile,
                    model_profile_id=(
                        f"gpt/{model}@{route_profile}/{api_form}"
                    ),
                    created_at=created_at,
                    finished_at=finished_at,
                    status=status,
                    returncode=0 if status == "completed" else 1,
                )

            add_job("older", status="completed", finished_at=20, created_at=10)
            add_job("latest", status="failed", finished_at=40, created_at=30)
            add_job("running", status="running", finished_at=None, created_at=50)
            add_job(
                "other-model",
                model="model-b",
                status="completed",
                finished_at=60,
                created_at=55,
            )
            add_job(
                "other-api-form",
                status="completed",
                finished_at=70,
                created_at=65,
                api_form="openai_responses",
            )
            add_job(
                "other-route",
                status="completed",
                finished_at=80,
                created_at=75,
                route_profile="cloud_adapter",
            )

            result = manager.latest_param_result(
                "provider-a",
                "model-a",
                "vendor_direct",
                "openai_chat_completions",
                "gpt/model-a@vendor_direct/openai_chat_completions",
                "reference-a",
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["id"], "latest")


if __name__ == "__main__":
    unittest.main()
