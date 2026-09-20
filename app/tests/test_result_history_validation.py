from __future__ import annotations

import copy
import tempfile
import unittest

from lib.config import (
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    load_config,
)
from lib.job_spec import (
    build_result_validation_contract,
    classify_cache_result,
    classify_parameter_result,
    make_job_spec,
)
from lib.model_profile_catalog import (
    database_snapshot,
    resolve_runtime_profile_binding,
    resolve_runtime_test_policy,
)
from lib.threshold import check_cache
from scripts.run_cache import _require_current_cache_job_spec


class ResultHistoryValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.provider = "gemini"
        self.model = "gemini-2.5-flash"
        family = get_model_family(self.config, self.model, self.provider)
        route = get_model_route_profile(self.config, self.model, self.provider)
        api_form = get_model_api_form(
            self.config,
            self.model,
            self.provider,
            route_profile=route,
        )
        binding = resolve_runtime_profile_binding(
            self.config,
            self.provider,
            self.model,
            family,
            route,
            api_form,
            modality="text",
        )
        policy = resolve_runtime_test_policy(binding)
        self.snapshot = database_snapshot(binding)
        self.job_spec = make_job_spec(
            job_type="param_test",
            provider=self.provider,
            model=self.model,
            model_family=family,
            api_form=api_form,
            route_profile=route,
            source_id=str(binding["source_id"]),
            profile_id=str(binding["profile_id"]),
            interface_id=str(binding["interface_id"]),
            test_binding_id=str(policy["test_binding_id"]),
            reference_contract_id=str(self.snapshot["reference_contract_id"]),
            model_profile_database=self.snapshot,
            workload="parameters",
            request_mode="fixed",
            target_rpm=0,
            target_tpm=0,
            result_contract=build_result_validation_contract(self.config),
        )
        self.assertEqual(
            self.job_spec["result_contract"]["token_policy"]["evidence_policy"],
            "exact_only",
        )
        self.cache_snapshot = database_snapshot(
            binding,
            include_parameter_binding=False,
        )
        self.cache_job_spec = make_job_spec(
            job_type="cache_suite",
            provider=self.provider,
            model=self.model,
            model_family=family,
            api_form=api_form,
            route_profile=route,
            source_id=str(binding["source_id"]),
            profile_id=str(binding["profile_id"]),
            interface_id=str(binding["interface_id"]),
            test_binding_id=str(policy["test_binding_id"]),
            reference_contract_id=str(
                self.cache_snapshot["reference_contract_id"]
            ),
            model_profile_database=self.cache_snapshot,
            workload="cache_suite",
            request_mode="fixed",
            target_rpm=0,
            target_tpm=0,
            cache_plan={"scenario": "shared_prefix"},
        )

    def _result(self, *, passed: bool = True) -> dict:
        return {
            "param_test_runs": self.job_spec["parameter_execution"]["runs"],
            "tool_validation_mode": "auto",
            "pass": passed,
            "token_validation_pass": True,
            "token_audit_summary": {
                "schema_version": 4,
                "validation_status": "pass",
                "pass": True,
                "exchange_count": 1,
                "required_exchange_count": 1,
                "validated_exchange_count": 1,
                "validation_failure_count": 0,
                "missing_audit_result_count": 0,
                "invalid_audit_result_count": 0,
                "missing_usage_count": 0,
                "gross_check_count": 1,
                "gross_failure_count": 0,
                "gross_partial_count": 0,
                "completion_check_count": 1,
                "completion_failure_count": 0,
                "completion_unverified_count": 0,
                "arithmetic_check_count": 1,
                "arithmetic_failure_count": 0,
            },
            "provider": self.job_spec["provider"],
            "model": self.job_spec["model"],
            "route_profile": self.job_spec["route_profile"],
            "api_form": self.job_spec["api_form"],
            "source_id": self.snapshot["source_id"],
            "profile_id": self.snapshot["profile_id"],
            "interface_id": self.snapshot["interface_id"],
            "test_binding_id": self.snapshot["test_binding_id"],
            "reference_contract_id": self.snapshot["reference_contract_id"],
            "model_profile_database": copy.deepcopy(self.snapshot),
        }

    def test_schema_three_pass_does_not_prove_complete_output(self) -> None:
        old = self._result()
        old["token_audit_summary"]["schema_version"] = 3
        classified = classify_parameter_result(self.job_spec, old)
        self.assertFalse(classified["pass"])
        self.assertEqual(classified["status"], "legacy_unverified")

    def test_completion_evidence_is_required_even_with_a_passing_count_summary(self) -> None:
        for field, value in (("gross_partial_count", 1), ("completion_check_count", 0),
                             ("completion_failure_count", 1),
                             ("completion_unverified_count", 1)):
            with self.subTest(field=field):
                result = self._result()
                result["token_audit_summary"][field] = value
                self.assertFalse(classify_parameter_result(self.job_spec, result)["pass"])

    def _cache_result(self, *, passed: bool = True) -> dict:
        return {
            "schema_version": 1,
            "pass": passed,
            "threshold_pass": passed,
            "stage": "cache",
            "completed": True,
            "latency_evidence_only": True,
            "official_usage_required_for_hit_rate": True,
            "mode": "gate",
            "summary": {
                "record_count": 3,
                "business_record_count": 1,
                "business_success_count": 1 if passed else 0,
                "cache_usage_fields_seen": 1,
                "cached_input_token_ratio": 0.5,
                "cache_usage_accuracy_status": "pass",
                "cache_usage_accuracy_pass": True,
                "cache_usage_accuracy_record_count": 3,
                "cache_usage_accuracy_failure_count": 0,
                "cache_controls_present": True,
                "cache_control_positive_warm_count": 1,
                "cache_control_negative_count": 1,
                "cache_control_metrics": {
                    "positive_long_prefix": {
                        "cached_input_token_ratio": 0.8,
                    },
                    "negative_unique_prefix": {
                        "cached_input_token_ratio": 0.0,
                    },
                },
            },
            "failures": (
                []
                if passed
                else [
                    {
                        "metric": "cached_input_token_ratio",
                        "actual": 0.5,
                        "expected": ">=0.9",
                    }
                ]
            ),
            "provider": self.cache_job_spec["provider"],
            "model": self.cache_job_spec["model"],
            "route_profile": self.cache_job_spec["route_profile"],
            "api_form": self.cache_job_spec["api_form"],
            "source_id": self.cache_snapshot["source_id"],
            "profile_id": self.cache_snapshot["profile_id"],
            "interface_id": self.cache_snapshot["interface_id"],
            "test_binding_id": self.cache_snapshot["test_binding_id"],
            "reference_contract_id": self.cache_snapshot[
                "reference_contract_id"
            ],
            "model_profile_database": copy.deepcopy(self.cache_snapshot),
        }

    def test_cache_history_requires_current_matching_mpdb_snapshots(self) -> None:
        current = classify_cache_result(
            self.cache_job_spec,
            self._cache_result(),
        )
        self.assertEqual(current["status"], "current_pass")
        self.assertTrue(current["tested"])
        self.assertTrue(current["pass"])

        failed = classify_cache_result(
            self.cache_job_spec,
            self._cache_result(passed=False),
        )
        self.assertEqual(failed["status"], "current_fail")
        self.assertTrue(failed["tested"])
        self.assertFalse(failed["pass"])

        legacy = copy.deepcopy(self.cache_job_spec)
        legacy["schema_version"] = 3
        unverified = classify_cache_result(legacy, self._cache_result())
        self.assertEqual(unverified["status"], "legacy_unverified")
        self.assertFalse(unverified["tested"])
        self.assertFalse(unverified["pass"])

        mismatched = self._cache_result()
        mismatched["model"] = "other-model"
        invalid = classify_cache_result(self.cache_job_spec, mismatched)
        self.assertEqual(invalid["status"], "legacy_unverified")
        self.assertFalse(invalid["pass"])

        versioned_job = copy.deepcopy(self.cache_job_spec)
        versioned_result = self._cache_result()
        versioned_job["model_profile_database"]["snapshot_schema_version"] = 2
        versioned_result["model_profile_database"][
            "snapshot_schema_version"
        ] = 2
        unsupported = classify_cache_result(versioned_job, versioned_result)
        self.assertIn(
            "job_mpdb_snapshot_schema_not_current",
            unsupported["reasons"],
        )
        self.assertIn(
            "result_mpdb_snapshot_schema_not_current",
            unsupported["reasons"],
        )
        self.assertFalse(unsupported["pass"])

    def test_cache_history_accepts_current_threshold_writer_verdict(self) -> None:
        expected = self._cache_result()
        cache_result = {
            key: copy.deepcopy(expected[key])
            for key in (
                "summary",
                "provider",
                "model",
                "route_profile",
                "api_form",
                "source_id",
                "profile_id",
                "interface_id",
                "test_binding_id",
                "model_profile_database",
            )
        }
        with tempfile.TemporaryDirectory() as output_dir:
            verdict = check_cache(
                cache_result,
                {"thresholds": {"cache": {"mode": "observe"}}},
                output_dir,
            )
        classified = classify_cache_result(self.cache_job_spec, verdict)
        self.assertEqual(classified["status"], "current_pass")
        self.assertTrue(classified["tested"])
        self.assertTrue(classified["pass"])

    def test_cache_history_rejects_incomplete_or_inconsistent_verdicts(self) -> None:
        required_fields = (
            "schema_version",
            "stage",
            "completed",
            "latency_evidence_only",
            "official_usage_required_for_hit_rate",
            "summary",
        )
        for field in required_fields:
            incomplete = self._cache_result()
            incomplete.pop(field)
            with self.subTest(missing=field):
                classified = classify_cache_result(self.cache_job_spec, incomplete)
                self.assertEqual(classified["status"], "legacy_unverified")
                self.assertFalse(classified["tested"])
                self.assertFalse(classified["pass"])

        no_evidence = self._cache_result()
        no_evidence["summary"]["cache_usage_fields_seen"] = 0
        no_evidence["summary"]["cached_input_token_ratio"] = None
        for control in no_evidence["summary"]["cache_control_metrics"].values():
            control["cached_input_token_ratio"] = None
        classified = classify_cache_result(self.cache_job_spec, no_evidence)
        self.assertIn(
            "cache_verdict_missing_usage_or_control_evidence",
            classified["reasons"],
        )
        self.assertFalse(classified["pass"])

        inconsistent = self._cache_result()
        inconsistent["threshold_pass"] = False
        classified = classify_cache_result(self.cache_job_spec, inconsistent)
        self.assertIn("cache_verdict_threshold_inconsistent", classified["reasons"])
        self.assertFalse(classified["pass"])

    def test_current_history_rejects_job_identity_view_conflicts(self) -> None:
        cases = (
            (
                "cache",
                self.cache_job_spec,
                self.cache_snapshot,
                self._cache_result(),
                classify_cache_result,
            ),
            (
                "parameter",
                self.job_spec,
                self.snapshot,
                self._result(),
                classify_parameter_result,
            ),
        )
        mutations = (
            (("model",), "other-model", "job_model_mismatch"),
            (("transport",), "other-transport", "job_transport_mismatch"),
            (
                ("execution_target", "request_model_id"),
                "other-model",
                "job_execution_target_request_model_id_mismatch",
            ),
            (
                ("reference_identity", "profile_id"),
                "other-profile",
                "job_reference_identity_profile_id_mismatch",
            ),
            (
                (
                    "model_capability_profile",
                    "model_profile_database",
                    "runtime_model_id",
                ),
                "other-model",
                "job_mpdb_snapshot_views_conflict",
            ),
        )
        for kind, base_spec, snapshot, result, classifier in cases:
            structured = copy.deepcopy(base_spec)
            target = snapshot["execution_target"]
            structured["execution_target"] = {
                "provider_id": target["provider_id"],
                "request_model_id": target["request_model_id"],
                "route_profile": target["route_profile"],
                "api_form": target["api_form"],
                "transport": structured.get("transport"),
            }
            structured["reference_identity"] = {
                field: structured.get(field)
                for field in (
                    "source_id",
                    "profile_id",
                    "interface_id",
                    "test_binding_id",
                    "reference_contract_id",
                )
            }
            structured["model_capability_profile"] = {
                "model_profile_database": copy.deepcopy(snapshot)
            }
            for path, value, expected_reason in mutations:
                tampered = copy.deepcopy(structured)
                target_object = tampered
                for field in path[:-1]:
                    target_object = target_object[field]
                target_object[path[-1]] = value
                with self.subTest(kind=kind, path=path):
                    classified = classifier(tampered, result)
                    self.assertFalse(classified["tested"])
                    self.assertFalse(classified["pass"])
                    self.assertFalse(classified["identity_match"])
                    self.assertIn(expected_reason, classified["reasons"])

            malformed = copy.deepcopy(structured)
            malformed["execution_target"] = []
            classified = classifier(malformed, result)
            self.assertFalse(classified["pass"])
            self.assertIn("job_execution_target_invalid", classified["reasons"])

    def test_cache_replay_rejects_legacy_or_snapshotless_jobs(self) -> None:
        self.assertEqual(
            _require_current_cache_job_spec(self.cache_job_spec),
            self.cache_snapshot,
        )

        legacy = copy.deepcopy(self.cache_job_spec)
        legacy["schema_version"] = 3
        with self.assertRaisesRegex(RuntimeError, "requires schema 4"):
            _require_current_cache_job_spec(legacy)

        snapshotless = copy.deepcopy(self.cache_job_spec)
        snapshotless.pop("model_profile_database")
        with self.assertRaisesRegex(RuntimeError, "immutable MPDB snapshot"):
            _require_current_cache_job_spec(snapshotless)

        tampered = copy.deepcopy(self.cache_job_spec)
        tampered["model_profile_database"]["execution_target"][
            "request_model_id"
        ] = "other-model"
        with self.assertRaisesRegex(RuntimeError, "valid immutable MPDB snapshot"):
            _require_current_cache_job_spec(tampered)

        planless = copy.deepcopy(self.cache_job_spec)
        planless["cache_plan"] = {}
        with self.assertRaisesRegex(RuntimeError, "frozen cache_plan"):
            _require_current_cache_job_spec(planless)

        wrong_snapshot_version = copy.deepcopy(self.cache_job_spec)
        wrong_snapshot_version["model_profile_database"][
            "snapshot_schema_version"
        ] = 2
        with self.assertRaisesRegex(RuntimeError, "MPDB snapshot schema 1"):
            _require_current_cache_job_spec(wrong_snapshot_version)

        wrong_type = copy.deepcopy(self.cache_job_spec)
        wrong_type["type"] = "param_test"
        with self.assertRaisesRegex(RuntimeError, "cache_suite Job spec"):
            _require_current_cache_job_spec(wrong_type)

    def test_current_result_requires_complete_token_and_identity_evidence(self) -> None:
        current = classify_parameter_result(self.job_spec, self._result())
        self.assertEqual(current["status"], "current_pass")
        self.assertTrue(current["tested"])
        self.assertTrue(current["pass"])

        failed = classify_parameter_result(
            self.job_spec,
            self._result(passed=False),
        )
        self.assertEqual(failed["status"], "current_fail")
        self.assertTrue(failed["tested"])
        self.assertFalse(failed["pass"])

    def test_legacy_and_incomplete_results_never_become_current_pass(self) -> None:
        old_schema = self._result()
        old_schema["token_audit_summary"]["schema_version"] = 2
        legacy = classify_parameter_result(self.job_spec, old_schema)
        self.assertEqual(legacy["status"], "legacy_unverified")
        self.assertFalse(legacy["tested"])
        self.assertFalse(legacy["pass"])

        no_contract = copy.deepcopy(self.job_spec)
        no_contract.pop("result_contract")
        legacy = classify_parameter_result(no_contract, self._result())
        self.assertEqual(legacy["status"], "legacy_unverified")
        self.assertFalse(legacy["pass"])

        missing_pass = self._result()
        missing_pass.pop("pass")
        incomplete = classify_parameter_result(self.job_spec, missing_pass)
        self.assertEqual(incomplete["status"], "unverified")
        self.assertFalse(incomplete["tested"])
        self.assertFalse(incomplete["pass"])

    def test_token_alias_is_legacy_and_identity_mismatch_fails(self) -> None:
        alias_result = self._result()
        alias_result.pop("token_validation_pass")
        alias_result["token_accuracy_pass"] = True
        aliased = classify_parameter_result(self.job_spec, alias_result)
        self.assertEqual(aliased["status"], "legacy_unverified")
        self.assertFalse(aliased["tested"])
        self.assertFalse(aliased["pass"])
        self.assertEqual(
            aliased["compatibility_fallbacks"],
            ["token_accuracy_pass"],
        )
        self.assertIn(
            "missing_boolean_token_validation_pass",
            aliased["reasons"],
        )

        mismatched = self._result()
        mismatched["model"] = "other-model"
        invalid = classify_parameter_result(self.job_spec, mismatched)
        self.assertEqual(invalid["status"], "identity_mismatch")
        self.assertFalse(invalid["tested"])
        self.assertFalse(invalid["pass"])

    def test_empty_or_contradictory_token_summary_never_becomes_current(self) -> None:
        minimal = self._result()
        minimal["token_audit_summary"] = {"schema_version": 4, "pass": True}
        invalid = classify_parameter_result(self.job_spec, minimal)
        self.assertEqual(invalid["status"], "token_validation_failed")
        self.assertFalse(invalid["tested"])
        self.assertFalse(invalid["pass"])
        self.assertFalse(invalid["token_audit_summary_current"])

        contradictory = self._result()
        contradictory["token_audit_summary"].update(
            {
                "pass": False,
                "validation_status": "fail",
                "validated_exchange_count": 0,
                "validation_failure_count": 1,
                "missing_usage_count": 1,
                "gross_failure_count": 1,
            }
        )
        invalid = classify_parameter_result(self.job_spec, contradictory)
        self.assertEqual(invalid["status"], "token_validation_failed")
        self.assertFalse(invalid["tested"])
        self.assertFalse(invalid["pass"])
        self.assertIn("token_audit_summary_not_pass", invalid["reasons"])
        self.assertIn("token_audit_summary_contains_failures", invalid["reasons"])


if __name__ == "__main__":
    unittest.main()
