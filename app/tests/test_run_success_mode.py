from __future__ import annotations

import unittest
import copy
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from lib.param_outcome import (
    apply_any_run_success,
    compatibility_pass_from_statuses,
    normalize_run_success_mode,
)
from scripts import param_test
from lib.client import ChatResult


def _run(profile: str, status: str, *, token_ok: bool = True, run_index: int = 1) -> dict:
    return {
        "profile": profile,
        "provider": "kimi_official",
        "model": "kimi-k3",
        "reference_source": "kimi_k3_openai_compat",
        "api_form": "openai_chat_completions",
        "route_profile": "vendor_direct",
        "transport": "chat_completions",
        "request_endpoint": "https://example.invalid/v1/chat/completions",
        "expectation": "supported",
        "status_code": 503 if status == "fail" else 200,
        "run_index": run_index,
        "status": status,
        "compatibility_status": status,
        "compatibility_pass": status == "pass",
        "pass": status == "pass",
        "token_validation_pass": token_ok,
        "overall_pass": status == "pass" and token_ok,
        "overall_status": status if status != "pass" else ("pass" if token_ok else "token_validation_failed"),
        "failed_check": None if status == "pass" else "preserved_thinking_mismatch",
        "failure_classification": None if status == "pass" else "preserved_thinking_mismatch",
        "failed_request_body": {"messages": [{"role": "user", "content": "Probe"}]},
        "failed_response_raw": "missing historical numbers" if status != "pass" else None,
    }


