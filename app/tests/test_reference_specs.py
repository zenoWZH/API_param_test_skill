from __future__ import annotations

import copy
import unittest

from lib.config import SUPPORTED_API_FORMS, SUPPORTED_MODEL_FAMILIES, load_config
from lib.deepseek_params import build_request
from lib.reference_specs import (
    default_reference_source_for_family,
    default_reference_source_for_model,
    family_for_reference,
    get_reference_source,
    list_reference_sources,
    load_model_capability_profile,
    load_model_capability_profiles,
    reference_param_rows,
    test_profiles_for_reference as reference_test_profiles,
)


class ReferenceSpecTest(unittest.TestCase):
    @staticmethod
    def _inject_provider(
        config: dict,
        name: str,
        models: dict,
    ) -> None:
        """Add a minimal self-contained provider fixture (no network access)."""
        config.setdefault("providers", {})[name] = {
            "name": name,
            "label": name,
            "base_url": "http://127.0.0.1:9/v1",
            "backend": "openai_compatible",
            "default_transport": "chat_completions",
            "api_interfaces": {
                "chat_completions": {"path": "/chat/completions", "auth": "bearer"}
            },
            "api_key_env": "DUMMY_REFERENCE_SPEC_TEST_KEY",
            "models": models,
        }

    @classmethod
    def _inject_aliyun_maas(cls, config: dict) -> None:
        cls._inject_provider(
            config,
            "aliyun_maas",
            {
                "default": "glm-5.2",
                "candidates": ["glm-5.2", "deepseek-v4-pro"],
                "families": {"glm-5.2": "glm", "deepseek-v4-pro": "deepseek"},
                "routes": {
                    "glm-5.2": {
                        "aliyun_maas": {"api_forms": {"openai_chat_completions": {}}}
                    },
                    "deepseek-v4-pro": {
                        "aliyun_maas": {"api_forms": {"openai_chat_completions": {}}}
                    },
                },
                "default_routes": {
                    "glm-5.2": "aliyun_maas",
                    "deepseek-v4-pro": "aliyun_maas",
                },
                "default_api_forms": {
                    "glm-5.2": {"aliyun_maas": "openai_chat_completions"},
                    "deepseek-v4-pro": {"aliyun_maas": "openai_chat_completions"},
                },
            },
        )

    @classmethod
    def _inject_moonshot_official_k3(cls, config: dict) -> None:
        cls._inject_provider(
            config,
            "moonshot_official_k3",
            {
                "default": "kimi-k3",
                "candidates": ["kimi-k3"],
                "families": {"kimi-k3": "kimi"},
                "routes": {
                    "kimi-k3": {
                        "vendor_direct": {
                            "api_forms": {
                                "openai_chat_completions": {
                                    "reference_source": "kimi_k3_openai_compat"
                                }
                            }
                        }
                    }
                },
                "default_routes": {"kimi-k3": "vendor_direct"},
                "default_api_forms": {
                    "kimi-k3": {"vendor_direct": "openai_chat_completions"}
                },
            },
        )

    def test_route_wrapper_inherits_contract_without_inheriting_route_identity(
        self,
    ) -> None:
        direct = get_reference_source("kimi_k3_openai_compat")
        dynamic = get_reference_source("kimi_k3_dynamic_aggregator")
        self.assertEqual(dynamic["params"], direct["params"])
        self.assertEqual(dynamic["test_profiles"], direct["test_profiles"])
        self.assertEqual(dynamic["model_family"], "kimi")
        self.assertEqual(dynamic["api_form"], "openai_chat_completions")
        self.assertEqual(dynamic["route_profile"], "dynamic_aggregator")
        self.assertEqual(dynamic["contract_reference_source"], "kimi_k3_openai_compat")
        self.assertEqual(dynamic["certification_scope"], "adapter_only")
        self.assertTrue(dynamic["route_stability_required"])

        capability = load_model_capability_profile(
            "text",
            "kimi",
            "kimi-k3",
            route_profile="dynamic_aggregator",
            api_form="openai_chat_completions",
        )
        self.assertEqual(capability["profile_status"], "registered")
        self.assertEqual(capability["certification_scope"], "adapter_only")
        self.assertTrue(capability["route_stability_required"])

    def test_every_reference_source_declares_exact_family_form_and_route(self) -> None:
        for source in list_reference_sources():
            with self.subTest(source=source["id"]):
                self.assertIn(source["model_family"], SUPPORTED_MODEL_FAMILIES)
                self.assertNotIn(source["model_family"], {"openai", "aliyun"})
                self.assertIn(source["api_form"], SUPPORTED_API_FORMS)
                self.assertTrue(source["route_profile"])

    def test_capability_reference_sources_match_family_route_and_form(self) -> None:
        payload = load_model_capability_profiles()
        for modality_cfg in (payload.get("modalities") or {}).values():
            for family, family_cfg in (modality_cfg.get("families") or {}).items():
                for route, route_cfg in (
                    family_cfg.get("route_profiles") or {}
                ).items():
                    for api_form, form_cfg in (
                        route_cfg.get("api_forms") or {}
                    ).items():
                        sources = list(form_cfg.get("reference_sources") or [])
                        default_source = form_cfg.get("default_reference_source")
                        if default_source:
                            sources.append(default_source)
                        comparison_source = form_cfg.get("comparison_reference_source")
                        if comparison_source:
                            sources.append(comparison_source)
                        for model_cfg in (
                            form_cfg.get("model_profiles") or {}
                        ).values():
                            sources.extend(model_cfg.get("reference_sources") or [])
                            if model_cfg.get("default_reference_source"):
                                sources.append(model_cfg["default_reference_source"])
                            if model_cfg.get("comparison_reference_source"):
                                sources.append(model_cfg["comparison_reference_source"])
                        for source_id in set(sources):
                            with self.subTest(
                                family=family,
                                route=route,
                                api_form=api_form,
                                source=source_id,
                            ):
                                source = get_reference_source(str(source_id))
                                self.assertEqual(source["model_family"], family)
                                self.assertEqual(source["route_profile"], route)
                                self.assertEqual(source["api_form"], api_form)

    def test_provider_model_can_override_family_default_reference_source(self) -> None:
        config = copy.deepcopy(load_config())
        self._inject_aliyun_maas(config)
        provider = config["providers"]["aliyun_maas"]
        form_cfg = provider["models"]["routes"]["glm-5.2"]["aliyun_maas"]["api_forms"][
            "openai_chat_completions"
        ]
        form_cfg["reference_source"] = "aliyun_glm5_openai_compat"

        self.assertEqual(
            default_reference_source_for_model(config, "glm", "glm-5.2", "aliyun_maas"),
            "aliyun_glm5_openai_compat",
        )
        self.assertEqual(
            default_reference_source_for_model(
                config, "deepseek", "deepseek-v4-pro", "aliyun_maas"
            ),
            "aliyun_deepseek_v4_openai_compat",
        )

        form_cfg["reference_source"] = "not-a-reference-source"
        with self.assertRaisesRegex(KeyError, "not found"):
            default_reference_source_for_model(config, "glm", "glm-5.2", "aliyun_maas")

    def test_aliyun_series_sources_resolve_only_documented_profiles(self) -> None:
        config = load_config()
        expected_profiles = {
            "aliyun_glm5_openai_compat": {
                "aliyun_reasoning_high",
                "aliyun_top_k",
                "aliyun_json_object",
                "aliyun_tool_stream",
            },
            "aliyun_kimi_k2_6_openai_compat": {
                "aliyun_disable_thinking",
                "aliyun_preserve_thinking",
                "aliyun_tools",
            },
            "aliyun_kimi_k2_7_code_openai_compat": {
                "aliyun_enable_thinking",
                "aliyun_preserve_thinking",
                "aliyun_tools",
            },
            "aliyun_deepseek_v3_2_openai_compat": {
                "aliyun_enable_thinking",
                "aliyun_disable_thinking",
                "aliyun_tools",
            },
            "aliyun_deepseek_v4_openai_compat": {
                "aliyun_reasoning_high",
                "aliyun_reasoning_max",
                "aliyun_tools",
            },
        }
        expected_families = {
            "aliyun_glm5_openai_compat": "glm",
            "aliyun_kimi_k2_6_openai_compat": "kimi",
            "aliyun_kimi_k2_7_code_openai_compat": "kimi",
            "aliyun_deepseek_v3_2_openai_compat": "deepseek",
            "aliyun_deepseek_v4_openai_compat": "deepseek",
        }

        for source, required in expected_profiles.items():
            with self.subTest(source=source):
                family = expected_families[source]
                self.assertEqual(family_for_reference(source), family)
                profiles = reference_test_profiles(source)
                self.assertTrue(required.issubset(profiles))
                for profile in profiles:
                    request = build_request(
                        config,
                        "compatibility_profiles",
                        profile,
                        model_family_override=family,
                        api_form_override="openai_chat_completions",
                        route_profile_override="aliyun_maas",
                        reference_source=source,
                    )
                    self.assertEqual(request.metadata["model_family"], family)
                    self.assertEqual(
                        request.metadata["api_form"], "openai_chat_completions"
                    )
                    self.assertEqual(request.metadata["route_profile"], "aliyun_maas")
                    self.assertEqual(request.metadata["transport"], "chat_completions")
                    self.assertIn("messages", request.body)

        kimi_code_profiles = reference_test_profiles(
            "aliyun_kimi_k2_7_code_openai_compat"
        )
        self.assertNotIn("aliyun_disable_thinking", kimi_code_profiles)
        self.assertNotIn("aliyun_top_k", kimi_code_profiles)
        deepseek_v3_profiles = reference_test_profiles(
            "aliyun_deepseek_v3_2_openai_compat"
        )
        self.assertNotIn("aliyun_reasoning_high", deepseek_v3_profiles)
        self.assertNotIn("aliyun_json_object", deepseek_v3_profiles)

        kimi_rows = {
            row["parameter"]: row
            for row in reference_param_rows("aliyun_kimi_k2_6_openai_compat")
        }
        deepseek_rows = {
            row["parameter"]: row
            for row in reference_param_rows("aliyun_deepseek_v3_2_openai_compat")
        }
        self.assertEqual(kimi_rows["top_k"]["official"], "unsupported")
        self.assertEqual(kimi_rows["n"]["official"], "unsupported")
        self.assertEqual(kimi_rows["response_format"]["official"], "unsupported")
        self.assertEqual(deepseek_rows["top_k"]["official"], "unsupported")
        self.assertEqual(deepseek_rows["response_format"]["official"], "unsupported")

    def test_gpt_chat_source_is_independent_from_glm_extensions(self) -> None:
        self.assertEqual(default_reference_source_for_family("gpt"), "openai_chat_base")
        self.assertEqual(family_for_reference("openai_chat_base"), "gpt")
        rows = {
            row["parameter"]: row for row in reference_param_rows("openai_chat_base")
        }

        self.assertIn("tools", rows)
        self.assertNotIn("thinking.type", rows)
        self.assertNotIn("reasoning_effort", rows)
        self.assertNotIn("user_id", rows)
        config = load_config()
        built = build_request(
            config,
            "compatibility_profiles",
            "sampling_non_thinking",
            model_family_override="gpt",
        )
        self.assertNotIn("thinking", built.body)
        self.assertEqual(built.body["temperature"], 0.7)

    def test_glm_5_2_source_covers_reasoning_levels_and_preserved_tools(self) -> None:
        rows = {
            row["parameter"]: row for row in reference_param_rows("glm_openai_compat")
        }
        profiles = reference_test_profiles("glm_openai_compat")

        self.assertEqual(len(rows), 18)
        self.assertEqual(len(profiles), 23)
        self.assertEqual(
            rows["reasoning_effort"]["test_profiles"],
            [
                "glm_reasoning_none",
                "glm_reasoning_minimal",
                "glm_reasoning_low",
                "glm_reasoning_medium",
                "glm_reasoning_high",
                "glm_reasoning_xhigh",
                "glm_reasoning_max",
                "glm_tool_calls_thinking",
            ],
        )
        self.assertEqual(
            rows["response.reasoning_content"]["test_profiles"],
            [
                "glm_thinking_enabled",
                "glm_thinking_disabled",
                "glm_reasoning_none",
                "glm_reasoning_minimal",
                "glm_reasoning_low",
                "glm_reasoning_medium",
                "glm_reasoning_high",
                "glm_reasoning_xhigh",
                "glm_reasoning_max",
                "glm_tool_calls_thinking",
            ],
        )

        config = load_config()
        preserved = build_request(
            config,
            "compatibility_profiles",
            "glm_clear_thinking",
            overrides={"model": "glm-5.2"},
            model_family_override="glm",
            enforce_model_capabilities=False,
        )
        self.assertFalse(preserved.body["thinking"]["clear_thinking"])
        tool = build_request(
            config,
            "compatibility_profiles",
            "glm_tool_calls_thinking",
            overrides={"model": "glm-5.2"},
            model_family_override="glm",
            enforce_model_capabilities=False,
        )
        self.assertFalse(tool.body["thinking"]["clear_thinking"])
        self.assertEqual(tool.body["reasoning_effort"], "max")
        self.assertTrue(tool.metadata["pass_reasoning_content"])
        self.assertTrue(tool.metadata["multi_turn"])

    def test_kimi_k3_uses_dedicated_official_reference_source(self) -> None:
        config = load_config()
        self._inject_moonshot_official_k3(config)
        self.assertEqual(
            default_reference_source_for_model(
                config,
                "kimi",
                "kimi-k3",
                "moonshot_official_k3",
            ),
            "kimi_k3_openai_compat",
        )
        self.assertEqual(
            family_for_reference("kimi_k3_openai_compat"),
            "kimi",
        )
        profiles = reference_test_profiles("kimi_k3_openai_compat")
        self.assertIn("kimi_k3_reasoning_low", profiles)
        self.assertIn("kimi_k3_reasoning_high", profiles)
        self.assertIn("kimi_k3_reasoning_max", profiles)
        self.assertIn("kimi_k3_preserved_thinking", profiles)
        self.assertIn("kimi_k3_prompt_cache_key", profiles)
        self.assertIn("kimi_k3_dynamic_tools", profiles)

        rows = {
            row["parameter"]: row
            for row in reference_param_rows("kimi_k3_openai_compat")
        }
        self.assertEqual(
            rows["messages[].reasoning_content"]["test_profiles"],
            ["kimi_k3_preserved_thinking"],
        )
        self.assertEqual(
            rows["prompt_cache_key"]["test_profiles"],
            ["kimi_k3_prompt_cache_key"],
        )
        self.assertEqual(
            rows["messages[].tools"]["test_profiles"],
            ["kimi_k3_dynamic_tools"],
        )

    def test_kimi_k3_profiles_build_exact_extension_payloads(self) -> None:
        config = load_config()
        positive_profiles = [
            "kimi_k3_stream",
            "kimi_k3_stream_usage",
            "kimi_k3_reasoning_low",
            "kimi_k3_reasoning_high",
            "kimi_k3_reasoning_max",
            "kimi_k3_preserved_thinking",
            "kimi_k3_prompt_cache_key",
            "kimi_k3_dynamic_tools",
        ]
        for profile in positive_profiles:
            with self.subTest(profile=profile):
                built = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    overrides={"model": "kimi-k3"},
                    model_family_override="kimi",
                    enforce_model_capabilities=False,
                )
                self.assertEqual(built.body["temperature"], 1.0)
                self.assertEqual(built.body["top_p"], 0.95)
                self.assertIn(
                    built.body["reasoning_effort"],
                    {"low", "high", "max"},
                )
                self.assertEqual(built.body["max_completion_tokens"], 512)

        preserved = build_request(
            config,
            "compatibility_profiles",
            "kimi_k3_preserved_thinking",
            overrides={"model": "kimi-k3"},
            model_family_override="kimi",
            enforce_model_capabilities=False,
        )
        self.assertEqual(
            preserved.body["messages"][1]["reasoning_content"],
            "I'll start by listing five numbers: 473, 921, 235, 215, 222, and I'll tell you the first three.",
        )

        dynamic = build_request(
            config,
            "compatibility_profiles",
            "kimi_k3_dynamic_tools",
            overrides={"model": "kimi-k3"},
            model_family_override="kimi",
            enforce_model_capabilities=False,
        )
        system_message = dynamic.body["messages"][0]
        self.assertEqual(system_message["role"], "system")
        self.assertEqual(system_message["content"], "")
        self.assertEqual(
            system_message["tools"][0]["function"]["name"],
            "get_weather",
        )
        self.assertNotIn("tools", dynamic.body)

    def test_deepseek_parameters_have_explicit_matrix_coverage(self) -> None:
        rows = {row["parameter"]: row for row in reference_param_rows("deepseek_chat")}
        profiles = reference_test_profiles("deepseek_chat")

        self.assertEqual(len(rows), 22)
        self.assertEqual(len(profiles), 18)
        self.assertEqual(rows["messages"]["coverage_mode"], "all_profiles")
        self.assertEqual(rows["messages"]["test_profiles"], profiles)
        self.assertEqual(rows["max_tokens"]["test_profiles"], ["deepseek_max_tokens"])
        self.assertEqual(rows["user_id"]["test_profiles"], ["deepseek_user_id"])
        self.assertEqual(
            rows["frequency_penalty"]["test_profiles"],
            ["deepseek_frequency_penalty"],
        )
        self.assertEqual(rows["messages[].prefix"]["coverage_mode"], "not_tested")
        self.assertEqual(
            rows["tools"]["test_profiles"], ["tool_calls", "tool_calls_thinking"]
        )
        self.assertEqual(rows["tool_choice"]["test_profiles"], ["tool_choice_required"])
        self.assertEqual(
            rows["response.reasoning_content"]["test_profiles"],
            [
                "thinking_low",
                "thinking_enabled",
                "thinking_max",
                "thinking_disabled",
                "tool_calls_thinking",
            ],
        )

    def test_deepseek_v4_flash_0731_uses_independent_xinyun_profile(self) -> None:
        source = get_reference_source("deepseek_xinyunai_v4_flash_0731_openai_compat")
        self.assertEqual(source["model_family"], "deepseek")
        self.assertEqual(source["route_profile"], "dynamic_aggregator")
        self.assertEqual(source["api_form"], "openai_chat_completions")
        self.assertEqual(source["contract_reference_source"], "deepseek_chat")
        self.assertEqual(source["certification_scope"], "adapter_only")
        self.assertIn("thinking_low", source["test_profiles"])
        self.assertIn("deepseek_json_output_256", source["test_profiles"])
        self.assertNotIn("json_output", source["test_profiles"])

        base = load_model_capability_profile(
            "text",
            "deepseek",
            "deepseek-v4-flash",
            route_profile="dynamic_aggregator",
            api_form="openai_chat_completions",
        )
        capability = load_model_capability_profile(
            "text",
            "deepseek",
            "deepseek-v4-flash-0731",
            route_profile="dynamic_aggregator",
            api_form="openai_chat_completions",
        )
        self.assertEqual(base["profile_status"], "registered")
        self.assertEqual(base["profile_id"], "deepseek-v4-flash")
        self.assertEqual(
            base["model_api_profile_id"],
            "deepseek/deepseek-v4-flash@dynamic_aggregator/openai_chat_completions",
        )
        self.assertEqual(
            base["allowed_reference_sources"],
            ["deepseek_dynamic_aggregator"],
        )
        self.assertEqual(capability["profile_status"], "registered")
        self.assertEqual(capability["profile_id"], "deepseek-v4-flash-0731")
        self.assertEqual(
            capability["model_api_profile_id"],
            "deepseek/deepseek-v4-flash-0731@dynamic_aggregator/openai_chat_completions",
        )
        self.assertEqual(
            capability["allowed_reference_sources"],
            ["deepseek_xinyunai_v4_flash_0731_openai_compat"],
        )
        self.assertEqual(
            capability["default_reference_source"],
            "deepseek_xinyunai_v4_flash_0731_openai_compat",
        )
        self.assertEqual(
            capability["comparison_reference_source"],
            "deepseek_xinyunai_v4_flash_0731_openai_compat",
        )
        self.assertNotEqual(
            base["model_api_profile_id"], capability["model_api_profile_id"]
        )

        direct = load_model_capability_profile(
            "text",
            "deepseek",
            "deepseek-v4-flash-0731",
            route_profile="vendor_direct",
            api_form="openai_chat_completions",
        )
        self.assertEqual(direct["profile_status"], "unregistered_model_profile")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            load_model_capability_profile(
                "text",
                "deepseek",
                "deepseek-v4-flash",
                route_profile="dynamic_aggregator",
                api_form="openai_chat_completions",
                reference_source="deepseek_xinyunai_v4_flash_0731_openai_compat",
            )

    def test_deepseek_v4_effort_levels_follow_current_model_contract(self) -> None:
        config = load_config()
        low = build_request(
            config,
            "compatibility_profiles",
            "thinking_low",
            overrides={"model": "deepseek-v4-flash-0731"},
            model_family_override="deepseek",
            enforce_model_capabilities=False,
        )
        flash_xhigh = build_request(
            config,
            "compatibility_profiles",
            "thinking_enabled",
            overrides={
                "model": "deepseek-v4-flash-0731",
                "reasoning_effort": "xhigh",
            },
            model_family_override="deepseek",
            enforce_model_capabilities=False,
        )
        pro_xhigh = build_request(
            config,
            "compatibility_profiles",
            "thinking_enabled",
            overrides={"model": "deepseek-v4-pro", "reasoning_effort": "xhigh"},
            model_family_override="deepseek",
            enforce_model_capabilities=False,
        )
        self.assertEqual(low.body["reasoning_effort"], "low")
        self.assertEqual(flash_xhigh.body["reasoning_effort"], "high")
        self.assertEqual(pro_xhigh.body["reasoning_effort"], "max")

    def test_aliyun_deepseek_v4_structured_output_is_supported(self) -> None:
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("aliyun_deepseek_v4_openai_compat")
        }
        profiles = reference_test_profiles("aliyun_deepseek_v4_openai_compat")

        self.assertEqual(rows["response_format"]["official"], "supported")
        self.assertEqual(
            rows["response_format"]["test_profiles"],
            ["aliyun_json_object"],
        )
        self.assertIn("aliyun_json_object", profiles)

    def test_gemini_latest_reasoning_and_tools_use_dedicated_profiles(self) -> None:
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("gemini_openai_compat")
        }
        profiles = reference_test_profiles("gemini_openai_compat")
        config = load_config()

        self.assertEqual(rows["tools"]["test_profiles"], ["gemini_tools"])
        self.assertEqual(
            rows["tool_choice"]["test_profiles"],
            ["gemini_tool_choice_auto"],
        )
        reasoning_profiles = [
            "gemini_reasoning_minimal",
            "gemini_reasoning_low",
            "gemini_reasoning_medium",
            "gemini_reasoning_high",
        ]
        self.assertEqual(
            rows["reasoning_effort"]["test_profiles"],
            reasoning_profiles,
        )
        self.assertNotIn("thinking_enabled", profiles)
        for profile, effort in zip(
            reasoning_profiles,
            ("minimal", "low", "medium", "high"),
            strict=True,
        ):
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="gemini",
                )
                self.assertEqual(request.body["reasoning_effort"], effort)
                self.assertNotIn("thinking", request.body)

        native_rows = {
            row["parameter"]: row
            for row in reference_param_rows("gemini_native_generate_content")
        }
        native_profiles = [
            "gemini_native_thinking_minimal",
            "gemini_native_thinking_low",
            "gemini_native_thinking_medium",
            "gemini_native_thinking_high",
        ]
        self.assertEqual(
            native_rows["generationConfig.thinkingConfig"]["test_profiles"],
            native_profiles,
        )
        self.assertEqual(
            native_rows["response.candidates[].content.parts[].thought"][
                "test_profiles"
            ],
            [
                "gemini_native_thinking_medium",
                "gemini_native_thinking_high",
            ],
        )
        for profile, level in zip(
            native_profiles,
            ("MINIMAL", "LOW", "MEDIUM", "HIGH"),
            strict=True,
        ):
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="gemini",
                )
                self.assertEqual(
                    request.body["generationConfig"]["thinkingConfig"]["thinkingLevel"],
                    level,
                )
                self.assertTrue(
                    request.body["generationConfig"]["thinkingConfig"][
                        "includeThoughts"
                    ]
                )

        chat_count = build_request(
            config,
            "compatibility_profiles",
            "gemini_n",
            model_family_override="gemini",
        )
        self.assertEqual(chat_count.body["n"], 2)
        native_count = build_request(
            config,
            "compatibility_profiles",
            "gemini_native_candidate_count",
            model_family_override="gemini",
        )
        self.assertEqual(
            native_count.body["generationConfig"]["candidateCount"],
            2,
        )

    def test_claude_native_messages_is_default_and_profiles_resolve(self) -> None:
        self.assertEqual(
            default_reference_source_for_family("claude"),
            "claude_native_messages",
        )
        self.assertEqual(family_for_reference("claude_native_messages"), "claude")
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("claude_native_messages")
        }
        profiles = reference_test_profiles("claude_native_messages")
        config = load_config()

        self.assertEqual(
            rows["tools"]["test_profiles"],
            ["claude_native_tools", "claude_native_tool_choice_auto"],
        )
        self.assertEqual(
            rows["tool_choice"]["test_profiles"],
            ["claude_native_tool_choice_auto"],
        )
        self.assertIn("claude_native_thinking_adaptive", profiles)
        self.assertIn("claude_native_stream", profiles)
        effort_profiles = [
            "claude_native_effort_low",
            "claude_native_effort_medium",
            "claude_native_effort_high",
            "claude_native_effort_xhigh",
            "claude_native_effort_max",
        ]
        self.assertEqual(
            rows["output_config.effort"]["test_profiles"],
            effort_profiles,
        )
        for profile in profiles:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="claude",
                )
                self.assertEqual(request.metadata["model_family"], "claude")
                self.assertEqual(request.metadata["transport"], "claude_messages")
                self.assertEqual(request.metadata["request_endpoint"], "/messages")
                self.assertIn("messages", request.body)
        for profile, effort in zip(
            effort_profiles,
            ("low", "medium", "high", "xhigh", "max"),
            strict=True,
        ):
            request = build_request(
                config,
                "compatibility_profiles",
                profile,
                model_family_override="claude",
            )
            self.assertEqual(request.body["thinking"]["type"], "adaptive")
            self.assertEqual(request.body["output_config"]["effort"], effort)

    def test_claude_fable_native_messages_is_default_and_profiles_resolve(self) -> None:
        self.assertEqual(
            default_reference_source_for_family("claude_fable"),
            "claude_fable_native_messages",
        )
        self.assertEqual(
            family_for_reference("claude_fable_native_messages"),
            "claude_fable",
        )
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("claude_fable_native_messages")
        }
        profiles = reference_test_profiles("claude_fable_native_messages")
        config = load_config()

        self.assertNotIn("claude_native_top_p", profiles)
        self.assertNotIn("claude_native_thinking_disabled", profiles)
        self.assertNotIn("claude_native_thinking_budget", profiles)
        self.assertIn("claude_native_thinking_adaptive", profiles)
        self.assertEqual(rows["top_p"]["coverage_mode"], "not_tested")
        self.assertEqual(rows["thinking.budget_tokens"]["coverage_mode"], "not_tested")
        self.assertEqual(
            rows["output_config.effort"]["test_profiles"],
            [
                "claude_fable_thinking_effort_low",
                "claude_fable_thinking_effort_medium",
                "claude_fable_thinking_effort_high",
            ],
        )
        for profile in profiles:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="claude_fable",
                )
                self.assertEqual(request.metadata["transport"], "claude_messages")
                self.assertNotIn("top_p", request.body)
                thinking = request.body.get("thinking")
                if isinstance(thinking, dict):
                    self.assertEqual(thinking.get("type"), "adaptive")
                    self.assertNotIn("budget_tokens", thinking)
                if profile.startswith("claude_fable_thinking_effort_"):
                    effort = profile.rsplit("_", 1)[-1]
                    self.assertEqual(request.body["output_config"]["effort"], effort)
                    self.assertEqual(request.body["thinking"]["type"], "adaptive")

    def test_claude_openai_compat_profiles_resolve_as_optional_source(self) -> None:
        self.assertEqual(
            family_for_reference("claude_openai_compat"),
            "claude",
        )
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("claude_openai_compat")
        }
        profiles = reference_test_profiles("claude_openai_compat")
        config = load_config()

        self.assertEqual(
            rows["tools"]["test_profiles"],
            ["claude_tools", "claude_parallel_tool_calls"],
        )
        self.assertEqual(
            rows["tool_choice"]["test_profiles"],
            ["claude_tool_choice_auto", "claude_parallel_tool_calls"],
        )
        self.assertIn("claude_thinking_adaptive", profiles)
        self.assertIn("basic_stream", profiles)
        for profile in profiles:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="claude",
                )
                self.assertEqual(request.metadata["model_family"], "claude")
                self.assertIn("messages", request.body)

    def test_deepseek_deprecated_probe_is_actually_sent(self) -> None:
        config = load_config()

        frequency = build_request(
            config,
            "compatibility_profiles",
            "deepseek_frequency_penalty",
            model_family_override="deepseek",
        )
        presence = build_request(
            config,
            "compatibility_profiles",
            "deepseek_presence_penalty",
            model_family_override="deepseek",
        )

        self.assertEqual(frequency.body["frequency_penalty"], 0.2)
        self.assertEqual(presence.body["presence_penalty"], 0.2)
        self.assertNotIn("send_deprecated", frequency.body)

    def test_qwen_openai_compat_is_default_and_native_profiles_resolve(self) -> None:
        self.assertEqual(
            default_reference_source_for_family("qwen"),
            "qwen_openai_compat",
        )
        self.assertEqual(family_for_reference("qwen_openai_compat"), "qwen")
        rows = {
            row["parameter"]: row for row in reference_param_rows("qwen_openai_compat")
        }
        profiles = reference_test_profiles("qwen_openai_compat")
        config = load_config()

        self.assertIn("qwen_n", profiles)
        self.assertIn("qwen_logprobs", profiles)
        self.assertIn("qwen_response_format", profiles)
        self.assertEqual(rows["n"]["test_profiles"], ["qwen_n"])
        self.assertEqual(rows["logprobs"]["test_profiles"], ["qwen_logprobs"])
        self.assertEqual(rows["top_logprobs"]["test_profiles"], ["qwen_logprobs"])
        self.assertEqual(
            rows["response_format"]["test_profiles"], ["qwen_response_format"]
        )
        self.assertEqual(
            rows["response.reasoning_content"]["test_profiles"],
            [
                "qwen_thinking_enabled",
                "qwen_thinking_disabled",
                "qwen_thinking_budget",
                "qwen_preserve_thinking",
            ],
        )
        self.assertEqual(
            rows["vl_high_resolution_images"]["coverage_mode"], "not_tested"
        )
        self.assertEqual(
            rows["header.X-DashScope-DataInspection"]["coverage_mode"],
            "not_tested",
        )

        for profile in profiles:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="qwen",
                )
                self.assertEqual(request.metadata["model_family"], "qwen")
                self.assertEqual(request.metadata["transport"], "chat_completions")
                self.assertNotIn("thinking", request.body)
                self.assertNotIn("user_id", request.body)
                if profile.startswith("qwen_") or profile in {
                    "basic_stream",
                    "stream_with_usage",
                    "stop_sequences",
                }:
                    # Shared stream/stop profiles are rewritten; dedicated qwen_*
                    # profiles set enable_thinking explicitly.
                    self.assertIn("enable_thinking", request.body)

        native = build_request(
            config,
            "compatibility_profiles",
            "qwen_n",
            model_family_override="qwen",
        )
        self.assertEqual(native.body["n"], 2)
        self.assertEqual(native.body["enable_thinking"], False)

        logprobs = build_request(
            config,
            "compatibility_profiles",
            "qwen_logprobs",
            model_family_override="qwen",
        )
        self.assertTrue(logprobs.body["logprobs"])
        self.assertEqual(logprobs.body["top_logprobs"], 5)
        self.assertEqual(logprobs.body["enable_thinking"], False)

        json_mode = build_request(
            config,
            "compatibility_profiles",
            "qwen_response_format",
            model_family_override="qwen",
        )
        self.assertEqual(json_mode.body["response_format"]["type"], "json_object")
        self.assertEqual(json_mode.body["enable_thinking"], False)

        search = build_request(
            config,
            "compatibility_profiles",
            "qwen_search_options",
            model_family_override="qwen",
        )
        self.assertTrue(search.body["enable_search"])
        self.assertEqual(search.body["search_options"]["search_strategy"], "turbo")

        preserved = build_request(
            config,
            "compatibility_profiles",
            "qwen_preserve_thinking",
            model_family_override="qwen",
        )
        self.assertFalse(preserved.body["stream"])
        self.assertTrue(preserved.body["enable_thinking"])
        self.assertTrue(preserved.body["preserve_thinking"])
        self.assertEqual(preserved.body["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", preserved.body)
        self.assertEqual(
            [message["role"] for message in preserved.body["messages"]],
            ["user", "assistant", "user"],
        )
        self.assertIn(
            "215",
            preserved.body["messages"][1]["reasoning_content"],
        )
        self.assertIn(
            "222",
            preserved.body["messages"][1]["reasoning_content"],
        )

    def test_gemini_vertex_fingerprint_source_and_profiles_resolve(self) -> None:
        self.assertEqual(
            family_for_reference("gemini_vertex_generate_content"),
            "gemini",
        )
        # Vertex fingerprint is optional; AI Studio OpenAI-compat remains default.
        self.assertEqual(
            default_reference_source_for_family("gemini"),
            "gemini_openai_compat",
        )
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("gemini_vertex_generate_content")
        }
        profiles = reference_test_profiles("gemini_vertex_generate_content")
        config = load_config()

        self.assertEqual(
            profiles,
            [
                "gemini_vertex_traffic_type",
                "gemini_vertex_labels",
                "gemini_vertex_service_tier_body",
                "gemini_vertex_request_type_header",
                "gemini_vertex_shared_request_type_header",
            ],
        )
        self.assertEqual(
            rows["usageMetadata.trafficType"]["test_profiles"],
            ["gemini_vertex_traffic_type"],
        )
        self.assertEqual(rows["labels"]["test_profiles"], ["gemini_vertex_labels"])
        self.assertEqual(
            rows["serviceTier"]["test_profiles"],
            ["gemini_vertex_service_tier_body"],
        )
        self.assertEqual(
            rows["header.X-Vertex-AI-LLM-Request-Type"]["test_profiles"],
            ["gemini_vertex_request_type_header"],
        )
        self.assertEqual(
            rows["header.X-Vertex-AI-LLM-Shared-Request-Type"]["test_profiles"],
            ["gemini_vertex_shared_request_type_header"],
        )

        baseline = build_request(
            config,
            "compatibility_profiles",
            "gemini_vertex_traffic_type",
            model_family_override="gemini",
        )
        self.assertEqual(baseline.metadata["transport"], "gemini_generate_content")
        self.assertNotIn("labels", baseline.body)
        self.assertNotIn("request_headers", baseline.metadata)

        labels = build_request(
            config,
            "compatibility_profiles",
            "gemini_vertex_labels",
            model_family_override="gemini",
        )
        self.assertEqual(
            labels.body["labels"],
            {"loadtest_probe": "vertex-source-detect"},
        )

        service_tier = build_request(
            config,
            "compatibility_profiles",
            "gemini_vertex_service_tier_body",
            model_family_override="gemini",
        )
        self.assertEqual(service_tier.body["serviceTier"], "standard")

        request_type = build_request(
            config,
            "compatibility_profiles",
            "gemini_vertex_request_type_header",
            model_family_override="gemini",
        )
        self.assertEqual(
            request_type.metadata["request_headers"],
            {"X-Vertex-AI-LLM-Request-Type": "shared"},
        )

        shared = build_request(
            config,
            "compatibility_profiles",
            "gemini_vertex_shared_request_type_header",
            model_family_override="gemini",
        )
        self.assertEqual(
            shared.metadata["request_headers"],
            {
                "X-Vertex-AI-LLM-Request-Type": "shared",
                "X-Vertex-AI-LLM-Shared-Request-Type": "flex",
            },
        )

        malicious = copy.deepcopy(config)
        malicious["compatibility_profiles"]["gemini_vertex_request_type_header"][
            "request_headers"
        ] = {"authorization": "Bearer attacker"}
        with self.assertRaisesRegex(ValueError, "not allowed"):
            build_request(
                malicious,
                "compatibility_profiles",
                "gemini_vertex_request_type_header",
                model_family_override="gemini",
            )

    def test_gpt5_chat_source_is_optional_and_builds_modern_params(self) -> None:
        self.assertEqual(default_reference_source_for_family("gpt"), "openai_chat_base")
        self.assertEqual(family_for_reference("openai_gpt5_chat"), "gpt")
        profiles = reference_test_profiles("openai_gpt5_chat")
        self.assertIn("gpt5_chat_tools", profiles)
        self.assertNotIn("stop_sequences", profiles)
        rows = {
            row["parameter"]: row for row in reference_param_rows("openai_gpt5_chat")
        }
        self.assertIn("max_completion_tokens", rows)
        self.assertIn("reasoning_effort", rows)
        self.assertNotIn("stop", rows)

        config = load_config()
        tools = build_request(
            config,
            "compatibility_profiles",
            "gpt5_chat_tools",
            model_family_override="gpt",
        )
        self.assertEqual(tools.metadata["transport"], "chat_completions")
        self.assertEqual(tools.body["reasoning_effort"], "none")
        self.assertEqual(tools.body["max_completion_tokens"], 256)
        self.assertNotIn("max_tokens", tools.body)
        self.assertNotIn("stop", tools.body)
        self.assertTrue(tools.metadata["multi_turn"])

        reasoning = build_request(
            config,
            "compatibility_profiles",
            "gpt5_chat_reasoning_low",
            model_family_override="gpt",
        )
        self.assertEqual(reasoning.body["reasoning_effort"], "low")

    def test_openai_gpt56_sources_cover_current_chat_and_responses_contracts(
        self,
    ) -> None:
        chat_profiles = reference_test_profiles("openai_gpt56_chat")
        self.assertIn("gpt5_chat_reasoning_max", chat_profiles)
        self.assertIn("gpt5_chat_reject_temperature", chat_profiles)
        chat_rows = {
            row["parameter"]: row for row in reference_param_rows("openai_gpt56_chat")
        }
        self.assertEqual(chat_rows["temperature"]["official"], "unsupported")
        self.assertEqual(chat_rows["stop"]["official"], "unsupported")

        config = load_config()
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            profile = f"gpt5_chat_reasoning_{effort}"
            request = build_request(
                config,
                "compatibility_profiles",
                profile,
                overrides={"model": "gpt-5.6-sol"},
                model_family_override="gpt",
            )
            self.assertEqual(request.body["reasoning_effort"], effort)

        responses_profiles = reference_test_profiles("openai_gpt56_responses")
        self.assertIn("openai_responses_reasoning_max", responses_profiles)
        self.assertIn("openai_responses_pro_medium", responses_profiles)
        self.assertIn("openai_responses_explicit_cache", responses_profiles)
        self.assertIn(
            "openai_responses_reasoning_context_all_turns",
            responses_profiles,
        )
        response_rows = {
            row["parameter"]: row
            for row in reference_param_rows("openai_gpt56_responses")
        }
        self.assertIn("reasoning.context", response_rows)
        self.assertIn("reasoning.mode", response_rows)
        self.assertIn("prompt_cache_options.mode", response_rows)
        self.assertIn("text.verbosity", response_rows)

        pro = build_request(
            config,
            "compatibility_profiles",
            "openai_responses_pro_medium",
            overrides={"model": "gpt-5.6-sol"},
            model_family_override="gpt",
        )
        self.assertEqual(pro.metadata["transport"], "openai_responses")
        self.assertEqual(pro.body["reasoning"], {"mode": "pro", "effort": "medium"})

        context = build_request(
            config,
            "compatibility_profiles",
            "openai_responses_reasoning_context_all_turns",
            overrides={"model": "gpt-5.6-sol"},
            model_family_override="gpt",
        )
        self.assertEqual(context.body["reasoning"]["context"], "all_turns")

        cache = build_request(
            config,
            "compatibility_profiles",
            "openai_responses_explicit_cache",
            overrides={"model": "gpt-5.6-sol"},
            model_family_override="gpt",
        )
        self.assertEqual(
            cache.body["prompt_cache_options"],
            {"mode": "explicit", "ttl": "30m"},
        )
        self.assertEqual(cache.body["prompt_cache_key"], "param-test-gpt56-sol")

    def test_grok_chat_completions_is_default_and_profiles_resolve(self) -> None:
        self.assertEqual(default_reference_source_for_family("grok"), "grok_responses")
        self.assertEqual(family_for_reference("grok_responses"), "grok")
        self.assertEqual(family_for_reference("grok_chat_completions"), "grok")
        profiles = reference_test_profiles("grok_chat_completions")
        self.assertEqual(
            profiles,
            [
                "grok_stream",
                "grok_stream_usage",
                "grok_max_completion_tokens",
                "grok_reasoning_effort_low",
                "grok_reasoning_effort_medium",
                "grok_reasoning_effort_high",
                "grok_reasoning_effort_none",
                "grok_json",
                "grok_tools",
                "grok_reject_stop",
                "grok_reject_presence_penalty",
            ],
        )
        rows = {
            row["parameter"]: row
            for row in reference_param_rows("grok_chat_completions")
        }
        self.assertIn("max_completion_tokens", rows)
        self.assertIn("reasoning_effort", rows)
        self.assertEqual(rows["stop"]["test_profiles"], ["grok_reject_stop"])
        self.assertEqual(
            rows["presence_penalty"]["test_profiles"],
            ["grok_reject_presence_penalty"],
        )

        config = load_config()
        positive = [
            profile
            for profile in profiles
            if profile
            not in {
                "grok_reasoning_effort_none",
                "grok_reject_stop",
                "grok_reject_presence_penalty",
            }
        ]
        for profile in positive:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="grok",
                )
                self.assertEqual(request.metadata["model_family"], "grok")
                self.assertEqual(request.metadata["transport"], "chat_completions")
                self.assertIn("max_completion_tokens", request.body)
                self.assertNotIn("max_tokens", request.body)
                self.assertNotIn("stop", request.body)
                self.assertIn(
                    request.body.get("reasoning_effort"), {"low", "medium", "high"}
                )

        none_effort = build_request(
            config,
            "compatibility_profiles",
            "grok_reasoning_effort_none",
            model_family_override="grok",
        )
        self.assertEqual(none_effort.body["reasoning_effort"], "none")

        reject_stop = build_request(
            config,
            "compatibility_profiles",
            "grok_reject_stop",
            model_family_override="grok",
        )
        self.assertEqual(reject_stop.body["stop"], ["END"])

        reject_presence = build_request(
            config,
            "compatibility_profiles",
            "grok_reject_presence_penalty",
            model_family_override="grok",
        )
        self.assertEqual(reject_presence.body["presence_penalty"], 0.5)

        tools = build_request(
            config,
            "compatibility_profiles",
            "grok_tools",
            model_family_override="grok",
        )
        self.assertTrue(tools.metadata["multi_turn"])
        self.assertEqual(tools.body["tool_choice"], "required")
        self.assertTrue(tools.body.get("tools"))

    def test_grok_responses_is_default_and_profiles_resolve(self) -> None:
        self.assertEqual(default_reference_source_for_family("grok"), "grok_responses")
        profiles = reference_test_profiles("grok_responses")
        self.assertIn("grok_responses_tools", profiles)
        self.assertIn("grok_responses_reasoning_high", profiles)
        self.assertIn("grok_responses_reasoning_effort_none", profiles)
        self.assertNotIn("grok_responses_reject_stop", profiles)
        rows = {row["parameter"]: row for row in reference_param_rows("grok_responses")}
        self.assertIn("reasoning.effort", rows)
        self.assertIn("max_output_tokens", rows)
        self.assertEqual(rows["stop"]["coverage_mode"], "not_tested")

        config = load_config()
        basic = build_request(
            config,
            "compatibility_profiles",
            "grok_responses_basic",
            model_family_override="grok",
        )
        self.assertEqual(basic.metadata["transport"], "openai_responses")
        self.assertEqual(basic.metadata["request_endpoint"], "/responses")
        self.assertEqual(basic.body["reasoning"]["effort"], "low")
        self.assertNotIn("messages", basic.body)
        self.assertIn("input", basic.body)
        # Positive suite stays on low|medium|high; effort=none is the Responses reject probe.
        positive = [
            profile
            for profile in profiles
            if profile != "grok_responses_reasoning_effort_none"
        ]
        for profile in positive:
            with self.subTest(profile=profile):
                request = build_request(
                    config,
                    "compatibility_profiles",
                    profile,
                    model_family_override="grok",
                )
                effort = (request.body.get("reasoning") or {}).get("effort")
                self.assertIn(effort, {"low", "medium", "high"})

        none_effort = build_request(
            config,
            "compatibility_profiles",
            "grok_responses_reasoning_effort_none",
            model_family_override="grok",
        )
        self.assertEqual(none_effort.body["reasoning"]["effort"], "none")

    def test_gpt_responses_source_builds_native_body(self) -> None:
        self.assertEqual(family_for_reference("openai_responses"), "gpt")
        profiles = reference_test_profiles("openai_responses")
        self.assertIn("openai_responses_tools", profiles)
        rows = {
            row["parameter"]: row for row in reference_param_rows("openai_responses")
        }
        self.assertIn("input", rows)
        self.assertIn("text.format", rows)

        config = load_config()
        basic = build_request(
            config,
            "compatibility_profiles",
            "openai_responses_basic",
            overrides={"model": "gpt-5.4"},
            model_family_override="gpt",
        )
        self.assertEqual(basic.metadata["transport"], "openai_responses")
        self.assertEqual(basic.metadata["request_endpoint"], "/responses")
        self.assertEqual(basic.body["model"], "gpt-5.4")
        self.assertEqual(basic.body["reasoning"]["effort"], "low")
        self.assertEqual(basic.body["store"], False)
        self.assertIsInstance(basic.body["input"], str)
        self.assertNotIn("messages", basic.body)

        tools = build_request(
            config,
            "compatibility_profiles",
            "openai_responses_tools",
            overrides={"model": "gpt-5.4"},
            model_family_override="gpt",
        )
        self.assertEqual(tools.body["tools"][0]["type"], "function")
        self.assertEqual(tools.body["tools"][0]["name"], "get_weather")
        self.assertNotIn("function", tools.body["tools"][0])
        self.assertTrue(tools.metadata["multi_turn"])


if __name__ == "__main__":
    unittest.main()
