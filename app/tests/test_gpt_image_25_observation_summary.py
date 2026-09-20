from __future__ import annotations

import copy
import pytest
from lib.gpt_image_25 import summarize_observations


def sample_rows():
    return [
        {"case": "baseline", "pass": True, "overall_pass": True, "compatibility_pass": True,
         "metadata": {"gpt_image_25": True}},
        {"case": "fidelity", "pass": False, "overall_pass": False, "compatibility_pass": False,
         "diagnostic_pass": True, "status": "observed_parameter_rejection",
         "metadata": {"gpt_image_25": True}},
    ]


def test_resolved_observation_does_not_force_mixed_matrix_failure():
    rows = sample_rows()
    original = copy.deepcopy(rows)
    summary = {"pass": False, "planned_case_count": 2, "token_validation_pass": True,
               "model_identity_pass": True}
    summarize_observations(summary, rows)
    assert summary["pass"] is True
    assert summary["overall_pass"] is True
    assert summary["certified_route_contract_pass"] is False
    assert summary["observed_parameter_rejection_count"] == 1
    assert summary["pass_count"] == 1
    assert summary["failure_count"] == 0
    assert rows == original


@pytest.mark.parametrize("failure", ["contract", "observation", "missing", "duplicate", "token", "identity"])
def test_real_failure_or_incomplete_execution_still_blocks_mixed_matrix(failure):
    rows = sample_rows()
    summary = {"planned_case_count": 2}
    if failure == "contract":
        rows[0]["overall_pass"] = False
    elif failure == "observation":
        rows[1]["diagnostic_pass"] = False
    elif failure == "missing":
        summary["planned_case_count"] = 3
        summary["not_executed_count"] = 1
    elif failure == "duplicate":
        rows[1]["case"] = rows[0]["case"]
    elif failure == "token":
        summary["token_validation_pass"] = False
    elif failure == "identity":
        summary["model_identity_pass"] = False
    summarize_observations(summary, rows)
    assert summary["pass"] is False
    assert summary["overall_pass"] is False


def test_observation_only_run_does_not_claim_parameter_contract_pass():
    rows = sample_rows()[1:]
    summary = {"case_count": 1}
    summarize_observations(summary, rows)
    assert summary["diagnostic_pass"] is True
    assert summary["pass"] is False
    assert summary["compatibility_pass"] is False


def test_unrelated_matrix_summary_is_untouched():
    summary = {"pass": True, "case_count": 1}
    summarize_observations(summary, [{"case": "other", "pass": True, "metadata": {}}])
    assert summary == {"pass": True, "case_count": 1}
