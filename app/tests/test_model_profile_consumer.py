from __future__ import annotations

import copy
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import lib.reference_specs as reference_specs
import lib.model_profile_catalog as model_profile_catalog

from lib.cache_suite import run_cache_suite
from lib.config import (
    get_image_model_config,
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    load_config,
)
from lib.job_spec import (
    load_job_spec,
    make_job_spec,
    resolve_image_plan,
    resolve_job_model_profile_snapshot,
)
from lib.deepseek_params import build_request
from lib.model_profile_catalog import (
    binding_from_database_snapshot,
    capability_profile_from_database_snapshot,
    catalog_metadata,
    get_model_profile_catalog,
    resolve_runtime_parameter_config,
)
from lib.reference_specs import (
    load_model_capability_profile,
    load_reference_specs,
    pressure_profiles_for_model,
    test_profiles_for_reference as reference_test_profiles,
)
from scripts.web_console import app


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
PACKAGE_ROOT = REPO_ROOT / "packages" / "model-profile-db"


class _CacheSmokeClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_completion(self, body: dict[str, object]) -> SimpleNamespace:
        self.calls += 1
        prompt_tokens = 5000
        cached = 4900 if self.calls % 2 == 0 else 0
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "total_tokens": prompt_tokens + 10,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": prompt_tokens - cached,
        }
        return SimpleNamespace(
            timestamp=time.time(),
            success=True,
            status_code=200,
            latency_ms=50,
            ttft_ms=None,
            text="ok",
            response_length=2,
            finish_reason="stop",
            usage=usage,
            error_type=None,
            failure_classification=None,
            cache_headers={},
        )


class ModelProfileConsumerCutoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.provider = "yibu"
        self.model = "deepseek-v4-flash"
        self.config["active_provider"] = self.provider
        self.config["providers"][self.provider]["models"]["default"] = self.model
        self.family = get_model_family(
            self.config, self.model, self.provider
        )
        self.route = get_model_route_profile(
            self.config, self.model, self.provider
        )
        self.api_form = get_model_api_form(
            self.config,
            self.model,
            self.provider,
            route_profile=self.route,
        )

    def _parameter_config(self) -> dict[str, object]:
        return resolve_runtime_parameter_config(
            self.config,
            self.provider,
            self.model,
            self.family,
            self.route,
            self.api_form,
        )

    def test_runtime_text_alias_reuses_one_source_local_snapshot(self) -> None:
        config = copy.deepcopy(self.config)
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

        parameter = resolve_runtime_parameter_config(
            config,
            "yibu",
            alias,
            "deepseek",
            "dynamic_aggregator",
            "openai_chat_completions",
        )
        snapshot = parameter["model_profile_database"]
        capability = capability_profile_from_database_snapshot(snapshot)

        self.assertEqual(parameter["source_id"], "aliyun_maas")
        self.assertEqual(
            parameter["contract_id"], "aliyun_deepseek_v4_openai_compat"
        )
        self.assertEqual(capability["model"], alias)
        self.assertEqual(capability["canonical_model_slug"], canonical)
        self.assertEqual(capability["profile_id"], parameter["profile_id"])
        self.assertEqual(capability["interface_id"], parameter["interface_id"])
        self.assertEqual(
            capability["reference_contract_id"], parameter["contract_id"]
        )

        runtime_config = copy.deepcopy(config)
        runtime_config["_model_profile_database"] = copy.deepcopy(snapshot)
        built = build_request(
            runtime_config,
            "throughput_profiles",
            "standard_short",
            reference_source=str(parameter["contract_id"]),
        )
        self.assertEqual(built.body["model"], alias)
        self.assertEqual(built.metadata["requested_model"], alias)
        self.assertEqual(
            built.metadata["capability_profile_id"], parameter["profile_id"]
        )

    def test_runtime_image_alias_reuses_canonical_profile_snapshot(self) -> None:
        config = copy.deepcopy(self.config)
        provider = config["providers"]["gemini"]
        canonical = "gemini-3.1-flash-image"
        alias = "vendor-banana-image"
        canonical_row = next(
            row
            for row in provider["image"]["models"]
            if row["id"] == canonical
        )
        alias_row = copy.deepcopy(canonical_row)
        alias_row["id"] = alias
        alias_row["reference_model_id"] = canonical
        provider["image"]["models"].append(alias_row)
        provider["image"]["default"] = alias

        plan = resolve_image_plan(
            config,
            {
                "image_plan": {
                    "suite": "smoke",
                    "no_cross_control": True,
                }
            },
            "gemini",
            alias,
            60,
        )
        snapshot = plan["model_profile_database"]
        capability = capability_profile_from_database_snapshot(snapshot)

        self.assertEqual(plan["model"], alias)
        self.assertEqual(capability["model"], alias)
        self.assertEqual(capability["canonical_model_slug"], canonical)
        self.assertEqual(capability["source_id"], "google_ai_studio")
        self.assertEqual(
            plan["model_capability_profile"]["profile_id"],
            capability["profile_id"],
        )
        self.assertEqual(
            plan["model_capability_profile"]["reference_contract_id"],
            capability["reference_contract_id"],
        )

    def test_capability_snapshot_never_requeries_current_catalog(self) -> None:
        parameter = self._parameter_config()
        database = copy.deepcopy(parameter["model_profile_database"])
        with patch.object(
            reference_specs,
            "reference_param_rows",
            side_effect=AssertionError("current Contract projection queried"),
        ), patch.object(
            reference_specs,
            "get_reference_source",
            side_effect=AssertionError("current Contract queried"),
        ), patch.object(
            model_profile_catalog,
            "get_model_profile_catalog",
            side_effect=AssertionError("current catalog queried"),
        ):
            snapshot = reference_specs.capability_profile_snapshot(
                "text",
                self.family,
                self.model,
                ["sampling"],
                reference_source=str(parameter["contract_id"]),
                api_form=self.api_form,
                route_profile=self.route,
                model_profile_database=database,
            )

        self.assertEqual(snapshot["profile_id"], database["profile_id"])
        self.assertEqual(
            snapshot["reference_contract_id"],
            database["reference_contract_id"],
        )
        self.assertTrue(snapshot["resolved_parameter_expectations"])

    def test_consumer_is_independent_of_working_directory(self) -> None:
        script = r"""
import json
import re
from pathlib import Path
import model_profile_db
from lib.model_profile_catalog import get_model_profile_catalog
from lib.reference_specs import load_reference_specs

package_file = Path(model_profile_db.__file__).resolve()
version = None
for parent in package_file.parents:
    pyproject = parent / "pyproject.toml"
    if not pyproject.is_file():
        continue
    match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', pyproject.read_text(encoding="utf-8"))
    if match:
        version = match.group(1)
        break
info = get_model_profile_catalog().database_info()
contracts = load_reference_specs()["reference_sources"]
contract_fields = (
    "profile_eligible",
    "enabled",
    "executable",
    "parameter_test_enabled",
    "pressure_test_enabled",
    "disabled_reason",
)
print(json.dumps({
    "package_version": version,
    "catalog_digest": info["catalog_digest"],
    "test_extension_digest": info["test_extension_digest"],
    "contract_facade": {
        contract_id: {
            field: contracts[contract_id].get(field)
            for field in contract_fields
        }
        for contract_id in (
            "gemini_native_generate_content",
            "gemini_image_interactions",
            "deepseek_chat",
        )
    },
}))
"""
        base_env = dict(os.environ)
        base_env.update(
            {
                "LOADTEST_SKIP_DOTENV": "1",
                "LLM_API_TEST_PROVIDERS_LOCAL": "/tmp/nonexistent-providers-local.yaml",
            }
        )
        identities = []
        pythonpath = f"{APP_ROOT}:{PACKAGE_ROOT}"
        for cwd in (REPO_ROOT, APP_ROOT):
            env = dict(base_env)
            env["PYTHONPATH"] = pythonpath
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            identities.append(json.loads(completed.stdout.splitlines()[-1]))
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(identities[1]["package_version"], catalog_metadata()["package_version"])
        self.assertTrue(identities[1]["catalog_digest"])
        self.assertTrue(identities[1]["test_extension_digest"])
        facade = identities[1]["contract_facade"]
        for contract_id in (
            "gemini_native_generate_content",
            "gemini_image_interactions",
        ):
            with self.subTest(contract_id=contract_id):
                self.assertTrue(facade[contract_id]["profile_eligible"])
                self.assertTrue(facade[contract_id]["enabled"])
                self.assertTrue(facade[contract_id]["executable"])
                self.assertFalse(facade[contract_id]["parameter_test_enabled"])
                self.assertFalse(facade[contract_id]["pressure_test_enabled"])
                self.assertEqual(facade[contract_id]["disabled_reason"], "")
        self.assertTrue(facade["deepseek_chat"]["profile_eligible"])
        self.assertTrue(facade["deepseek_chat"]["enabled"])
        self.assertTrue(facade["deepseek_chat"]["executable"])
        self.assertTrue(facade["deepseek_chat"]["parameter_test_enabled"])
        self.assertTrue(facade["deepseek_chat"]["pressure_test_enabled"])
        self.assertEqual(facade["deepseek_chat"]["disabled_reason"], "")

    def test_checkout_and_install_manifest_pin_exact_mpdb_identity(self) -> None:
        expected = model_profile_catalog.EXPECTED_MODEL_PROFILE_DATABASE
        requirements = (APP_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(
            f"yibu-model-profile-db=={expected['package_version']}",
            requirements.splitlines(),
        )
        self.assertEqual(model_profile_catalog.PACKAGE_LOAD_MODE, "checkout")
        package_origin = Path(model_profile_catalog._model_profile_db.__file__).resolve()
        self.assertTrue(package_origin.is_relative_to(PACKAGE_ROOT.resolve()))

        catalog_info = get_model_profile_catalog().database_info()
        manifest = model_profile_catalog._load_and_verify_runtime_manifest()
        model_profile_catalog._validate_pinned_model_profile_database(
            package_version=model_profile_catalog._package_version(),
            catalog_info=catalog_info,
            manifest=manifest,
        )
        stale = copy.deepcopy(catalog_info)
        stale["catalog_digest"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "catalog_digest"):
            model_profile_catalog._validate_pinned_model_profile_database(
                package_version=model_profile_catalog._package_version(),
                catalog_info=stale,
                manifest=manifest,
            )
        with patch.object(
            model_profile_catalog,
            "_sha256_file",
            return_value="0" * 64,
        ), self.assertRaisesRegex(RuntimeError, "does not match its manifest"):
            model_profile_catalog._load_and_verify_runtime_manifest()

    def test_disabled_interface_cannot_enable_contract_facade(self) -> None:
        contract_id = "fixture_contract"
        contract = {
            "contract_id": contract_id,
            "source_id": "fixture_source",
            "source_ids": ["fixture_source"],
            "family_id": "fixture_family",
            "api_form": "openai_chat_completions",
            "routing_mode": "vendor_direct",
            "test_binding_status": "required",
            "parameter_capabilities": {
                "temperature": {"state": "supported"}
            },
        }
        policy = {
            "extension_type": "model_test_policy",
            "interface_id": "fixture_interface",
            "reference_contract_ids": [contract_id],
            "parameter_test_enabled": True,
            "pressure_test_enabled": True,
        }
        interface = {
            "interface_id": "fixture_interface",
            "profile_id": "fixture_profile",
            "contract_ids": [contract_id],
            "enabled": False,
            "executable": False,
            "test_binding_status": "required",
            "test_bindings": [policy],
        }
        catalog = Mock()
        catalog.get_contract.return_value = contract
        catalog.list_test_bindings.return_value = [
            {
                "test_binding_id": "parameter/fixture_contract",
                "extension_type": "parameter",
                "contract_id": contract_id,
                "test_cases": ["sampling"],
                "parameter_coverage": {"temperature": "sampling"},
            }
        ]
        catalog.list_interfaces.return_value = [interface]
        catalog.get_interface.return_value = interface
        catalog.get_profile.return_value = {"profile_state": "executable"}

        with patch.object(
            model_profile_catalog,
            "get_model_profile_catalog",
            return_value=catalog,
        ):
            projected = model_profile_catalog.reference_contract_payload(
                contract_id
            )
        self.assertFalse(projected["profile_eligible"])
        self.assertFalse(projected["enabled"])
        self.assertFalse(projected["executable"])
        self.assertFalse(projected["parameter_test_enabled"])
        self.assertFalse(projected["pressure_test_enabled"])

    def test_parameter_policy_does_not_control_contract_executability(self) -> None:
        contract_id = "fixture_contract"
        contract = {
            "contract_id": contract_id,
            "source_id": "fixture_source",
            "source_ids": ["fixture_source"],
            "family_id": "fixture_family",
            "api_form": "openai_chat_completions",
            "routing_mode": "vendor_direct",
            "test_binding_status": "required",
            "parameter_capabilities": {
                "temperature": {"state": "supported"}
            },
        }
        policy = {
            "extension_type": "model_test_policy",
            "interface_id": "fixture_interface",
            "reference_contract_ids": [contract_id],
            "parameter_test_enabled": False,
            "pressure_test_enabled": True,
        }
        interface = {
            "interface_id": "fixture_interface",
            "profile_id": "fixture_profile",
            "contract_ids": [contract_id],
            "enabled": True,
            "executable": True,
            "test_binding_status": "required",
            "test_bindings": [policy],
        }
        catalog = Mock()
        catalog.get_contract.return_value = contract
        catalog.list_test_bindings.return_value = [
            {
                "test_binding_id": "parameter/fixture_contract",
                "extension_type": "parameter",
                "contract_id": contract_id,
                "test_cases": ["sampling"],
                "parameter_coverage": {"temperature": "sampling"},
            }
        ]
        catalog.list_interfaces.return_value = [interface]
        catalog.get_interface.return_value = interface
        catalog.get_profile.return_value = {"profile_state": "executable"}

        with patch.object(
            model_profile_catalog,
            "get_model_profile_catalog",
            return_value=catalog,
        ):
            projected = model_profile_catalog.reference_contract_payload(
                contract_id
            )
        self.assertTrue(projected["profile_eligible"])
        self.assertTrue(projected["enabled"])
        self.assertTrue(projected["executable"])
        self.assertFalse(projected["parameter_test_enabled"])
        self.assertTrue(projected["pressure_test_enabled"])
        self.assertEqual(projected["disabled_reason"], "")

    def test_default_consumers_are_mpdb_only(self) -> None:
        parameter = self._parameter_config()
        contract_id = str(parameter["contract_id"])
        for retired_path in (
            APP_ROOT / "api_reference_specs.yaml",
            APP_ROOT / "model_capability_profiles.yaml",
            APP_ROOT / "scripts" / "register_model.py",
        ):
            with self.subTest(retired_path=retired_path):
                self.assertFalse(retired_path.exists())
        for retired_symbol in (
            "REFERENCE_SPECS_PATH",
            "CAPABILITY_PROFILES_PATH",
            "LOCAL_CAPABILITY_PROFILES_PATH",
            "_read_yaml_cached",
            "_resolve_reference_source_inheritance",
            "_merge_v3_migration_override",
            "_migrate_v3_capability_payload",
            "_normalize_capability_payload",
            "_match_model_profile",
        ):
            with self.subTest(retired_symbol=retired_symbol):
                self.assertFalse(hasattr(reference_specs, retired_symbol))
        for public_api in (
            reference_specs.load_reference_specs,
            reference_specs.comparison_reference_source_for_model,
            reference_specs.load_model_capability_profiles,
            reference_specs.load_model_capability_profile,
            reference_specs.resolve_profile_expectation,
            reference_specs.resolve_parameter_expectation,
            reference_specs.resolve_suite_expectations,
            reference_specs.capability_profile_snapshot,
        ):
            parameters = inspect.signature(public_api).parameters
            with self.subTest(public_api=public_api.__name__):
                self.assertNotIn("path", parameters)
                self.assertNotIn("reference_specs_path", parameters)

        with self.subTest("MPDB consumer smoke"):
            self.assertEqual(
                load_reference_specs()["projection_source"],
                "yibu-model-profile-db",
            )
            self.assertTrue(reference_test_profiles(contract_id))
            built = build_request(
                self.config,
                "compatibility_profiles",
                reference_test_profiles(contract_id)[0],
                model_family_override=self.family,
                api_form_override=self.api_form,
                route_profile_override=self.route,
                reference_source=contract_id,
                enforce_model_capabilities=False,
            )
            self.assertEqual(built.metadata["requested_model"], self.model)
            pressure_provider = self.provider
            pressure_model = self.model
            pressure_family = get_model_family(
                self.config, pressure_model, pressure_provider
            )
            pressure_route = get_model_route_profile(
                self.config, pressure_model, pressure_provider
            )
            pressure_form = get_model_api_form(
                self.config,
                pressure_model,
                pressure_provider,
                route_profile=pressure_route,
            )
            pressure_parameter = resolve_runtime_parameter_config(
                self.config,
                pressure_provider,
                pressure_model,
                pressure_family,
                pressure_route,
                pressure_form,
            )
            pressure_profiles_for_model(
                pressure_family,
                pressure_model,
                str(pressure_parameter["contract_id"]),
                api_form=pressure_form,
                route_profile=pressure_route,
            )
            image_target = get_image_model_config(
                self.config, "gemini", "gemini-3.1-flash-image"
            )
            image_capability = load_model_capability_profile(
                "image",
                str(image_target["family"]),
                "gemini-3.1-flash-image",
                api_form=str(image_target["api_form"]),
                route_profile=str(image_target["route_profile"]),
            )
            self.assertEqual(
                image_capability["model_profile_database"]["catalog_digest"],
                catalog_metadata()["catalog_digest"],
            )

            cache_config = copy.deepcopy(self.config)
            cache_config.setdefault("cache_test", {})["scenario"] = "invalid-smoke"
            with tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
                ValueError, "cache_test.scenario"
            ):
                run_cache_suite(
                    cache_config,
                    Mock(),
                    Path(temp_dir),
                    measured_requests=1,
                )

            response = app.test_client().get("/api/config")
            self.assertEqual(response.status_code, 200)

    def test_cache_smoke_uses_catalog_snapshot(self) -> None:
        cache_config = copy.deepcopy(self.config)
        cache_config["active_provider"] = "yibu"
        cache_config["providers"]["yibu"]["models"][
            "default"
        ] = "deepseek-v4-flash"
        cache_config["cache_test"] = {
            **(cache_config.get("cache_test") or {}),
            "scenario": "shared_prefix",
            "warmup_requests": 1,
            "wait_after_warmup_sec": 0,
            "controls": {
                "mode": "custom",
                "positive_long_prefix_pairs": 1,
                "negative_unique_prefix_requests": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_cache_suite(
                cache_config,
                _CacheSmokeClient(),
                Path(temp_dir),
                measured_requests=1,
            )
        self.assertEqual(result["provider"], "yibu")
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertEqual(
            result["model_profile_database"]["catalog_digest"],
            catalog_metadata()["catalog_digest"],
        )
        self.assertEqual(
            result["model_profile_database"]["profile_id"],
            "text/deepseek/deepseek/deepseek-v4-flash-0731",
        )
        self.assertEqual(
            result["model_profile_database"]["interface"]["default_contract_id"],
            "deepseek_chat",
        )
        self.assertTrue(
            result["model_profile_database"]["interface"]["test_bindings"][0][
                "pressure_test_enabled"
            ]
        )
        self.assertGreater(result["summary"]["business_record_count"], 0)

    def test_wrong_source_family_api_form_and_binding_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "family"):
            resolve_runtime_parameter_config(
                self.config,
                self.provider,
                self.model,
                "gpt",
                self.route,
                self.api_form,
            )
        with self.assertRaisesRegex(ValueError, "API form"):
            resolve_runtime_parameter_config(
                self.config,
                self.provider,
                self.model,
                self.family,
                self.route,
                "anthropic_messages",
            )

        wrong_source = copy.deepcopy(self.config)
        wrong_source["providers"][self.provider]["models"].setdefault(
            "reference_source_ids", {}
        )[self.model] = "openai"
        with self.assertRaisesRegex(ValueError, "official MPDB binding"):
            resolve_runtime_parameter_config(
                wrong_source,
                self.provider,
                self.model,
                self.family,
                self.route,
                self.api_form,
            )

        catalog = get_model_profile_catalog()
        other = next(
            row
            for row in catalog.list_profiles(modality="text")
            if row["source_id"] != "deepseek" and row.get("interface_ids")
        )
        wrong_binding = copy.deepcopy(self.config)
        wrong_binding["providers"][self.provider]["models"].setdefault(
            "reference_bindings", {}
        )[self.model] = {
            "profile_id": other["profile_id"],
            "interface_id": other["interface_ids"][0],
        }
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_runtime_parameter_config(
                wrong_binding,
                self.provider,
                self.model,
                self.family,
                self.route,
                self.api_form,
            )

        tampered = copy.deepcopy(
            self._parameter_config()["model_profile_database"]
        )
        tampered["reference_contract"]["source_id"] = "openai"
        with self.assertRaisesRegex(ValueError, "snapshot"):
            binding_from_database_snapshot(tampered)

    def test_job_v5_pins_snapshot_and_history_never_queries_current_catalog(self) -> None:
        parameter = self._parameter_config()
        snapshot = copy.deepcopy(parameter["model_profile_database"])
        job = make_job_spec(
            job_type="param_test",
            provider=self.provider,
            model=self.model,
            model_family=self.family,
            api_form=self.api_form,
            route_profile=self.route,
            workload="param_test",
            request_mode="fixed",
            target_rpm=0.0,
            target_tpm=0.0,
            reference_contract_id=str(parameter["contract_id"]),
            model_profile_database=snapshot,
        )
        self.assertEqual(job["schema_version"], 5)
        self.assertNotIn("reference_source", job)
        for field in (
            "package_version",
            "catalog_digest",
            "test_extension_digest",
            "snapshot_digest",
        ):
            self.assertTrue(job["model_profile_database"][field])

        with patch(
            "lib.model_profile_catalog.get_model_profile_catalog",
            side_effect=AssertionError("current catalog queried"),
        ):
            restored = resolve_job_model_profile_snapshot(job)
        self.assertEqual(restored["resolution_status"], "snapshot")
        self.assertEqual(
            restored["reference_contract_id"], parameter["contract_id"]
        )

        tampered = copy.deepcopy(job)
        tampered["provider"] = "wrong-provider"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_spec.json"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "execution target conflicts"):
                load_job_spec(path)


if __name__ == "__main__":
    unittest.main()
