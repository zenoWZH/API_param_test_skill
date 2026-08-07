from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lib.config import (
    get_model_reference_source,
    get_model_transport,
    get_provider_interface,
    load_config,
    resolve_threshold_config,
    validate_provider_config,
)
from lib.job_spec import (
    resolve_cache_plan,
    resolve_staircase_plan,
    validate_workload,
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
                    "provider": "yibu",
                    "model": "deepseek-v4-pro",
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
                job.job_spec["reference_source"],
                job.reference_source,
            )
            self.assertIsNone(job.users)
            self.assertEqual(job.staircase_plan["steps"], [1, 2])


if __name__ == "__main__":
    unittest.main()
