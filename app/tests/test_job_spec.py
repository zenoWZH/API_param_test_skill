from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lib.config import (
    get_image_model_config,
    get_model_api_form,
    get_model_family,
    get_model_reference_source,
    get_model_route_profile,
    get_model_transport,
    get_provider_interface,
    load_config,
    resolve_threshold_config,
    validate_provider_config,
)
from lib.job_spec import (
    CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION,
    JOB_SPEC_VERSION,
    load_job_spec,
    make_job_spec,
    resolve_cache_plan,
    resolve_image_plan,
    resolve_staircase_plan,
    validate_workload,
)
from lib.model_profile_catalog import (
    capability_profile_from_database_snapshot,
    resolve_runtime_parameter_config,
)
from scripts.web_console import JobManager
from scripts.run_staircase import run_locust


class ProviderRoutingContractTest(unittest.TestCase):
    def test_model_reference_source_is_explicit_and_validated(self) -> None:
        config = copy.deepcopy(load_config())
        provider = config["providers"]["yibu"]
        model = provider["models"]["candidates"][0]
        route = provider["models"]["default_routes"][model]
        api_form = provider["models"]["default_api_forms"][model][route]
        provider["models"]["routes"][model][route]["api_forms"][api_form][
            "reference_source"
        ] = "deepseek_dynamic_aggregator"

        validate_provider_config(config)
        self.assertEqual(
            get_model_reference_source(config, model, "yibu"),
            "deepseek_dynamic_aggregator",
        )

        provider["models"]["routes"]["not-configured"] = copy.deepcopy(
            provider["models"]["routes"][model]
        )
        with self.assertRaisesRegex(ValueError, "contains unknown models"):
            validate_provider_config(config)

    def test_every_configured_model_has_an_explicit_resolvable_transport(self) -> None:
        config = load_config()
        checked = 0
        for provider, provider_cfg in config["providers"].items():
            models = provider_cfg.get("models") or {}
            candidates = list(models.get("candidates") or [])
            if models.get("default") and models["default"] not in candidates:
                candidates.append(models["default"])
            for model in candidates:
                with self.subTest(provider=provider, model=model):
                    transport = get_model_transport(config, str(model), provider)
                    interface = get_provider_interface(config, transport, provider)
                    self.assertTrue(interface["base_url"])
                    self.assertTrue(str(interface["path"]).startswith("/"))
                    self.assertIn(interface["auth"], {"bearer", "anthropic", "google_api_key"})
                    checked += 1
        self.assertGreater(checked, 0)

    def test_provider_validation_rejects_incomplete_model_family_coverage(self) -> None:
        config = copy.deepcopy(load_config())
        provider = config["providers"]["yibu"]
        missing_model = provider["models"]["candidates"][0]
        provider["models"]["families"].pop(missing_model, None)

        with self.assertRaisesRegex(ValueError, "families is missing models"):
            validate_provider_config(config)


class JobSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def _text_database(self, provider: str, model: str) -> dict[str, object]:
        family = get_model_family(self.config, model, provider)
        route = get_model_route_profile(self.config, model, provider)
        api_form = get_model_api_form(
            self.config, model, provider, route_profile=route
        )
        return resolve_runtime_parameter_config(
            self.config,
            provider,
            model,
            family,
            route,
            api_form,
        )["model_profile_database"]

    def _image_database(self, provider: str, model: str) -> dict[str, object]:
        target = get_image_model_config(self.config, provider, model)
        return resolve_runtime_parameter_config(
            self.config,
            provider,
            model,
            str(target["family"]),
            str(target["route_profile"]),
            str(target["api_form"]),
            modality="image",
        )["model_profile_database"]

    def test_text_entrypoints_reject_unregistered_route_before_execution(self) -> None:
        with patch(
            "scripts.web_console.load_config", return_value=self.config
        ), patch.object(JobManager, "_load_finished_jobs", return_value=None):
            manager = JobManager()
            for job_type in ("param_test", "cache_suite", "quick_load", "staircase"):
                with self.subTest(job_type=job_type), self.assertRaisesRegex(
                    ValueError,
                    "does not expose route profile 'unregistered-route'",
                ):
                    manager.create(
                        {
                            "type": job_type,
                            "provider": "yibu",
                            "model": "deepseek-v4-pro",
                            "route_profile": "unregistered-route",
                        }
                    )

    def test_new_job_specs_record_modality_and_reject_media_pressure_snapshots(self) -> None:
        common = {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "workload": "throughput",
            "request_mode": "fixed",
            "target_rpm": 0.0,
            "target_tpm": 0.0,
        }
        text = make_job_spec(
            job_type="quick_load",
            model_profile_database=self._text_database(
                "gemini", "gemini-2.5-flash"
            ),
            model_capability_profile={
                "modality": "text",
                "pressure_test_enabled": True,
            },
            **common,
        )
        image = make_job_spec(
            job_type="image_param_test",
            provider="gemini",
            model="gemini-3.1-flash-image",
            model_profile_database=self._image_database(
                "gemini", "gemini-3.1-flash-image"
            ),
            model_capability_profile={
                "modality": "image",
                "pressure_test_enabled": False,
                "test_policy_pressure_test_enabled": False,
            },
            **{
                key: value
                for key, value in common.items()
                if key not in {"provider", "model"}
            },
        )
        self.assertEqual(text["schema_version"], 4)
        self.assertEqual(text["modality"], "text")
        self.assertEqual(image["modality"], "image")

        for modality in ("image", "video"):
            with self.subTest(modality=modality), self.assertRaisesRegex(
                ValueError,
                "modality='text'",
            ):
                make_job_spec(
                    job_type="quick_load",
                    model_profile_database=self._text_database(
                        "gemini", "gemini-2.5-flash"
                    ),
                    model_capability_profile={
                        "modality": modality,
                        "pressure_test_enabled": False,
                        "test_policy_pressure_test_enabled": False,
                    },
                    **common,
                )

    def test_job_spec_rejects_capability_identity_mixed_with_another_snapshot(self) -> None:
        database = self._text_database("yibu", "deepseek-v4-pro")
        capability = capability_profile_from_database_snapshot(database)
        capability["interface_id"] = "text/other/source/model#chat"

        with self.assertRaisesRegex(
            ValueError,
            "Job capability conflicts with immutable MPDB snapshot: interface_id",
        ):
            make_job_spec(
                job_type="param_test",
                provider="yibu",
                model="deepseek-v4-pro",
                workload="param_test",
                request_mode="fixed",
                target_rpm=0.0,
                target_tpm=0.0,
                model_profile_database=database,
                model_capability_profile=capability,
            )

    def test_job_spec_rejects_family_transport_and_capability_snapshot_drift(self) -> None:
        database = self._text_database("yibu", "deepseek-v4-pro")
        capability = capability_profile_from_database_snapshot(database)
        common = {
            "job_type": "param_test",
            "provider": "yibu",
            "model": "deepseek-v4-pro",
            "workload": "param_test",
            "request_mode": "fixed",
            "target_rpm": 0.0,
            "target_tpm": 0.0,
            "model_profile_database": database,
            "model_capability_profile": capability,
        }
        for field, value in (
            ("model_family", "wrong-family"),
            ("transport", "gemini_generate_content"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, field
            ):
                make_job_spec(**common, **{field: value})

        for field, value in (
            ("family", "wrong-family"),
            ("modality", "image"),
            ("parameter_test_binding_id", "parameter/wrong-contract"),
        ):
            mismatched = copy.deepcopy(capability)
            mismatched[field] = value
            with self.subTest(capability_field=field), self.assertRaisesRegex(
                ValueError, field
            ):
                make_job_spec(
                    **{
                        **common,
                        "model_capability_profile": mismatched,
                    }
                )

        nested_cases = (
            (("reference_identity", "source_id"), "other-source"),
            (("execution_target", "request_model_id"), "other-model"),
            (
                ("test_binding", "pressure_test_enabled"),
                not bool(capability["test_binding"]["pressure_test_enabled"]),
            ),
            (("reference_contract", "contract_id"), "other-contract"),
            (
                ("parameter_test_binding", "test_binding_id"),
                "parameter/other-contract",
            ),
        )
        for path, value in nested_cases:
            mismatched = copy.deepcopy(capability)
            mismatched[path[0]][path[1]] = value
            with self.subTest(capability_path=path), self.assertRaisesRegex(
                ValueError, path[0]
            ):
                make_job_spec(
                    **{
                        **common,
                        "model_capability_profile": mismatched,
                    }
                )

        valid = make_job_spec(**common)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"
            persisted_cases = (
                (("family",), "wrong-family"),
                *nested_cases,
            )
            for field_path, value in persisted_cases:
                tampered = copy.deepcopy(valid)
                cursor = tampered["model_capability_profile"]
                for part in field_path[:-1]:
                    cursor = cursor[part]
                cursor[field_path[-1]] = value
                path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.subTest(
                    persisted_capability_path=field_path
                ), self.assertRaisesRegex(RuntimeError, field_path[0]):
                    load_job_spec(path)

    def test_image_job_rejects_a_plan_from_another_model_snapshot(self) -> None:
        first_model = "gemini-3.1-flash-image"
        second_model = "gemini-2.5-flash-image"
        first_plan = resolve_image_plan(
            self.config,
            {"image_plan": {"suite": "smoke", "no_cross_control": True}},
            "gemini",
            first_model,
            60,
        )
        second_plan = resolve_image_plan(
            self.config,
            {"image_plan": {"suite": "smoke", "no_cross_control": True}},
            "gemini",
            second_model,
            60,
        )
        common = {
            "job_type": "image_param_test",
            "provider": "gemini",
            "model": first_model,
            "workload": "image_param",
            "request_mode": "fixed",
            "target_rpm": 0.0,
            "target_tpm": 0.0,
            "model_family": first_plan["family"],
            "api_form": first_plan["api_form"],
            "route_profile": first_plan["route_profile"],
            "transport": first_plan["transport"],
            "model_profile_database": first_plan["model_profile_database"],
            "model_capability_profile": first_plan["model_capability_profile"],
        }

        with self.assertRaisesRegex(ValueError, "Job image_plan conflicts"):
            make_job_spec(**common, image_plan=second_plan)

        valid = make_job_spec(**common, image_plan=first_plan)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"
            tampered = copy.deepcopy(valid)
            tampered["image_plan"] = second_plan
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Job image_plan conflicts"):
                load_job_spec(path)

            for field_path, value in (
                (("reference_identity", "source_id"), "other-source"),
                (
                    ("execution_target", "request_model_id"),
                    "other-image-model",
                ),
            ):
                tampered = copy.deepcopy(valid)
                # make_job_spec may retain the same immutable capability object
                # at the top level and under image_plan. Break that in-memory
                # alias so this case specifically exercises the nested guard.
                tampered["image_plan"]["model_capability_profile"] = copy.deepcopy(
                    tampered["image_plan"]["model_capability_profile"]
                )
                cursor = tampered["image_plan"]["model_capability_profile"]
                for part in field_path[:-1]:
                    cursor = cursor[part]
                cursor[field_path[-1]] = value
                path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.subTest(
                    persisted_image_capability_path=field_path
                ), self.assertRaisesRegex(RuntimeError, "Job image_plan conflicts"):
                    load_job_spec(path)

    def test_schema_v4_snapshot_views_must_be_nonempty_and_equal(self) -> None:
        provider = "gemini"
        model = "gemini-2.5-flash"
        snapshot = self._text_database(provider, model)
        payload = make_job_spec(
            job_type="param_test",
            provider=provider,
            model=model,
            model_profile_database=snapshot,
            reference_contract_id=str(snapshot["reference_contract_id"]),
            workload="parameters",
            request_mode="fixed",
            target_rpm=0,
            target_tpm=0,
            model_capability_profile={"modality": "text"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"

            equal_views = copy.deepcopy(payload)
            equal_views["model_capability_profile"][
                "model_profile_database"
            ] = copy.deepcopy(snapshot)
            path.write_text(json.dumps(equal_views), encoding="utf-8")
            self.assertEqual(load_job_spec(path)["model"], model)

            malformed_top = copy.deepcopy(equal_views)
            malformed_top["model_profile_database"] = {}
            path.write_text(json.dumps(malformed_top), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "empty or malformed snapshot"):
                load_job_spec(path)

            malformed_nested = copy.deepcopy(payload)
            malformed_nested["model_capability_profile"][
                "model_profile_database"
            ] = []
            path.write_text(json.dumps(malformed_nested), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "empty or malformed snapshot"):
                load_job_spec(path)

            conflicting = copy.deepcopy(equal_views)
            conflicting["model_capability_profile"]["model_profile_database"][
                "runtime_model_id"
            ] = "another-model"
            path.write_text(json.dumps(conflicting), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "snapshots conflict"):
                load_job_spec(path)

    def test_schema_v4_executable_jobs_require_current_snapshot_schema(self) -> None:
        provider = "gemini"
        model = "gemini-2.5-flash"
        family = get_model_family(self.config, model, provider)
        route = get_model_route_profile(self.config, model, provider)
        api_form = get_model_api_form(
            self.config,
            model,
            provider,
            route_profile=route,
        )
        snapshot = self._text_database(provider, model)
        common = {
            "job_type": "param_test",
            "provider": provider,
            "model": model,
            "model_family": family,
            "api_form": api_form,
            "route_profile": route,
            "reference_contract_id": str(snapshot["reference_contract_id"]),
            "workload": "parameters",
            "request_mode": "fixed",
            "target_rpm": 0,
            "target_tpm": 0,
        }
        valid = make_job_spec(model_profile_database=snapshot, **common)
        invalid_versions = (
            ("missing", None),
            ("boolean", True),
            ("legacy", 0),
            ("future", CURRENT_MPDB_SNAPSHOT_SCHEMA_VERSION + 1),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"
            for label, version in invalid_versions:
                invalid_snapshot = copy.deepcopy(snapshot)
                if version is None:
                    invalid_snapshot.pop("snapshot_schema_version")
                else:
                    invalid_snapshot["snapshot_schema_version"] = version
                with self.subTest(builder=label), self.assertRaisesRegex(
                    ValueError,
                    "requires MPDB snapshot schema 1",
                ):
                    make_job_spec(
                        model_profile_database=invalid_snapshot,
                        **common,
                    )

                invalid_job = copy.deepcopy(valid)
                invalid_job["model_profile_database"] = invalid_snapshot
                path.write_text(json.dumps(invalid_job), encoding="utf-8")
                with self.subTest(loader=label), self.assertRaisesRegex(
                    RuntimeError,
                    "requires MPDB snapshot schema 1",
                ):
                    load_job_spec(path)

        trace = make_job_spec(
            job_type="trace_test",
            provider="provider",
            model="model",
            model_family="family",
            api_form="openai_chat_completions",
            route_profile="provider_compat",
            workload="trace",
            request_mode="fixed",
            target_rpm=0,
            target_tpm=0,
        )
        self.assertEqual(trace["schema_version"], JOB_SPEC_VERSION)
        self.assertNotIn("model_profile_database", trace)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trace-job.json"
            path.write_text(json.dumps(trace), encoding="utf-8")
            self.assertEqual(load_job_spec(path)["type"], "trace_test")

    def test_job_spec_loader_rejects_media_pressure_markers_but_reads_legacy(self) -> None:
        pressure_types = ("quick_load", "staircase", "soak", "cache_suite")
        invalid_markers = (
            {"modality": "image"},
            {"interface_id": "video/source/family/model#native"},
            {
                "model_capability_profile": {
                    "modality": "image",
                    "pressure_test_enabled": False,
                    "test_policy_pressure_test_enabled": False,
                }
            },
            {
                "model_profile_database": {
                    "profile_id": "video/source/family/model",
                    "interface_id": "video/source/family/model#native",
                    "interface": {"modality": "video"},
                }
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"
            for schema_version in (1, 3):
                legacy = {
                    "schema_version": schema_version,
                    "type": "quick_load",
                }
                path.write_text(json.dumps(legacy), encoding="utf-8")
                self.assertEqual(load_job_spec(path), legacy)

            legacy_text_policy = {
                "schema_version": 3,
                "type": "quick_load",
                "modality": "text",
                "model_capability_profile": {
                    "modality": "text",
                    "pressure_test_enabled": False,
                    "test_policy_pressure_test_enabled": False,
                },
                "model_profile_database": {
                    "profile_id": "text/source/family/model",
                    "interface_id": "text/source/family/model#chat",
                    "interface": {
                        "modality": "text",
                        "pressure_test_enabled": False,
                    },
                },
            }
            path.write_text(json.dumps(legacy_text_policy), encoding="utf-8")
            self.assertEqual(load_job_spec(path), legacy_text_policy)

            for invalid_modality in (None, "unknown"):
                payload = {
                    "schema_version": 4,
                    "type": "quick_load",
                }
                if invalid_modality is not None:
                    payload["modality"] = invalid_modality
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(invalid_modality=invalid_modality), self.assertRaisesRegex(
                    RuntimeError,
                    "must declare modality='text'",
                ):
                    load_job_spec(path)

            for job_type in pressure_types:
                for marker in invalid_markers:
                    with self.subTest(job_type=job_type, marker=marker):
                        payload = {
                            "schema_version": 4,
                            "type": job_type,
                            "modality": "text",
                            **copy.deepcopy(marker),
                        }
                        path.write_text(json.dumps(payload), encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "Invalid pressure job spec",
                        ):
                            load_job_spec(path)

    def test_staircase_rejects_quick_load_fields_and_keeps_dedicated_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "top-level"):
            resolve_staircase_plan(self.config, {"users": 77})
        plan = resolve_staircase_plan(
            self.config,
            {
                "staircase_plan": {
                    "steps": [1, 2],
                    "step_duration": "15s",
                    "spawn_rate": 1,
                    "warmup": {"enabled": False},
                    "auto_extend": {
                        "enabled": True,
                        "increment_users": 1,
                        "max_users": 3,
                    },
                }
            },
        )
        self.assertEqual(plan["steps"], [1, 2])
        self.assertEqual(plan["step_duration"], "15s")

    def test_threshold_precedence_is_job_model_provider_global(self) -> None:
        config = copy.deepcopy(self.config)
        provider = "yibu"
        model = "deepseek-v4-pro"
        config.setdefault("thresholds", {})["staircase"] = {
            "success_rate_min": 0.50,
            "p95_latency_max_ms": 40000,
        }
        provider_cfg = config["providers"][provider]
        provider_cfg.setdefault("thresholds", {})["staircase"] = {
            "success_rate_min": 0.60,
            "error_429_max_ratio": 0.03,
        }
        provider_cfg.setdefault("models", {}).setdefault("thresholds", {}).setdefault(
            model, {}
        )["staircase"] = {
            "success_rate_min": 0.70,
            "error_5xx_max_ratio": 0.02,
        }
        resolved = resolve_threshold_config(
            config,
            "staircase",
            provider,
            model,
            {"success_rate_min": 0.80},
        )

        self.assertEqual(resolved["success_rate_min"], 0.80)
        self.assertEqual(resolved["p95_latency_max_ms"], 40000)
        self.assertEqual(resolved["error_429_max_ratio"], 0.03)
        self.assertEqual(resolved["error_5xx_max_ratio"], 0.02)

    def test_cache_plan_enforces_confirmation_and_hard_limit(self) -> None:
        payload: dict[str, object] = {
            "cache_plan": {"scenario": "kilocode_agent_session", "steps": 200}
        }
        with self.assertRaisesRegex(ValueError, "confirm_large_run"):
            resolve_cache_plan(self.config, payload)
        payload["confirm_large_run"] = True
        plan = resolve_cache_plan(self.config, payload)
        self.assertGreater(plan["estimated_request_count"], 100)
        with self.assertRaisesRegex(ValueError, "hard limit"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "progressive_customer_session",
                        "sessions": 250,
                        "rounds_per_session": 4,
                        "tool_stage": {"enabled": True, "round": 3},
                    },
                    "confirm_large_run": True,
                },
            )

    def test_progressive_cache_plan_resolves_profiles_controls_and_request_count(self) -> None:
        plan = resolve_cache_plan(
            self.config,
            {
                "cache_plan": {
                    "scenario": "progressive_customer_session",
                    "sessions": 10,
                    "rounds_per_session": 4,
                    "content_profile": "realistic",
                    "tool_stage": {"enabled": True, "round": 3},
                    "controls": {"mode": "auto"},
                }
            },
        )

        self.assertEqual(plan["resolved_content_ranges"]["user_chars"], {"min": 200, "max": 2000})
        self.assertEqual(plan["controls"]["positive_long_prefix_pairs"], 3)
        self.assertEqual(plan["controls"]["negative_unique_prefix_requests"], 3)
        self.assertEqual(plan["estimated_customer_request_count"], 50)
        self.assertEqual(plan["estimated_structure_probe_request_count"], 1)
        self.assertEqual(plan["estimated_control_request_count"], 9)
        self.assertEqual(plan["estimated_request_count"], 60)

    def test_progressive_cache_plan_validates_tool_round_custom_ranges_and_gate_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_stage.round"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "rounds_per_session": 2,
                        "tool_stage": {"enabled": True, "round": 3},
                    }
                },
            )
        custom = resolve_cache_plan(
            self.config,
            {
                "cache_plan": {
                    "content_profile": "custom",
                    "content_ranges": {
                        "user_chars": {"min": 111, "max": 222},
                        "tool_result_chars": {"min": 333, "max": 444},
                    },
                    "tool_stage": {"enabled": False, "round": 3},
                    "controls": {
                        "mode": "custom",
                        "positive_long_prefix_pairs": 1,
                        "negative_unique_prefix_requests": 2,
                    },
                }
            },
        )
        self.assertEqual(custom["resolved_content_ranges"]["user_chars"]["min"], 111)
        self.assertEqual(custom["estimated_request_count"], 45)

        with self.assertRaisesRegex(ValueError, "structure_probe.enabled must remain true"):
            resolve_cache_plan(
                self.config,
                {"cache_plan": {"structure_probe": {"enabled": False}}},
            )

        gated = copy.deepcopy(self.config)
        gated.setdefault("thresholds", {})["cache"] = {
            "mode": "gate",
            "cached_input_token_ratio_min": 0.1,
            "measurement_coverage_min": 0.9,
            "positive_control_cached_ratio_min": 0.5,
            "negative_control_cached_ratio_max": 0.05,
        }
        with self.assertRaisesRegex(ValueError, "require positive and negative controls"):
            resolve_cache_plan(
                gated,
                {"cache_plan": {"controls": {"mode": "off"}}},
            )

        with self.assertRaisesRegex(ValueError, "content_ranges.user_chars.min must be an integer"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "content_profile": "custom",
                        "content_ranges": {
                            "user_chars": {"max": 222},
                            "tool_result_chars": {"min": 333, "max": 444},
                        },
                    }
                },
            )

    def test_kilocode_cache_plan_merges_diagnostic_defaults_and_estimates_requests(self) -> None:
        plan = resolve_cache_plan(
            self.config,
            {"cache_plan": {"scenario": "kilocode_agent_session"}},
        )

        self.assertEqual(plan["scenario"], "kilocode_agent_session")
        self.assertEqual(plan["steps"], 20)
        self.assertEqual(plan["trajectory_mode"], "scripted")
        self.assertEqual(plan["warmup_requests"], 1)
        self.assertEqual(plan["controls"]["positive_long_prefix_pairs"], 3)
        self.assertEqual(plan["controls"]["negative_unique_prefix_requests"], 3)
        self.assertEqual(plan["estimated_request_count"], 30)
        self.assertEqual(plan["thresholds"]["mode"], "gate")
        self.assertEqual(plan["thresholds"]["cached_input_token_ratio_min"], 0.90)
        self.assertEqual(plan["system_prompt_fixture"], "fixtures/kilocode_system_prompt.txt")
        self.assertEqual(plan["tools_fixture"], "fixtures/kilocode_tools.json")
        self.assertNotIn("cases", plan)

    def test_kilocode_cache_plan_validates_steps_trajectory_and_gate_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "steps must be at least 2"):
            resolve_cache_plan(
                self.config,
                {"cache_plan": {"scenario": "kilocode_agent_session", "steps": 1}},
            )
        with self.assertRaisesRegex(ValueError, "trajectory_mode"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "kilocode_agent_session",
                        "trajectory_mode": "bogus",
                    }
                },
            )
        with self.assertRaisesRegex(ValueError, "positive control pairs must be positive"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "kilocode_agent_session",
                        "controls": {
                            "positive_long_prefix_pairs": 0,
                            "negative_unique_prefix_requests": 0,
                        },
                    }
                },
            )
        with self.assertRaisesRegex(ValueError, "tools_fixture"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "kilocode_agent_session",
                        "tools_fixture": "fixtures/does_not_exist.json",
                    }
                },
            )
        with self.assertRaisesRegex(ValueError, "must stay inside the project root"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "kilocode_agent_session",
                        "system_prompt_fixture": "/etc/hostname",
                    }
                },
            )
        with self.assertRaisesRegex(ValueError, "must stay inside the project root"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "scenario": "kilocode_agent_session",
                        "tools_fixture": "../../../etc/hosts",
                    }
                },
            )
        random_plan = resolve_cache_plan(
            self.config,
            {
                "cache_plan": {
                    "scenario": "kilocode_agent_session",
                    "steps": 5,
                    "trajectory_mode": "random",
                    "warmup_requests": 2,
                    "controls": {
                        "positive_long_prefix_pairs": 1,
                        "negative_unique_prefix_requests": 2,
                    },
                }
            },
        )
        self.assertEqual(random_plan["estimated_request_count"], 2 + 5 + 2 + 2)

    def test_legacy_cache_scenarios_also_require_positive_and_negative_controls(self) -> None:
        for scenario in ("growing_conversation", "shared_prefix"):
            with self.subTest(scenario=scenario):
                plan = resolve_cache_plan(
                    self.config,
                    {
                        "cache_plan": {
                            "scenario": scenario,
                            "measured_requests": 5,
                            "warmup_requests": 1,
                        }
                    },
                )
                self.assertEqual(plan["controls"]["positive_long_prefix_pairs"], 3)
                self.assertEqual(plan["controls"]["negative_unique_prefix_requests"], 3)
                self.assertEqual(plan["estimated_request_count"], 15)
                with self.assertRaisesRegex(
                    ValueError, "require positive and negative controls"
                ):
                    resolve_cache_plan(
                        self.config,
                        {
                            "cache_plan": {
                                "scenario": scenario,
                                "controls": {"mode": "off"},
                            }
                        },
                    )

    def test_cache_plan_rejects_removed_customer_tool_flow_and_legacy_cases_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "kilocode_agent_session"):
            resolve_cache_plan(
                self.config,
                {"cache_plan": {"scenario": "customer_tool_flow"}},
            )
        with self.assertRaisesRegex(ValueError, "scenario is required"):
            resolve_cache_plan(
                self.config,
                {
                    "cache_plan": {
                        "cases": {
                            "direct_varying_user": {
                                "sessions": 2,
                                "user_chars": {"min": 200, "max": 2000},
                            }
                        }
                    }
                },
            )

    def test_staircase_and_soak_reject_mixed_compat(self) -> None:
        with self.assertRaisesRegex(ValueError, "deterministic"):
            validate_workload(self.config, "staircase", "mixed_compat")
        with self.assertRaisesRegex(ValueError, "deterministic"):
            validate_workload(self.config, "soak", "mixed_compat")

    def test_staircase_targets_do_not_enable_locust_rate_limiters(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> object:
            captured["env"] = kwargs.get("env")
            return Mock(returncode=0)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"LOADTEST_TARGET_RPM": "999", "LOADTEST_TARGET_TPM": "99999", "YIBU_API_KEY": "test-secret-value-123"},
        ), patch("scripts.run_staircase.subprocess.run", side_effect=fake_run):
            run_locust(
                config=self.config,
                report_dir=Path(temp_dir),
                users=1,
                spawn_rate=1,
                duration="1s",
                workload="throughput_rpm",
                phase="measure",
                staircase_step=1,
                target_rpm=10,
                target_tpm=1000,
                target_tokens_per_request=100,
            )

        env = captured["env"]
        self.assertIsInstance(env, dict)
        assert isinstance(env, dict)
        self.assertNotIn("LOADTEST_TARGET_RPM", env)
        self.assertNotIn("LOADTEST_TARGET_TPM", env)
        self.assertEqual(env["LOADTEST_TARGET_TOKENS_PER_REQUEST"], "100")

    def test_job_manager_writes_secret_free_effective_staircase_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "scripts.web_console.JOBS_ROOT", Path(temp_dir)
        ), patch("scripts.web_console.provider_has_api_key", return_value=True), patch.object(
            JobManager, "_load_finished_jobs", return_value=None
        ), patch.object(JobManager, "_start_locked", return_value=None):
            manager = JobManager()
            job = manager.create(
                {
                    "type": "staircase",
                    "provider": "gemini",
                    "model": "gemini-2.5-flash",
                    "workload": "throughput_rpm",
                    "request_mode": "unique",
                    "staircase_plan": {
                        "steps": [1, 2],
                        "step_duration": "15s",
                        "spawn_rate": 1,
                        "warmup": {"enabled": False},
                        "auto_extend": {"enabled": False},
                    },
                }
            )
            spec_path = job.report_dir / "job_spec.json"
            payload = spec_path.read_text(encoding="utf-8")
            self.assertIn('"steps": [', payload)
            self.assertIn('"request_mode": "unique"', payload)
            self.assertNotIn("api_key", payload)
            self.assertEqual(
                job.job_spec["model_capability_profile"]["profile_status"],
                "registered",
            )
            self.assertEqual(
                job.job_spec["reference_contract_id"],
                job.reference_source,
            )
            self.assertIsNone(job.users)
            self.assertEqual(job.staircase_plan["steps"], [1, 2])


class DirectLocustSafetyTest(unittest.TestCase):
    def test_direct_locust_import_rejects_registered_image_model(self) -> None:
        env = dict(os.environ)
        env.pop("LOADTEST_JOB_SPEC", None)
        env.update(
            {
                "LOADTEST_SKIP_DOTENV": "1",
                "LOADTEST_PROVIDER": "gemini",
                "LOADTEST_MODEL": "gemini-3.1-flash-lite-image",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", "import locustfile"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "image/video or unregistered models are rejected",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
