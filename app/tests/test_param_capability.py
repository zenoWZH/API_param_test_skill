from __future__ import annotations

import copy
import unittest

from lib.config import (
    _normalize_provider_config,
    get_image_model_config,
    get_model_api_form,
    get_model_api_forms,
    get_model_family,
    get_model_route_profile,
    get_model_route_profiles,
    list_image_providers,
    load_config,
)
from lib.deepseek_params import build_request, weighted_workload_profiles
from lib.image_validation import apply_capability_expectations, evaluate_case, grok_imagine_cases
from lib.param_outcome import compatibility_pass_from_statuses, map_probe_outcome
from lib.model_profile_catalog import (
    get_model_profile_catalog,
    resolve_runtime_profile_binding,
)
from lib.reference_specs import (
    capability_profile_snapshot,
    comparison_reference_source_for_model,
    default_reference_source_for_model,
    load_model_capability_profile,
    load_model_capability_profiles,
    model_reference_spec_payload,
    pressure_profiles_for_model,
    pressure_test_runnable,
    reference_param_rows,
    reference_sources_for_model,
    resolve_profile_expectation,
)


class ParamCapabilityMatrixTest(unittest.TestCase):
    @staticmethod
    def _legacy_text_provider(
        name: str,
        model: str,
        family: str,
        *,
        transport: str = "chat_completions",
        reference_source: str | None = None,
    ) -> dict:
        models = {
            "default": model,
            "candidates": [model],
            "families": {model: family},
            "transports": {model: transport},
        }
        if reference_source:
            models["reference_sources"] = {model: reference_source}
        return {
            "label": name,
            "base_url": f"https://{name}.example/v1",
            "backend": "proxy_unknown",
            "default_transport": transport,
            "models": models,
        }

    def test_capability_loaders_are_mpdb_only(self) -> None:
        profiles = load_model_capability_profiles()
        self.assertEqual(profiles["projection_source"], "yibu-model-profile-db")
        self.assertTrue(profiles["catalog_digest"])
        self.assertTrue(profiles["test_extension_digest"])
        with self.assertRaises(TypeError):
            load_model_capability_profiles("retired-capabilities.yaml")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            load_model_capability_profile(
                "text",
                "gpt",
                "gpt-5-mini",
                path="retired-capabilities.yaml",  # type: ignore[call-arg]
            )

    def test_legacy_route_migration_preserves_supplier_evidence(self) -> None:
        providers = {
            "unknown_proxy": self._legacy_text_provider(
                "unknown_proxy", "gpt-5.5", "gpt"
            ),
            "claude_bedrock": self._legacy_text_provider(
                "claude_bedrock",
                "claude-sonnet-5",
                "claude",
                transport="claude_messages",
            ),
            "claude_vertex": self._legacy_text_provider(
                "claude_vertex",
                "claude-sonnet-5",
                "claude",
                transport="claude_messages",
            ),
            "openrouter_baseline": self._legacy_text_provider(
                "openrouter_baseline", "moonshotai/kimi-k3", "kimi"
            ),
            "aliyun_reseller": self._legacy_text_provider(
                "aliyun_reseller",
                "glm-5.2",
                "glm",
                reference_source="aliyun_glm5_openai_compat",
            ),
        }
        config = {
            "active_provider": "unknown_proxy",
            "api": {"timeout_sec": 30},
            "providers": providers,
            "models": {},
        }
        _normalize_provider_config(config)
        first = copy.deepcopy(config)
        _normalize_provider_config(config)
        self.assertEqual(config, first, "provider normalization must be idempotent")

        expected = {
            "unknown_proxy": "dynamic_aggregator",
            "claude_bedrock": "aws_bedrock",
            "claude_vertex": "google_vertex",
            "openrouter_baseline": "openrouter",
            "aliyun_reseller": "aliyun_maas",
        }
        for provider, route in expected.items():
            with self.subTest(provider=provider):
                model = config["providers"][provider]["models"]["default"]
                routes = get_model_route_profiles(config, model, provider)
                self.assertEqual(list(routes), [route])

    def test_legacy_image_proxy_is_not_declared_vendor_direct(self) -> None:
        config = {
            "active_provider": "image_proxy",
            "api": {"timeout_sec": 30},
            "providers": {
                "image_proxy": {
                    "label": "image proxy",
                    "base_url": "https://images.example/v1",
                    "backend": "proxy_unknown",
                    "default_transport": "chat_completions",
                    "models": {"candidates": [], "families": {}},
                    "image": {
                        "enabled": True,
                        "default": "gpt-image-2",
                        "models": [
                            {
                                "id": "gpt-image-2",
                                "family": "gpt-image-2",
                                "transport": "images-generations",
                            }
                        ],
                    },
                }
            },
            "models": {},
        }
        model = get_image_model_config(config, "image_proxy", "gpt-image-2")
        self.assertEqual(model["route_profile"], "dynamic_aggregator")

    def test_every_configured_model_is_registered_or_explicitly_unresolved(self) -> None:
        config = load_config()
        config["providers"]["unresolved_fixture"] = {
            "label": "unresolved runtime fixture",
            "base_url": "https://example.invalid/v1",
            "backend": "proxy_unknown",
            "default_transport": "chat_completions",
            "models": {
                "default": "unregistered-model",
                "candidates": ["unregistered-model"],
                "families": {"unregistered-model": "gpt"},
                "routes": {
                    "unregistered-model": {
                        "dynamic_aggregator": {
                            "api_forms": {"openai_chat_completions": {}}
                        }
                    }
                },
                "default_routes": {
                    "unregistered-model": "dynamic_aggregator"
                },
                "default_api_forms": {
                    "unregistered-model": {
                        "dynamic_aggregator": "openai_chat_completions"
                    }
                },
            },
        }
        text_checked = 0
        unresolved_checked = 0
        fail_closed_checked = 0
        for provider, provider_cfg in config["providers"].items():
            models_cfg = provider_cfg.get("models") or {}
            candidates = list(models_cfg.get("candidates") or [])
            if models_cfg.get("default") not in candidates:
                candidates.append(models_cfg.get("default"))
            for raw_model in candidates:
                if not raw_model:
                    continue
                model = str(raw_model)
                family = get_model_family(config, model, provider)
                for route_profile in get_model_route_profiles(
                    config, model, provider
                ):
                    for api_form in get_model_api_forms(
                        config,
                        model,
                        provider,
                        route_profile=route_profile,
                    ):
                      with self.subTest(
                        modality="text",
                        provider=provider,
                        model=model,
                        api_form=api_form,
                    ):
                        binding = resolve_runtime_profile_binding(
                            config,
                            provider,
                            model,
                            family,
                            route_profile,
                            api_form,
                            modality="text",
                        )
                        if not binding.get("catalog_resolved"):
                            self.assertTrue(binding.get("reference_unresolved"))
                            self.assertIsNone(binding.get("profile_id"))
                            unresolved_checked += 1
                            continue
                        capability_family = str(
                            binding.get("suite_family_id") or family
                        )
                        profile = binding.get("profile") or {}
                        capability_model = str(
                            profile.get("model_slug") or model
                        )
                        try:
                            capability = load_model_capability_profile(
                                "text",
                                capability_family,
                                capability_model,
                                api_form=api_form,
                                route_profile=route_profile,
                            )
                        except (KeyError, ValueError):
                            interface = binding.get("interface") or {}
                            policies = [
                                row
                                for row in interface.get("test_bindings") or []
                                if row.get("extension_type") == "model_test_policy"
                            ]
                            self.assertTrue(
                                interface.get("test_binding_status") != "required"
                                or not policies
                            )
                            fail_closed_checked += 1
                            continue
                        if (
                            not capability["known_model"]
                            or not capability["known_api_profile"]
                        ):
                            self.assertIn(
                                route_profile,
                                {
                                    "cloud_adapter",
                                    "dynamic_aggregator",
                                    "openrouter",
                                    "provider_compat",
                                },
                            )
                            self.assertEqual(
                                binding["binding_source"],
                                "catalog_official_reference",
                            )
                            text_checked += 1
                            continue
                        self.assertTrue(capability["known_model"])
                        self.assertTrue(capability["known_api_profile"])
                        self.assertEqual(capability["api_form"], api_form)
                        self.assertEqual(
                            capability["route_profile"], route_profile
                        )
                        try:
                            sources = reference_sources_for_model(
                                config,
                                family,
                                model,
                                provider,
                                api_form=api_form,
                                route_profile=route_profile,
                            )
                        except (KeyError, ValueError):
                            self.assertFalse(capability["parameter_test_enabled"])
                            self.assertFalse(capability["pressure_test_enabled"])
                            fail_closed_checked += 1
                            continue
                        self.assertTrue(sources)
                        comparison_source = comparison_reference_source_for_model(
                            "text",
                            capability_family,
                            capability_model,
                            api_form=api_form,
                            route_profile=route_profile,
                        )
                        self.assertIn(comparison_source, sources)
                        if capability["pressure_test_enabled"]:
                            self.assertTrue(
                                pressure_profiles_for_model(
                                    capability_family,
                                    capability_model,
                                    sources[0],
                                    api_form=api_form,
                                    route_profile=route_profile,
                                )
                            )
                        text_checked += 1

        image_checked = 0
        for provider_cfg in list_image_providers(config):
            for model_cfg in provider_cfg["models"]:
                model = str(model_cfg["id"])
                family = str(model_cfg["family"])
                for route_profile, route_cfg in model_cfg["routes"].items():
                    for api_form in route_cfg["api_forms"]:
                      with self.subTest(
                        modality="image",
                        provider=provider_cfg["name"],
                        model=model,
                        api_form=api_form,
                    ):
                        binding = resolve_runtime_profile_binding(
                            config,
                            str(provider_cfg["name"]),
                            model,
                            family,
                            str(route_profile),
                            str(api_form),
                            modality="image",
                        )
                        if not binding.get("catalog_resolved"):
                            self.assertTrue(binding.get("reference_unresolved"))
                            self.assertIsNone(binding.get("profile_id"))
                            unresolved_checked += 1
                            continue
                        profile = binding.get("profile") or {}
                        capability_family = str(
                            binding.get("suite_family_id") or family
                        )
                        capability_model = str(
                            profile.get("model_slug") or model
                        )
                        try:
                            capability = load_model_capability_profile(
                                "image",
                                capability_family,
                                capability_model,
                                api_form=api_form,
                                route_profile=route_profile,
                            )
                        except (KeyError, ValueError):
                            interface = binding.get("interface") or {}
                            policies = [
                                row
                                for row in interface.get("test_bindings") or []
                                if row.get("extension_type") == "model_test_policy"
                            ]
                            self.assertTrue(
                                interface.get("test_binding_status") != "required"
                                or not policies
                            )
                            fail_closed_checked += 1
                            continue
                        self.assertTrue(capability["known_model"])
                        self.assertTrue(capability["known_api_profile"])
                        self.assertEqual(capability["api_form"], api_form)
                        self.assertIn(
                            capability["suite"],
                            {"banana", "gpt_image_2", "grok_imagine"},
                        )
                        self.assertFalse(capability["pressure_test_enabled"])
                        self.assertFalse(
                            capability["test_policy_pressure_test_enabled"]
                        )
                        for field in (
                            "pressure_profiles",
                            "pressure_omit_params",
                            "pressure_parameter_aliases",
                            "pressure_overrides",
                            "pressure_transport_overrides",
                        ):
                            self.assertFalse(capability[field])
                        image_checked += 1

        self.assertGreater(text_checked, 0)
        self.assertGreater(image_checked, 0)
        self.assertGreater(unresolved_checked, 0)
        # App provider fixtures may classify every non-runnable selection as
        # unresolved before the strict capability lookup. Both paths are
        # fail-closed; no diagnostic pseudo-profile is accepted.
        self.assertGreater(fail_closed_checked + unresolved_checked, 0)

    def test_each_api_form_resolves_an_isolated_model_profile(self) -> None:
        cases = (
            (
                "gpt",
                "gpt-5.6-luna",
                "vendor_direct",
                "openai_chat_completions",
                "openai_gpt56_chat",
                "openai_responses",
                "openai_gpt56_responses",
            ),
            (
                "gpt",
                "gpt-5.6-sol",
                "vendor_direct",
                "openai_chat_completions",
                "openai_gpt56_chat",
                "openai_responses",
                "openai_gpt56_responses",
            ),
            (
                "gpt",
                "gpt-5.6-terra",
                "vendor_direct",
                "openai_chat_completions",
                "openai_gpt56_chat",
                "openai_responses",
                "openai_gpt56_responses",
            ),
            (
                "claude",
                "claude-opus-4-8",
                "vendor_compat",
                "openai_chat_completions",
                "claude_openai_compat",
                "vendor_direct",
                "anthropic_messages",
                "claude_native_messages",
            ),
        )
        for case in cases:
            if len(case) == 7:
                family, model, shared_route, first_form, first_source, second_form, second_source = case
                first_route = second_route = shared_route
            else:
                family, model, first_route, first_form, first_source, second_route, second_form, second_source = case
            with self.subTest(family=family, model=model):
                first = load_model_capability_profile(
                    "text", family, model, route_profile=first_route, api_form=first_form
                )
                second = load_model_capability_profile(
                    "text", family, model, route_profile=second_route, api_form=second_form
                )
                self.assertEqual(first["default_reference_source"], first_source)
                self.assertEqual(second["default_reference_source"], second_source)
                self.assertNotEqual(first["model_api_profile_id"], second["model_api_profile_id"])
                self.assertNotEqual(first["transport"], second["transport"])
                with self.assertRaises(ValueError):
                    load_model_capability_profile(
                        "text",
                        family,
                        model,
                        route_profile=first_route,
                        api_form=first_form,
                        reference_source=second_source,
                    )

        # The Gemini 2.5 native Interface is retained as an access-restricted
        # identity, not promoted into an executable Test Binding.
        with self.assertRaises(ValueError):
            load_model_capability_profile(
                "text",
                "gemini",
                "gemini-2.5-pro",
                route_profile="google_ai_studio",
                api_form="gemini_generate_content",
            )

    def test_gemini_ai_studio_and_vertex_are_distinct_route_profiles(self) -> None:
        catalog = get_model_profile_catalog()
        studio = catalog.get_profile(
            "text/google_ai_studio/gemini/gemini-2.5-pro"
        )
        vertex = catalog.get_profile(
            "text/google_vertex/gemini/gemini-2.5-pro"
        )
        self.assertEqual(studio["source_id"], "google_ai_studio")
        self.assertEqual(vertex["source_id"], "google_vertex")
        self.assertEqual(studio["profile_state"], "executable")
        self.assertEqual(vertex["profile_state"], "identity_only")
        self.assertNotEqual(studio["profile_id"], vertex["profile_id"])
        self.assertNotEqual(
            {row["parameter"] for row in reference_param_rows(
                "gemini_native_generate_content"
            )},
            {row["parameter"] for row in reference_param_rows(
                "gemini_vertex_generate_content"
            )},
        )
        for route in ("google_ai_studio", "google_vertex"):
            with self.subTest(route=route):
                with self.assertRaises(ValueError):
                    load_model_capability_profile(
                        "text",
                        "gemini",
                        "gemini-2.5-pro",
                        route_profile=route,
                        api_form="gemini_generate_content",
                    )

    def test_route_is_resolved_before_api_form_and_invalid_form_is_scoped(self) -> None:
        config = copy.deepcopy(load_config())
        provider = config["providers"]["gemini"]
        model = "gemini-2.5-pro"
        provider["models"]["routes"][model]["google_vertex"] = {
            "api_forms": {"gemini_generate_content": {}}
        }
        provider["models"]["default_routes"][model] = "google_vertex"
        provider["models"]["default_api_forms"][model]["google_vertex"] = (
            "gemini_generate_content"
        )
        route = get_model_route_profile(config, model, "gemini")
        self.assertEqual(route, "google_vertex")
        self.assertEqual(
            get_model_api_form(
                config,
                model,
                "gemini",
                route_profile=route,
            ),
            "gemini_generate_content",
        )
        with self.assertRaisesRegex(ValueError, "on route 'google_vertex'"):
            get_model_api_form(
                config,
                model,
                "gemini",
                route_profile="google_vertex",
                api_form="openai_chat_completions",
            )

    def test_unregistered_route_and_form_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            load_model_capability_profile(
                "text",
                "gemini",
                "gemini-2.5-pro",
                route_profile="unregistered-route",
                api_form="gemini_generate_content",
            )
        with self.assertRaises(ValueError):
            load_model_capability_profile(
                "text",
                "gemini",
                "gemini-2.5-pro",
                route_profile="google_vertex",
                api_form="openai_chat_completions",
            )

    def test_resolve_inherits_family_defaults_and_model_overrides(self) -> None:
        baseline = resolve_profile_expectation(
            "text",
            "grok",
            "grok-4.5",
            "grok_responses_tools",
            route_profile="vendor_direct",
        )
        self.assertEqual(baseline, "supported")

        effort_none = resolve_profile_expectation(
            "text",
            "grok",
            "grok-4.5",
            "grok_responses_reasoning_effort_none",
            route_profile="vendor_direct",
        )
        self.assertEqual(effort_none, "unsupported")

        multi_agent_tools = resolve_profile_expectation(
            "text",
            "grok",
            "grok-4.20-multi-agent-0309",
            "grok_responses_tools",
            route_profile="vendor_direct",
        )
        self.assertEqual(multi_agent_tools, "unsupported")
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "grok",
                "grok-4.20-multi-agent-0309",
                "grok_responses_reasoning_effort_none",
                route_profile="vendor_direct",
            ),
            "supported",
        )

        # Snapshots must still resolve family defaults (regression: missing expectations key).
        from lib.reference_specs import capability_profile_snapshot

        snap = capability_profile_snapshot(
            "text",
            "grok",
            "grok-4.5",
            ["grok_responses_tools", "grok_responses_reasoning_effort_none"],
            route_profile="vendor_direct",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "grok",
                "grok-4.5",
                "grok_responses_reasoning_effort_none",
                capability_profile=snap,
            ),
            "unsupported",
        )

    def test_unknown_model_fails_closed_without_a_pseudo_profile(self) -> None:
        with self.assertRaises(ValueError):
            load_model_capability_profile(
                "text",
                "grok",
                "grok-future-unknown",
                route_profile="vendor_direct",
                api_form="openai_responses",
            )
        with self.assertRaises(ValueError):
            resolve_profile_expectation(
                "text",
                "grok",
                "grok-future-unknown",
                "grok_responses_tools",
                route_profile="vendor_direct",
                api_form="openai_responses",
            )

    def test_registered_models_expand_family_suite_into_explicit_lists(self) -> None:
        snapshot = capability_profile_snapshot(
            "text",
            "grok",
            "grok-4.20-multi-agent-0309",
            [
                "grok_responses_basic",
                "grok_responses_tools",
                "grok_responses_reasoning_effort_none",
            ],
            reference_source="grok_responses",
        )

        self.assertEqual(snapshot["profile_status"], "registered")
        self.assertIn("grok_responses_basic", snapshot["supported_profiles"])
        self.assertIn("grok_responses_tools", snapshot["unsupported_profiles"])
        self.assertIn("tools", snapshot["unsupported_parameters"])
        self.assertEqual(
            snapshot["resolved_parameter_expectations"]["reasoning.effort"],
            "supported",
        )

    def test_model_reference_payload_annotates_each_parameter(self) -> None:
        payload = model_reference_spec_payload(
            "text",
            "grok",
            "grok-4.20-multi-agent-0309",
            "grok_responses",
        )
        tools = next(
            row for row in payload["comparison"] if row["parameter"] == "tools"
        )
        reasoning = next(
            row
            for row in payload["comparison"]
            if row["parameter"] == "reasoning.effort"
        )

        self.assertEqual(tools["model_expectation"], "unsupported")
        self.assertEqual(
            tools["profile_expectations"]["grok_responses_tools"],
            "unsupported",
        )
        self.assertEqual(reasoning["model_expectation"], "supported")

        aliyun_deepseek = model_reference_spec_payload(
            "text",
            "deepseek",
            "deepseek-v4-pro",
            "aliyun_deepseek_v4_openai_compat",
        )
        unsupported = {
            row["parameter"]
            for row in aliyun_deepseek["comparison"]
            if row["model_expectation"] == "unsupported"
        }
        self.assertIn("top_k", unsupported)
        self.assertNotIn("response_format", unsupported)

    def test_comparison_reference_source_is_provider_independent_and_model_specific(self) -> None:
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "deepseek",
                "deepseek-v4-pro",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            ),
            "deepseek_v4_pro_0813_chat",
        )
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "deepseek",
                "deepseek-v4-pro",
                route_profile="aliyun_maas",
                api_form="openai_chat_completions",
            ),
            "aliyun_deepseek_v4_openai_compat",
        )
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "claude",
                "claude-opus-4-8",
                route_profile="vendor_direct",
                api_form="anthropic_messages",
            ),
            "claude_native_messages",
        )
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "gpt",
                "gpt-5.6-sol",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            ),
            "openai_gpt56_chat",
        )
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "kimi",
                "kimi-k3",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            ),
            "kimi_k3_openai_compat",
        )

    def test_transport_selects_model_family_reference_source(self) -> None:
        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                    "openai_responses": {
                        "path": "/responses",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "gpt-5.5",
                    "candidates": ["gpt-5.5", "grok-4.20-multi-agent-0309"],
                    "families": {
                        "gpt-5.5": "gpt",
                        "grok-4.20-multi-agent-0309": "grok",
                    },
                    "transports": {
                        "gpt-5.5": "chat_completions",
                        "grok-4.20-multi-agent-0309": "openai_responses",
                    },
                    "reference_sources": {},
                },
            }
        }

        self.assertEqual(
            default_reference_source_for_model(
                config,
                "gpt",
                "gpt-5.5",
                "capability_test",
            ),
            "openai_gpt5_chat",
        )
        self.assertEqual(
            default_reference_source_for_model(
                config,
                "grok",
                "grok-4.20-multi-agent-0309",
                "capability_test",
            ),
            "grok_responses",
        )

    def test_kimi_k3_negative_contract_and_pressure_parameters(self) -> None:
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "kimi",
                "kimi-k3",
                "kimi_k3_reject_top_p",
                reference_source="kimi_k3_openai_compat",
            ),
            "unsupported",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "kimi",
                "kimi-k3",
                "kimi_k3_reasoning_max",
                reference_source="kimi_k3_openai_compat",
            ),
            "supported",
        )
        pressure_profiles = pressure_profiles_for_model(
            "kimi",
            "kimi-k3",
            "kimi_k3_openai_compat",
        )
        self.assertIn("kimi_k3_reasoning_max", pressure_profiles)
        self.assertNotIn("kimi_k3_reject_top_p", pressure_profiles)

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "kimi-k3",
                    "candidates": ["kimi-k3"],
                    "families": {"kimi-k3": "kimi"},
                    "transports": {},
                    "reference_sources": {},
                },
            }
        }
        pressure = build_request(
            config,
            "throughput_profiles",
            "baseline_short",
        )
        self.assertNotIn("max_tokens", pressure.body)
        self.assertEqual(pressure.body["max_completion_tokens"], 128)
        self.assertEqual(pressure.body["temperature"], 1.0)
        self.assertEqual(pressure.body["top_p"], 0.95)
        self.assertEqual(pressure.body["reasoning_effort"], "max")
        self.assertEqual(
            pressure.metadata["capability_pressure_aliases"],
            {"max_tokens": "max_completion_tokens"},
        )
        self.assertEqual(
            pressure.metadata["capability_pressure_overrides"],
            {
                "temperature": 1.0,
                "top_p": 0.95,
                "reasoning_effort": "max",
            },
        )
        cache_request = build_request(
            config,
            "cache_profiles",
            "cache_long_context",
        )
        self.assertNotIn("max_tokens", cache_request.body)
        self.assertEqual(cache_request.body["max_completion_tokens"], 128)
        self.assertEqual(cache_request.body["temperature"], 1.0)
        self.assertEqual(cache_request.body["top_p"], 0.95)
        self.assertEqual(cache_request.body["reasoning_effort"], "max")

        mixed_profiles = {
            profile
            for group, profile, weight in weighted_workload_profiles(
                config,
                "mixed_compat",
            )
            if group == "compatibility_profiles" and weight > 0
        }
        self.assertIn("kimi_k3_stream", mixed_profiles)
        self.assertIn("kimi_k3_dynamic_tools", mixed_profiles)
        self.assertNotIn("basic_stream", mixed_profiles)
        self.assertNotIn("kimi_k3_reject_top_p", mixed_profiles)

    def test_glm_reasoning_support_is_model_specific_and_pressure_safe(self) -> None:
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "glm",
                "glm-5.2",
                "glm_reasoning_max",
                reference_source="glm_openai_compat",
            ),
            "supported",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "glm",
                "glm-5.1",
                "glm_reasoning_max",
                reference_source="glm_openai_compat",
            ),
            "unsupported",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "glm",
                "glm-5.1",
                "glm_reasoning_xhigh",
                reference_source="glm_openai_compat",
            ),
            "supported",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "glm",
                "glm-4.7",
                "glm_reasoning_high",
                reference_source="glm_openai_compat",
            ),
            "unsupported",
        )
        latest_pressure = pressure_profiles_for_model(
            "glm",
            "glm-5.2",
            "glm_openai_compat",
        )
        self.assertIn("glm_reasoning_max", latest_pressure)
        self.assertIn("glm_tool_calls_thinking", latest_pressure)
        previous_pressure = pressure_profiles_for_model(
            "glm",
            "glm-5.1",
            "glm_openai_compat",
        )
        self.assertNotIn("glm_reasoning_max", previous_pressure)
        self.assertNotIn("glm_tool_calls_thinking", previous_pressure)

        config = copy.deepcopy(load_config())
        config["active_provider"] = "yibu"
        config["providers"]["yibu"]["models"]["default"] = "glm-5.2"
        mixed_profiles = {
            profile
            for group, profile, weight in weighted_workload_profiles(
                config,
                "mixed_compat",
            )
            if group == "compatibility_profiles" and weight > 0
        }
        self.assertIn("glm_reasoning_max", mixed_profiles)
        self.assertIn("glm_tool_calls_thinking", mixed_profiles)
        pressure_request = build_request(
            config,
            "compatibility_profiles",
            "glm_tool_calls_thinking",
        )
        self.assertEqual(pressure_request.body["reasoning_effort"], "max")
        self.assertFalse(pressure_request.body["thinking"]["clear_thinking"])
        self.assertTrue(pressure_request.metadata["pass_reasoning_content"])

    def test_glm_5_3_and_flash_have_dedicated_pressure_policies(self) -> None:
        glm53 = load_model_capability_profile(
            "text",
            "glm",
            "glm-5.3",
            route_profile="vendor_direct",
            api_form="openai_chat_completions",
        )
        self.assertEqual(glm53["reference_source"], "zhipu_glm_5_3_openai_compat")
        self.assertTrue(glm53["pressure_test_enabled"])
        self.assertEqual(
            pressure_profiles_for_model(
                "glm",
                "glm-5.3",
                "zhipu_glm_5_3_openai_compat",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            ),
            [
                "glm53_stream",
                "glm53_reasoning_low",
                "glm53_reasoning_max",
                "glm53_temperature",
                "glm53_max_tokens",
                "glm53_json_output",
                "glm53_tools",
                "glm53_tool_calls_thinking",
            ],
        )

        flash = load_model_capability_profile(
            "text",
            "glm",
            "glm-5.3-flash",
            route_profile="vendor_direct",
            api_form="openai_chat_completions",
        )
        self.assertEqual(
            flash["reference_source"],
            "zhipu_glm_5_3_flash_openai_compat",
        )
        self.assertTrue(flash["pressure_test_enabled"])
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "glm",
                "glm-5.3-flash",
                "glm53_flash_file_image_mixed",
                capability_profile=flash,
                reference_source="zhipu_glm_5_3_flash_openai_compat",
            ),
            "unsupported",
        )

    def test_qwen_3_7_thinking_profiles_and_pressure_selection(self) -> None:
        latest_pressure = pressure_profiles_for_model(
            "qwen",
            "qwen3.7-max",
            "qwen_openai_compat",
        )
        self.assertIn("qwen_thinking_enabled", latest_pressure)
        self.assertIn("qwen_thinking_budget", latest_pressure)
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "qwen",
                "qwen3.7-max",
                "qwen_preserve_thinking",
                reference_source="qwen_openai_compat",
            ),
            "supported",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "qwen",
                "qwen3.5-plus",
                "qwen_preserve_thinking",
                reference_source="qwen_openai_compat",
            ),
            "unsupported",
        )

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "qwen3.7-max",
                    "candidates": ["qwen3.7-max"],
                    "families": {"qwen3.7-max": "qwen"},
                    "transports": {},
                    "reference_sources": {},
                },
            }
        }
        mixed_profiles = {
            profile
            for group, profile, weight in weighted_workload_profiles(
                config,
                "mixed_compat",
            )
            if group == "compatibility_profiles" and weight > 0
        }
        self.assertIn("qwen_thinking_enabled", mixed_profiles)
        self.assertIn("qwen_thinking_budget", mixed_profiles)

        thinking = build_request(
            config,
            "compatibility_profiles",
            "qwen_thinking_budget",
        )
        self.assertTrue(thinking.body["enable_thinking"])
        self.assertEqual(thinking.body["thinking_budget"], 64)
        self.assertEqual(thinking.body["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", thinking.body)

    def test_gemini_3_6_reasoning_and_pressure_contract(self) -> None:
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "gemini",
                "gemini-3.6-flash",
                "gemini_reasoning_high",
                reference_source="gemini_openai_compat",
            ),
            "supported",
        )
        for profile in (
            "gemini_n",
            "gemini_chat_candidate_count",
            "gemini_native_candidate_count",
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    resolve_profile_expectation(
                        "text",
                        "gemini",
                        "gemini-3.6-flash",
                        profile,
                        reference_source="gemini_openai_compat",
                    ),
                    "unsupported",
                )

        chat_pressure = pressure_profiles_for_model(
            "gemini",
            "gemini-3.6-flash",
            "gemini_openai_compat",
        )
        self.assertIn("gemini_reasoning_medium", chat_pressure)
        self.assertIn("gemini_reasoning_high", chat_pressure)
        self.assertNotIn("sampling_non_thinking", chat_pressure)
        self.assertNotIn("gemini_n", chat_pressure)
        with self.assertRaisesRegex(ValueError, "Pressure testing is disabled"):
            pressure_profiles_for_model(
                "gemini",
                "gemini-3.6-flash",
                "gemini_native_generate_content",
            )

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "gemini-3.6-flash",
                    "candidates": ["gemini-3.6-flash"],
                    "families": {"gemini-3.6-flash": "gemini"},
                    "routes": {
                        "gemini-3.6-flash": {
                            "google_ai_studio": {
                                "api_forms": {"openai_chat_completions": {}}
                            }
                        }
                    },
                    "default_routes": {
                        "gemini-3.6-flash": "google_ai_studio"
                    },
                    "default_api_forms": {
                        "gemini-3.6-flash": {
                            "google_ai_studio": "openai_chat_completions"
                        }
                    },
                    "transports": {},
                    "reference_sources": {},
                },
            }
        }
        mixed_profiles = {
            profile
            for group, profile, weight in weighted_workload_profiles(
                config,
                "mixed_compat",
            )
            if group == "compatibility_profiles" and weight > 0
        }
        self.assertIn("gemini_reasoning_medium", mixed_profiles)
        self.assertIn("gemini_reasoning_high", mixed_profiles)
        self.assertNotIn("sampling_non_thinking", mixed_profiles)

        pressure = build_request(
            config,
            "throughput_profiles",
            "streaming_latency_api",
        )
        self.assertNotIn("temperature", pressure.body)
        self.assertIn(
            "temperature",
            pressure.metadata["capability_omitted_params"],
        )

        nested = build_request(
            config,
            "compatibility_profiles",
            "gemini_chat_candidate_count",
        )
        self.assertNotIn("candidateCount", nested.body["generationConfig"])
        probe = build_request(
            config,
            "compatibility_profiles",
            "gemini_chat_candidate_count",
            model_family_override="gemini",
            enforce_model_capabilities=False,
        )
        self.assertEqual(probe.body["generationConfig"]["candidateCount"], 2)

    def test_claude_opus_5_adaptive_effort_and_pressure_contract(self) -> None:
        for profile, expected in (
            ("claude_native_top_p", "supported"),
            ("claude_native_thinking_budget", "unsupported"),
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    resolve_profile_expectation(
                        "text",
                        "claude",
                        "claude-opus-5",
                        profile,
                        reference_source="claude_native_messages",
                        route_profile="vendor_direct",
                    ),
                    expected,
                )
        with self.assertRaises(ValueError):
            resolve_profile_expectation(
                "text",
                "claude",
                "claude-opus-5",
                "claude_native_top_p",
                reference_source="claude_aws_bedrock_runtime_messages",
                route_profile="aws_bedrock",
                api_form="aws_bedrock_runtime_messages",
            )
        profiles = pressure_profiles_for_model(
            "claude",
            "claude-opus-5",
            "claude_native_messages",
            route_profile="vendor_direct",
        )
        self.assertNotIn("claude_native_thinking_adaptive", profiles)
        self.assertNotIn("claude_native_effort_medium", profiles)
        self.assertIn("claude_native_temperature", profiles)
        self.assertNotIn("claude_native_top_p", profiles)
        self.assertNotIn("claude_native_thinking_budget", profiles)

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "anthropic",
                "route_profile": "vendor_direct",
                "default_transport": "claude_messages",
                "api_interfaces": {
                    "claude_messages": {
                        "path": "/messages",
                        "auth": "x-api-key",
                    },
                },
                "models": {
                    "default": "claude-opus-5",
                    "candidates": ["claude-opus-5"],
                    "families": {"claude-opus-5": "claude"},
                    "transports": {"claude-opus-5": "claude_messages"},
                    "reference_sources": {},
                },
            }
        }
        mixed_profiles = {
            profile
            for group, profile, weight in weighted_workload_profiles(
                config,
                "mixed_compat",
            )
            if group == "compatibility_profiles" and weight > 0
        }
        self.assertNotIn("claude_native_effort_medium", mixed_profiles)
        self.assertIn("claude_native_temperature", mixed_profiles)

        effort = build_request(
            config,
            "compatibility_profiles",
            "claude_native_effort_medium",
        )
        self.assertEqual(effort.body["thinking"]["type"], "adaptive")
        self.assertEqual(effort.body["output_config"]["effort"], "medium")
        pressure = build_request(
            config,
            "throughput_profiles",
            "streaming_latency_api",
        )
        self.assertNotIn("temperature", pressure.body)
        self.assertIn(
            "temperature",
            pressure.metadata["capability_omitted_params"],
        )

    def test_gpt_5_6_sol_official_sources_and_transport_safe_pressure(self) -> None:
        self.assertEqual(
            comparison_reference_source_for_model(
                "text",
                "gpt",
                "gpt-5.6-sol",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            ),
            "openai_gpt56_chat",
        )
        self.assertEqual(
            resolve_profile_expectation(
                "text",
                "gpt",
                "gpt-5.6-sol",
                "gpt5_chat_reject_temperature",
                reference_source="openai_gpt56_chat",
            ),
            "unsupported",
        )
        chat_profiles = pressure_profiles_for_model(
            "gpt",
            "gpt-5.6-sol",
            "openai_gpt56_chat",
        )
        self.assertIn("gpt5_chat_tools", chat_profiles)
        self.assertNotIn("gpt5_chat_reject_temperature", chat_profiles)

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "route_profile": "vendor_direct",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                    "openai_responses": {
                        "path": "/responses",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "gpt-5.6-sol",
                    "candidates": ["gpt-5.6-sol"],
                    "families": {"gpt-5.6-sol": "gpt"},
                    "transports": {"gpt-5.6-sol": "chat_completions"},
                    "reference_sources": {},
                },
            }
        }
        self.assertEqual(
            default_reference_source_for_model(
                config,
                "gpt",
                "gpt-5.6-sol",
                "capability_test",
            ),
            "openai_gpt56_chat",
        )
        chat_pressure = build_request(
            config,
            "throughput_profiles",
            "baseline_short",
        )
        self.assertEqual(chat_pressure.metadata["transport"], "chat_completions")
        self.assertEqual(chat_pressure.body["reasoning_effort"], "none")
        self.assertEqual(chat_pressure.body["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", chat_pressure.body)
        self.assertNotIn("temperature", chat_pressure.body)
        self.assertNotIn("top_p", chat_pressure.body)
        self.assertNotIn("thinking", chat_pressure.body)

        config["providers"]["capability_test"]["default_transport"] = "openai_responses"
        config["providers"]["capability_test"]["models"]["transports"][
            "gpt-5.6-sol"
        ] = "openai_responses"
        self.assertEqual(
            default_reference_source_for_model(
                config,
                "gpt",
                "gpt-5.6-sol",
                "capability_test",
            ),
            "openai_gpt56_responses",
        )
        responses_pressure = build_request(
            config,
            "throughput_profiles",
            "baseline_short",
        )
        self.assertEqual(
            responses_pressure.metadata["transport"],
            "openai_responses",
        )
        self.assertEqual(responses_pressure.body["max_output_tokens"], 128)
        self.assertEqual(
            responses_pressure.body["reasoning"],
            {"effort": "low"},
        )
        self.assertFalse(responses_pressure.body["store"])
        self.assertNotIn("max_tokens", responses_pressure.body)
        self.assertNotIn("reasoning_effort", responses_pressure.body)
        self.assertNotIn("temperature", responses_pressure.body)

    def test_image_only_model_cannot_load_as_text_capability(self) -> None:
        with self.assertRaises(ValueError):
            load_model_capability_profile(
                "text",
                "gpt",
                "gpt-image-2",
                route_profile="vendor_direct",
                api_form="openai_chat_completions",
            )

    def test_image_and_video_pressure_policy_cannot_be_overridden(self) -> None:
        profiles = load_model_capability_profiles()
        self.assertFalse(
            profiles["modalities"]["image"]["pressure_test_enabled"]
        )
        self.assertFalse(
            profiles["modalities"]["video"]["pressure_test_enabled"]
        )

        invalid_overrides = (
            {"pressure_test_enabled": True},
            {"test_policy_pressure_test_enabled": True},
            {"pressure_profiles": {"source": ["profile"]}},
            {"pressure_omit_params": ["temperature"]},
            {"pressure_parameter_aliases": {"max_tokens": "max_output_tokens"}},
            {"pressure_overrides": {"temperature": 1}},
            {"pressure_transport_overrides": {"images-generations": {"x": 1}}},
        )
        for provider_override in invalid_overrides:
            with self.subTest(provider_override=provider_override), self.assertRaisesRegex(
                ValueError,
                "cannot enable pressure testing|cannot define pressure policy fields",
            ):
                load_model_capability_profile(
                    "image",
                    "gpt-image-2",
                    "gpt-image-2",
                    route_profile="vendor_direct",
                    api_form="openai_images_generations",
                    provider_override=provider_override,
                )

    def test_pressure_runnable_preserves_text_policy_compatibility(self) -> None:
        self.assertTrue(
            pressure_test_runnable(
                {
                    "modality": "text",
                    "pressure_test_enabled": True,
                }
            )
        )
        self.assertTrue(
            pressure_test_runnable(
                {
                    "modality": "text",
                    "pressure_test_enabled": True,
                    "test_policy_pressure_test_enabled": False,
                }
            )
        )
        self.assertFalse(
            pressure_test_runnable(
                {
                    "modality": "image",
                    "pressure_test_enabled": True,
                }
            )
        )
        self.assertFalse(
            pressure_test_runnable({"pressure_test_enabled": True})
        )

    def test_pressure_profiles_and_request_body_exclude_unsupported_tools(self) -> None:
        profiles = pressure_profiles_for_model(
            "grok",
            "grok-4.20-multi-agent-0309",
            "grok_responses",
        )
        self.assertIn("grok_responses_basic", profiles)
        self.assertNotIn("grok_responses_tools", profiles)

        config = copy.deepcopy(load_config())
        config["active_provider"] = "capability_test"
        config["providers"] = {
            "capability_test": {
                "label": "test",
                "base_url": "https://example.test/v1",
                "backend": "openai_compatible",
                "default_transport": "openai_responses",
                "api_interfaces": {
                    "openai_responses": {
                        "path": "/responses",
                        "auth": "bearer",
                    },
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    },
                },
                "models": {
                    "default": "grok-4.20-multi-agent-0309",
                    "candidates": ["grok-4.20-multi-agent-0309"],
                    "families": {
                        "grok-4.20-multi-agent-0309": "grok",
                    },
                    "transports": {
                        "grok-4.20-multi-agent-0309": "openai_responses",
                    },
                    "reference_sources": {},
                },
            }
        }

        pressure = build_request(
            config,
            "compatibility_profiles",
            "grok_responses_tools",
        )
        self.assertNotIn("tools", pressure.body)
        self.assertNotIn("tool_choice", pressure.body)
        self.assertIn("tools", pressure.metadata["capability_omitted_params"])

        probe = build_request(
            config,
            "compatibility_profiles",
            "grok_responses_tools",
            model_family_override="grok",
            enforce_model_capabilities=False,
        )
        self.assertIn("tools", probe.body)
        self.assertEqual(probe.body["tool_choice"], "required")

    def test_outcome_mapping_table(self) -> None:
        self.assertEqual(
            map_probe_outcome("supported", status_code=200, validation_ok=True)["status"],
            "pass",
        )
        self.assertEqual(
            map_probe_outcome("supported", status_code=400, validation_ok=False)["status"],
            "incompatible",
        )
        self.assertEqual(
            map_probe_outcome("supported", status_code=200, validation_ok=False)["status"],
            "incompatible",
        )
        rejected = map_probe_outcome("unsupported", status_code=400, validation_ok=True)
        self.assertEqual(rejected["status"], "expected_rejection")
        self.assertTrue(rejected["pass"])
        accepted = map_probe_outcome("unsupported", status_code=200, validation_ok=True)
        self.assertEqual(accepted["status"], "unexpected_acceptance")
        self.assertFalse(accepted["pass"])
        self.assertEqual(
            map_probe_outcome("supported", status_code=500, validation_ok=False)["status"],
            "fail",
        )
        self.assertEqual(
            map_probe_outcome("unsupported", status_code=429, validation_ok=False)["status"],
            "fail",
        )

    def test_multi_agent_tools_marked_supported_would_fail_compatibility(self) -> None:
        # Acceptance criterion: if multi-agent tools were wrongly marked supported,
        # a 400 rejection must count as incompatible (not expected_rejection).
        wrong = map_probe_outcome("supported", status_code=400, validation_ok=False)
        self.assertEqual(wrong["status"], "incompatible")
        self.assertFalse(
            compatibility_pass_from_statuses(["pass", "incompatible", "expected_rejection"])
        )
        self.assertTrue(
            compatibility_pass_from_statuses(["pass", "expected_rejection", "pass"])
        )

    def test_image_capability_marks_negative_cases_unsupported(self) -> None:
        cases = grok_imagine_cases("resolution", include_negative=True)
        applied = apply_capability_expectations(
            cases,
            family="grok-imagine",
            model="grok-imagine-image",
            route_profile="vendor_direct",
        )
        by_name = {case.name: case for case in applied}
        negative = by_name["grok_reject_aspect_ratio_7_5"]
        self.assertEqual(negative.expected_outcome, "rejection")
        self.assertEqual(negative.metadata.get("expectation"), "unsupported")

        accepted = evaluate_case(negative, status_code=200)
        self.assertFalse(accepted["pass"])
        self.assertEqual(accepted["status"], "unexpected_acceptance")

        rejected = evaluate_case(negative, status_code=400)
        self.assertTrue(rejected["pass"])
        self.assertEqual(rejected["status"], "expected_rejection")

        positive = by_name["grok_1k_square_b64"]
        self.assertEqual(positive.metadata.get("expectation"), "supported")
        failed = evaluate_case(positive, status_code=400)
        self.assertFalse(failed["pass"])
        self.assertEqual(failed["status"], "incompatible")

    def test_all_image_families_use_model_profiles(self) -> None:
        from lib.image_validation import banana_variant_cases, gpt_image_2_cases

        gpt_cases = apply_capability_expectations(
            gpt_image_2_cases("resolution", include_negative=True),
            family="gpt-image-2",
            model="gpt-image-2",
            route_profile="vendor_direct",
        )
        gpt_by_name = {case.name: case for case in gpt_cases}
        self.assertEqual(
            gpt_by_name["reject_non_multiple_of_16"].metadata["expectation"],
            "unsupported",
        )
        self.assertEqual(
            gpt_by_name["baseline_1024_square"].metadata["expectation"],
            "supported",
        )

        with self.assertRaises(ValueError):
            apply_capability_expectations(
                banana_variant_cases(
                    "resolution",
                    model_template="gemini-3.1-flash-image",
                    include_cross_control=False,
                ),
                family="banana",
                model="gemini-3.1-flash-image",
                route_profile="provider_compat",
                api_form="openai_chat_completions",
            )

        banana_cases = apply_capability_expectations(
            banana_variant_cases(
                "resolution",
                model_template="gemini-3.1-flash-image",
                include_cross_control=False,
            ),
            family="banana",
            model="gemini-3.1-flash-image",
            route_profile="google_ai_studio",
            api_form="gemini_interactions",
        )
        self.assertTrue(
            all(case.metadata["expectation"] == "supported" for case in banana_cases)
        )

        image_snapshot = capability_profile_snapshot(
            "image",
            "gpt-image-2",
            "gpt-image-2",
            [case.name for case in gpt_cases],
            route_profile="vendor_direct",
        )
        self.assertIn("size", image_snapshot["supported_parameters"])
        self.assertIn(
            "reject_non_multiple_of_16",
            image_snapshot["unsupported_profiles"],
        )


if __name__ == "__main__":
    unittest.main()
