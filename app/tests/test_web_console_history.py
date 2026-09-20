from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.web_console as web_console
from scripts.web_console import (
    Job,
    JobManager,
    _resolve_cache_measured_requests,
    _select_interface_contract_id,
)


class ParamHistoryTest(unittest.TestCase):
    @staticmethod
    def _vendor_alias_config() -> dict:
        config = copy.deepcopy(web_console.load_config())
        provider = config["providers"]["yibu"]
        models = provider["models"]
        alias = "vendor-deepseek-v4"
        canonical = "deepseek-v4-pro"
        provider["reference_source_id"] = "aliyun_maas"
        models["default"] = alias
        models["candidates"] = [alias]
        models["families"][alias] = "deepseek"
        models["routes"][alias] = copy.deepcopy(models["routes"][canonical])
        models["default_routes"][alias] = models["default_routes"][canonical]
        models["default_api_forms"][alias] = copy.deepcopy(
            models["default_api_forms"][canonical]
        )
        models["reference_model_ids"] = {alias: canonical}
        return config

    def test_cache_tool_gate_is_leaf_scoped_and_basic_scenarios_remain_runnable(
        self,
    ) -> None:
        client = web_console.app.test_client()
        response = client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        leaves = [
            leaf
            for models in response.get_json()["model_capabilities"].values()
            for model in models.values()
            for route in (model.get("routes") or {}).values()
            for leaf in (route.get("api_forms") or {}).values()
        ]
        self.assertTrue(leaves)
        for leaf in leaves:
            self.assertIsInstance(leaf["cache_tool_runnable"], bool)
            if leaf["cache_tool_runnable"]:
                self.assertTrue(leaf["cache_tool_profile"])

        unsupported_policy = {
            "pressure_profiles": {"cache_profiles": ["cache_long_context"]}
        }
        supported_policy = {
            "pressure_profiles": {"compatibility_profiles": ["tool_calls"]}
        }
        basic_plans = (
            {"scenario": "shared_prefix"},
            {"scenario": "growing_conversation"},
            {
                "scenario": "progressive_customer_session",
                "tool_stage": {"enabled": False},
            },
        )
        for plan in basic_plans:
            web_console._require_cache_plan_tool_capability(
                plan, unsupported_policy, "deepseek", "chat_completions"
            )
        for plan in (
            {
                "scenario": "progressive_customer_session",
                "tool_stage": {"enabled": True},
            },
            {"scenario": "kilocode_agent_session"},
        ):
            with self.assertRaisesRegex(ValueError, "MPDB-approved tool profile"):
                web_console._require_cache_plan_tool_capability(
                    plan, unsupported_policy, "deepseek", "chat_completions"
                )
            web_console._require_cache_plan_tool_capability(
                plan, supported_policy, "deepseek", "chat_completions"
            )

    def test_cache_job_post_rejects_tool_scenario_before_queueing(self) -> None:
        client = web_console.app.test_client()
        registry = client.get("/api/config").get_json()["model_capabilities"]
        provider, model, route, api_form = next(
            (provider, model, route, api_form)
            for provider, models in registry.items()
            for model, model_row in models.items()
            for route, route_row in (model_row.get("routes") or {}).items()
            for api_form, leaf in (route_row.get("api_forms") or {}).items()
            if leaf.get("pressure_test_runnable") is True
        )
        payload = {
            "type": "cache_suite",
            "provider": provider,
            "model": model,
            "route_profile": route,
            "api_form": api_form,
            "cache_plan": {
                "scenario": "kilocode_agent_session",
                "steps": 2,
                "controls": {
                    "positive_long_prefix_pairs": 1,
                    "negative_unique_prefix_requests": 1,
                },
                "max_run_seconds": 60,
                "consecutive_failure_limit": 1,
                "evidence_mode": "official_usage",
            },
        }
        manager = JobManager.__new__(JobManager)
        with (
            patch.object(web_console, "JOB_MANAGER", manager),
            patch.object(
                web_console,
                "approved_cache_tool_profile",
                side_effect=ValueError("test policy has no approved tools"),
            ),
        ):
            response = client.post("/api/jobs", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "requires an MPDB-approved tool profile",
            response.get_json()["error"],
        )

    def test_cache_route_and_api_form_are_leaf_scoped_and_submitted(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        html = (project_root / "scripts/templates/web_console.html").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            html.index('id="cacheRouteProfile"'),
            html.index('id="cacheApiForm"'),
        )
        script = (
            project_root
            / "scripts"
            / "static"
            / "web_console.js"
        ).read_text(encoding="utf-8")
        route_handler = script.split(
            '$("cacheRouteProfile").addEventListener("change"', 1
        )[1].split('$("cacheApiForm").addEventListener("change"', 1)[0]
        self.assertIn("form.apiForm = cacheApiFormForModel", route_handler)
        cache_payload = script.split("function jobPayload(type)", 1)[1].split(
            'if (type === "cache_suite")', 1
        )[1].split("const form = appState.formsByTab.load", 1)[0]
        self.assertIn("route_profile: form.routeProfile", cache_payload)
        self.assertIn("api_form: form.apiForm", cache_payload)
        self.assertIn("function cacheLeafRunnable(capability)", script)
        self.assertIn('capability.profile_status === "registered"', script)
        self.assertIn("capability.pressure_test_runnable === true", script)
        self.assertIn("return runnable.length ? runnable : models;", script)
        self.assertIn("return Object.keys(runnable).length ? runnable : routes;", script)
        self.assertIn("return Object.keys(runnable).length ? runnable : forms;", script)
        self.assertIn("loadCapability.pressure_test_runnable !== true", script)
        self.assertIn("cacheCapability.pressure_test_runnable !== true", script)
        self.assertNotIn("loadCapability.pressure_test_enabled !== true", script)
        self.assertNotIn("cacheCapability.pressure_test_enabled !== true", script)

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
        self.assertIn('form.referenceContractId = "";', route_handler)
        self.assertIn('appState.paramHistoryResult = null;', route_handler)
        self.assertIn('id="referenceContractId"', html)
        self.assertIn("appState.config.reference_contracts", script)
        self.assertIn("contract_id: contractId", script)
        self.assertIn("reference_contract_id: form.referenceContractId", script)
        self.assertNotIn("reference_source: contractId", script)
        self.assertNotIn("reference_source: form.referenceContractId", script)

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
                self.assertEqual(payload["contract_id"], expected_source)
                self.assertEqual(payload["reference_source"], expected_source)
                self.assertFalse(payload["legacy_query_alias_used"])
                self.assertEqual(
                    payload["deprecated_aliases"]["reference_source"],
                    {
                        "replacement": "contract_id",
                        "semantic_type": "InterfaceContract",
                        "deprecated": True,
                        "legacy": True,
                    },
                )
                self.assertNotIn("model_capability_profile", payload)

        legacy_family = client.get("/api/param-specs?family=openai")
        self.assertEqual(legacy_family.status_code, 400)
        self.assertIn("No reference source", legacy_family.get_json()["error"])

    def test_param_specs_contract_query_and_deprecated_alias_are_unambiguous(self) -> None:
        client = web_console.app.test_client()
        canonical = client.get(
            "/api/param-specs?family=gpt&contract_id=openai_chat_base"
        )
        self.assertEqual(canonical.status_code, 200)
        canonical_payload = canonical.get_json()
        self.assertEqual(canonical_payload["contract_id"], "openai_chat_base")
        self.assertEqual(
            canonical_payload["reference_source"], canonical_payload["contract_id"]
        )
        self.assertFalse(canonical_payload["legacy_query_alias_used"])

        legacy = client.get(
            "/api/param-specs?family=gpt&reference_source=openai_chat_base"
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.get_json()["legacy_query_alias_used"])

        equal_aliases = client.get(
            "/api/param-specs?family=gpt&contract_id=openai_chat_base"
            "&reference_source=openai_chat_base"
        )
        self.assertEqual(equal_aliases.status_code, 200)
        self.assertTrue(equal_aliases.get_json()["legacy_query_alias_used"])

        conflict = client.get(
            "/api/param-specs?family=gpt&contract_id=openai_chat_base"
            "&reference_source=openai_gpt5_chat"
        )
        self.assertEqual(conflict.status_code, 400)
        self.assertIn("must be equal", conflict.get_json()["error"])

    def test_config_exposes_contract_first_fields_and_equal_deprecated_aliases(self) -> None:
        payload = web_console.app.test_client().get("/api/config").get_json()
        self.assertEqual(
            payload["default_reference_source"],
            payload["default_reference_contract_id"],
        )
        self.assertEqual(payload["reference_sources"], payload["reference_contracts"])
        self.assertTrue(payload["reference_contracts"])
        self.assertTrue(
            all(
                row["contract_id"] == row["id"]
                and row["semantic_type"] == "InterfaceContract"
                for row in payload["reference_contracts"]
            )
        )
        self.assertEqual(
            payload["deprecated_aliases"],
            {
                "default_reference_source": {
                    "replacement": "default_reference_contract_id",
                    "semantic_type": "InterfaceContract",
                    "deprecated": True,
                    "legacy": True,
                },
                "reference_sources": {
                    "replacement": "reference_contracts",
                    "semantic_type": "InterfaceContract",
                    "deprecated": True,
                    "legacy": True,
                },
            },
        )
        checked = 0
        for models in payload["model_capabilities"].values():
            for model in models.values():
                for route in (model.get("routes") or {}).values():
                    for form in (route.get("api_forms") or {}).values():
                        self.assertEqual(
                            form["reference_source"],
                            form["default_reference_contract_id"],
                        )
                        self.assertEqual(
                            form["reference_sources"],
                            form["reference_contract_ids"],
                        )
                        self.assertEqual(
                            form["deprecated_aliases"]["reference_source"][
                                "semantic_type"
                            ],
                            "InterfaceContract",
                        )
                        checked += 1
        self.assertGreater(checked, 0)

    def test_vendor_alias_console_and_job_share_one_source_local_snapshot(self) -> None:
        config = self._vendor_alias_config()
        alias = "vendor-deepseek-v4"
        contract = "aliyun_deepseek_v4_openai_compat"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            web_console, "JOBS_ROOT", Path(temp_dir)
        ), patch.object(
            web_console, "load_config", return_value=config
        ), patch.object(
            web_console, "provider_has_api_key", return_value=True
        ), patch.object(
            JobManager, "_load_finished_jobs", return_value=None
        ), patch.object(
            JobManager, "_start_locked", return_value=None
        ):
            config_response = web_console.app.test_client().get("/api/config")
            self.assertEqual(config_response.status_code, 200)
            model_row = config_response.get_json()["model_capabilities"]["yibu"][alias]
            self.assertEqual(model_row["default_reference_contract_id"], contract)
            self.assertEqual(
                model_row["profile_id"],
                "text/aliyun_maas/deepseek/deepseek-v4-pro#openai-chat-default",
            )

            spec_response = web_console.app.test_client().get(
                "/api/param-specs?provider=yibu"
                f"&model={alias}"
                "&route_profile=dynamic_aggregator"
                "&api_form=openai_chat_completions"
            )
            self.assertEqual(spec_response.status_code, 200)
            spec = spec_response.get_json()
            self.assertEqual(spec["contract_id"], contract)
            self.assertEqual(
                spec["model_capability_profile"]["model"], alias
            )

            manager = JobManager()
            job = manager.create(
                {
                    "type": "quick_load",
                    "provider": "yibu",
                    "model": alias,
                    "workload": "throughput_rpm",
                    "target_rpm": 60,
                    "users": 2,
                    "spawn_rate": 1,
                    "duration": "60s",
                }
            )
            database = job.job_spec["model_profile_database"]
            capability = job.job_spec["model_capability_profile"]
            self.assertEqual(job.reference_source, contract)
            self.assertEqual(database["source_id"], "aliyun_maas")
            self.assertEqual(database["execution_target"]["request_model_id"], alias)
            for field in (
                "profile_id",
                "interface_id",
                "test_binding_id",
                "reference_contract_id",
            ):
                self.assertEqual(capability[field], database[field])

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

    def test_cache_public_view_rejects_legacy_snapshotless_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir)
            web_console.write_json(report_dir / "verdict.json", {"pass": True})
            job = Job(
                id="legacy-cache",
                type="cache_suite",
                provider="provider-a",
                provider_label="Provider A",
                model="model-a",
                model_family="gpt",
                workload="cache_suite",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=report_dir,
                command=[],
                job_spec={
                    "schema_version": 3,
                    "type": "cache_suite",
                },
                status="completed",
                returncode=0,
            )

            with patch.object(web_console, "REPORTS_ROOT", report_dir):
                payload = JobManager.__new__(JobManager).public(
                    job,
                    include_detail=False,
                )

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["returncode"], 1)
        self.assertEqual(
            payload["result_validation"]["status"],
            "legacy_unverified",
        )
        self.assertFalse(payload["result_validation"]["pass"])

    def test_cache_restore_keeps_invalid_schema4_snapshot_as_failed_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir) / "finished_cache_suite_invalid"
            report_dir.mkdir()
            web_console.write_json(
                report_dir / "job_spec.json",
                {
                    "schema_version": 4,
                    "type": "cache_suite",
                    "provider": "provider-a",
                    "model": "model-a",
                    "model_family": "gpt",
                    "workload": "cache_suite",
                    "cache_plan": {"scenario": "shared_prefix"},
                    "model_profile_database": {
                        "snapshot_schema_version": 1,
                        "snapshot_digest": "invalid",
                    },
                },
            )
            web_console.write_json(report_dir / "verdict.json", {"pass": True})
            manager = JobManager.__new__(JobManager)
            manager._lock = threading.Lock()
            manager._jobs = {}

            manager._load_finished_job(
                {"cache_test": {"measured_requests": 1}},
                report_dir,
            )

            restored = manager._jobs[report_dir.name]
            self.assertEqual(restored.status, "failed")
            self.assertEqual(restored.returncode, 1)
            validation = web_console._result_validation_for_job(restored)
            self.assertEqual(validation["status"], "legacy_unverified")
            self.assertFalse(validation["pass"])

    def test_execution_contract_selector_is_canonical_and_fail_closed(self) -> None:
        selected, legacy_used = _select_interface_contract_id(
            "openai_chat_base",
            None,
        )
        self.assertEqual(selected, "openai_chat_base")
        self.assertFalse(legacy_used)

        selected, legacy_used = _select_interface_contract_id(
            None,
            "openai_chat_base",
        )
        self.assertEqual(selected, "openai_chat_base")
        self.assertTrue(legacy_used)

        with self.assertRaisesRegex(ValueError, "must be equal"):
            _select_interface_contract_id(
                "openai_chat_base",
                "openai_gpt5_chat",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            job = Job(
                id="contract-first",
                type="param_test",
                provider="provider-a",
                provider_label="Provider A",
                model="gpt-4o",
                model_family="gpt",
                workload="param_test",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=Path(temp_dir),
                command=[],
                reference_source="openai_chat_base",
            )
            payload = JobManager.__new__(JobManager).public(
                job, include_detail=False
            )
        self.assertEqual(payload["reference_contract_id"], "openai_chat_base")
        self.assertEqual(
            payload["reference_source"], payload["reference_contract_id"]
        )
        self.assertEqual(
            payload["deprecated_aliases"]["reference_source"]["replacement"],
            "reference_contract_id",
        )

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

            # This unit test constructs an isolated in-memory manager.  Do not
            # let unrelated job specs under the process-global JOBS_ROOT alter
            # its ordering assertions.
            with patch.object(manager, "_discover_external_jobs"):
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
            self.assertEqual(
                result["result_validation"]["status"],
                "legacy_unverified",
            )
            self.assertFalse(result["result_validation"]["pass"])
            self.assertEqual(result["status"], "failed")

    def test_progress_counts_use_overall_token_validated_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            param_job = Job(
                id="param",
                type="param_test",
                provider="provider-a",
                provider_label="Provider A",
                model="model-a",
                model_family="gpt",
                workload="parameters",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=root,
                command=[],
            )
            detail = web_console._progress_detail(
                param_job,
                None,
                None,
                [
                    {"status": "pass", "pass": True, "overall_pass": True},
                    {"status": "pass", "pass": True, "overall_pass": False},
                ],
            )
            self.assertIn("passed 1", detail)
            self.assertIn("failed 1", detail)

            image_job = Job(
                id="image",
                type="image_param_test",
                provider="provider-a",
                provider_label="Provider A",
                model="image-a",
                model_family="image",
                workload="image_param",
                users=None,
                spawn_rate=None,
                duration=None,
                report_dir=root,
                command=[],
                image_plan={"cases": ["pass", "token-fail"]},
                status="failed",
            )
            progress = web_console._image_job_progress(
                image_job,
                {"cases": ["pass", "token-fail"]},
                [
                    {"case": "pass", "pass": True, "overall_pass": True},
                    {
                        "case": "token-fail",
                        "pass": True,
                        "overall_pass": False,
                    },
                ],
                None,
            )
            self.assertEqual(progress["pass_count"], 1)
            self.assertEqual(progress["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