class RunSuccessModeTest(unittest.TestCase):
    def test_normalize_rejects_unknown_mode(self) -> None:
        self.assertEqual(normalize_run_success_mode(None), "all")
        self.assertEqual(normalize_run_success_mode("ANY"), "any")
        with self.assertRaises(ValueError):
            normalize_run_success_mode("majority")

    def test_any_mode_needs_one_pass(self) -> None:
        results = [
            _run("kimi_k3_preserved_thinking", "incompatible", run_index=1),
            _run("kimi_k3_preserved_thinking", "pass", run_index=2),
            _run("kimi_k3_preserved_thinking", "incompatible", run_index=3),
            _run("kimi_k3_stream", "incompatible", run_index=1),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="any"
        )
        preserved = [item for item in results if item["profile"] == "kimi_k3_preserved_thinking"]
        self.assertTrue(all(item["status"] == "pass" for item in preserved))
        self.assertEqual(preserved[0]["pre_aggregation_status"], "incompatible")
        self.assertTrue(preserved[0]["satisfied_by_sibling_run"])
        self.assertEqual(preserved[0]["failed_check"], "preserved_thinking_mismatch")
        self.assertEqual(results[3]["status"], "incompatible")
        self.assertTrue(
            compatibility_pass_from_statuses(
                [item["status"] for item in preserved]
            )
        )

    def test_any_mode_does_not_upgrade_when_all_fail(self) -> None:
        results = [
            _run("kimi_k3_preserved_thinking", "incompatible", run_index=1),
            _run("kimi_k3_preserved_thinking", "incompatible", run_index=2),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="any"
        )
        self.assertTrue(all(item["status"] == "incompatible" for item in results))

    def test_any_mode_does_not_upgrade_hard_fail(self) -> None:
        results = [
            _run("kimi_k3_preserved_thinking", "fail", run_index=1),
            _run("kimi_k3_preserved_thinking", "pass", run_index=2),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="any"
        )
        self.assertEqual(results[0]["status"], "fail")
        self.assertEqual(results[1]["status"], "pass")

    def test_all_mode_is_noop(self) -> None:
        results = [
            _run("kimi_k3_preserved_thinking", "incompatible", run_index=1),
            _run("kimi_k3_preserved_thinking", "pass", run_index=2),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="all"
        )
        self.assertEqual(results[0]["status"], "incompatible")

    def test_any_mode_keeps_token_failure(self) -> None:
        results = [
            _run(
                "kimi_k3_preserved_thinking",
                "incompatible",
                token_ok=False,
                run_index=1,
            ),
            _run("kimi_k3_preserved_thinking", "pass", run_index=2),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="any"
        )
        self.assertEqual(results[0]["status"], "pass")
        self.assertFalse(results[0]["overall_pass"])
        self.assertEqual(results[0]["overall_status"], "token_validation_failed")

    def test_any_mode_does_not_upgrade_unexpected_acceptance(self) -> None:
        results = [
            _run("kimi_k3_preserved_thinking", "unexpected_acceptance", run_index=1),
            _run("kimi_k3_preserved_thinking", "pass", run_index=2),
        ]
        apply_any_run_success(
            results, profile="kimi_k3_preserved_thinking", mode="any"
        )
        self.assertEqual(results[0]["status"], "unexpected_acceptance")

    def test_any_mode_preserves_original_failure_artifacts(self) -> None:
        failed = _run("kimi_k3_preserved_thinking", "incompatible")
        original = copy.deepcopy(failed)
        results = [failed, _run("kimi_k3_preserved_thinking", "pass", run_index=2)]
        apply_any_run_success(results, profile=failed["profile"], mode="any")
        cases = param_test._failed_cases(results)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["status"], "incompatible")
        self.assertFalse(cases[0]["compatibility_pass"])
        self.assertEqual(cases[0]["aggregated_status"], "pass")
        for field in ("failed_check", "failed_request_body", "failed_response_raw"):
            self.assertEqual(cases[0][field], original[field])
        self.assertIn("satisfied by sibling run", param_test._failed_cases_log(cases))
        aggregated = copy.deepcopy(results)
        apply_any_run_success(results, profile=failed["profile"], mode="any")
        self.assertEqual(results, aggregated)

    def test_any_mode_does_not_cross_request_contexts(self) -> None:
        for field in (
            "provider", "model", "reference_source", "api_form", "route_profile",
            "transport", "request_endpoint", "expectation",
        ):
            with self.subTest(field=field):
                failed = _run("kimi_k3_preserved_thinking", "incompatible")
                passed = _run(failed["profile"], "pass", run_index=2)
                passed[field] = "different"
                apply_any_run_success([failed, passed], profile=failed["profile"], mode="any")
                self.assertEqual(failed["status"], "incompatible")

    def test_any_mode_does_not_hide_other_incompatibilities(self) -> None:
        mutations = [
            {"status_code": code} for code in (None, 400, 401, 403, 404, 422, 429, 503)
        ] + [{"failure_classification": "json_parse"}, {"failure_classification": "reasoning_content_missing"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                failed = _run("kimi_k3_preserved_thinking", "incompatible")
                failed.update(mutation)
                apply_any_run_success(
                    [failed, _run(failed["profile"], "pass", run_index=2)],
                    profile=failed["profile"], mode="any",
                )
                self.assertEqual(failed["status"], "incompatible")

    def test_any_mode_needs_valid_original_passing_evidence(self) -> None:
        for mutation in (
            {"status_code": 503},
            {"token_validation_pass": False},
            {"model_identity_audit": {"status": "mismatch"}},
            {"satisfied_by_sibling_run": True},
        ):
            with self.subTest(mutation=mutation):
                failed = _run("kimi_k3_preserved_thinking", "incompatible")
                passed = _run(failed["profile"], "pass", run_index=2)
                passed.update(mutation)
                apply_any_run_success([failed, passed], profile=failed["profile"], mode="any")
                self.assertEqual(failed["status"], "incompatible")


class RunSuccessModeRunnerTest(unittest.TestCase):
    profile = "kimi_k3_preserved_thinking"

    def _run_matrix(self, config: dict, output_dir: Path | None = None) -> list:
        class IdentityClient:
            def chat_completion(self, body):
                response = {"model": "kimi-k3", "choices": [{"message": {"content": "OK", "reasoning_content": "I will reply OK."}, "finish_reason": "stop"}]}
                return ChatResult(success=True, status_code=200, latency_ms=10, timestamp=0,
                                  response_json=response, usage={"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16})
        return param_test.run_param_tests(
            config, IdentityClient(), "kimi_official", "kimi-k3", "kimi",
            "kimi_k3_openai_compat", "kimi", 3, output_dir,
            capability_profile={"parameter_test_enabled": True},
        )

    def test_invalid_later_profile_mode_stops_before_first_request(self) -> None:
        config = {"compatibility_profiles": {"later": {"run_success_mode": "majority"}}}
        with patch.dict(os.environ, {"LOADTEST_PARAM_PROFILES": ""}), patch.object(
            param_test, "reference_test_profiles", return_value=[self.profile, "later"]
        ), patch.object(param_test, "run_one_profile") as run:
            with self.assertRaisesRegex(ValueError, "run_success_mode"):
                self._run_matrix(config)
            run.assert_not_called()

    def test_invalid_mode_stops_main_before_identity_or_client(self) -> None:
        with patch.dict(os.environ, {
            "LOADTEST_SKIP_DOTENV": "1",
            "LLM_API_TEST_PROVIDERS_LOCAL": "/tmp/nonexistent-review-provider.yaml",
            "LOADTEST_PROVIDER": "kimi_official", "LOADTEST_MODEL": "kimi-k3",
            "LOADTEST_ROUTE_PROFILE": "vendor_direct",
            "LOADTEST_API_FORM": "openai_chat_completions",
            "LOADTEST_REFERENCE_SOURCE": "kimi_k3_openai_compat",
        }, clear=True), patch.object(
            param_test, "_lookup_profile_settings", return_value={"run_success_mode": "majority"}
        ), patch.object(param_test.DeepSeekClient, "from_config") as client, patch.object(
            param_test, "run_identity_probe"
        ) as identity:
            with self.assertRaisesRegex(ValueError, "run_success_mode"):
                param_test.main()
            client.assert_not_called()
            identity.assert_not_called()

    def test_each_completed_run_is_persisted_before_interruption(self) -> None:
        config = param_test.load_config()
        config.setdefault("compatibility_profiles", {}).setdefault(self.profile, {})["run_success_mode"] = "any"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOADTEST_PARAM_PROFILES": ""}
        ), patch.object(param_test, "reference_test_profiles", return_value=[self.profile]), patch.object(
            param_test, "resolve_profile_expectation", return_value="supported"
        ), patch.object(param_test, "_sample_inputs_for_profile", return_value=[{"id": "profile_defined:offline", "prompt": "Reply OK."}] * 3):
            output_dir = Path(temporary)
            def run(*args, **kwargs):
                run_index = args[8]
                if run_index > 1:
                    persisted = json.loads((output_dir / "param_results.json").read_text())
                    self.assertEqual(len(persisted), run_index - 1)
                    cases = json.loads((output_dir / "param_failed_cases.json").read_text())
                    self.assertEqual(cases[0]["compatibility_status"], "incompatible")
                if run_index == 3:
                    self.assertEqual(persisted[0]["status"], "pass")
                    self.assertEqual(cases[0]["aggregated_status"], "pass")
                    raise KeyboardInterrupt("offline interruption")
                return _run(self.profile, "incompatible" if run_index == 1 else "pass", run_index=run_index)
            with patch.object(param_test, "run_one_profile", side_effect=run):
                with self.assertRaises(KeyboardInterrupt):
                    self._run_matrix(config, output_dir)
            self.assertEqual(len(json.loads((output_dir / "param_results.json").read_text())), 2)
            self.assertTrue((output_dir / "token_audit.json").exists())


if __name__ == "__main__":
    unittest.main()
