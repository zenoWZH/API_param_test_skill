"""Current cache controls and immutable historical plans, without API traffic."""
import copy
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib import anthropic_cache_reference as cache
from lib import anthropic_cache_legacy_20260908 as legacy
from lib.fixed_parameter_specs import fixed_spec_payload, fixed_suite_descriptor
from test_anthropic_cache_reference import (author, shared, candidate_catalog, snapshots, snapshot,
    _plan, _native, _record, _records, _change_response, NONCES)
from test_anthropic_cache_runner import client, Response, session_fixture
from lib import anthropic_cache_runner as runner
from lib import approved_report_retention as retention


def current(snapshot):
    return cache.build_cache_plan(snapshot, suite_id=cache.suite_id(snapshot["model_slug"]),
        create_runtime_nonce=True, nonce_factory=Mock(side_effect=list(NONCES.values())))


def controls(plan):
    # A real hit is enough, even below the old documented minimum and greater
    # than the preceding write count. Full input stays equal and valid.
    return [_record(plan, 0, _native(plan, read=0, write=1, total=8192)),
        _record(plan, 1, _native(plan, read=2, write=0, total=8192)),
        _record(plan, 2, _native(plan, read=0, write=0, total=8192))]


def test_historical_validator_archive_is_byte_pinned():
    assert hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest() == "3be354a308c322f346c296cec5d785f6f8b3cc0675407baa5ba092fd4a0604c6"


@pytest.mark.parametrize("model", cache.MODELS)
def test_new_plans_have_three_controls_without_official_numeric_gate(snapshots, model):
    plan = current(snapshots[model])
    assert plan.schema_version == 2 and plan.request_cap == 3
    assert [r.label for r in plan.requests] == ["cold", "repeat", "negative"]
    assert all(r.kind == "generation" and r.endpoint == cache.ENDPOINT for r in plan.requests)
    result = cache.evaluate_cache_plan(plan, controls(plan))
    assert result["cache_effect_verified"] and result["scenario_acceptance"]["status"] == "pass"
    assert result["minimum_prefix_tokens"] is None and result["count_requests_sent"] == 0
    assert not result["official_numeric_reference_required"]
    assert not any("read_lte_cold_creation" in row for row in result["case_results"])
    assert cache.next_cache_request(plan, []).label == "cold"
    assert cache.next_cache_request(plan, controls(plan)) is None
    spec = fixed_spec_payload(plan)
    descriptor = fixed_suite_descriptor(plan.suite_id)
    assert spec["fixed_request_count"] == descriptor["request_count"] == 3
    assert spec["count_request_count"] == descriptor["count_request_count"] == 0


@pytest.mark.parametrize("mutation", ["cold_read", "repeat_miss", "negative_read", "missing_read", "bad_type", "unequal_input", "identity"])
def test_current_policy_preserves_native_control_failures(snapshot, mutation):
    plan = current(snapshot)
    records = controls(plan)
    if mutation == "cold_read": _change_response(records, 0, lambda p: p["usage"].update(cache_read_input_tokens=1, input_tokens=8190))
    if mutation == "repeat_miss": _change_response(records, 1, lambda p: p["usage"].update(cache_read_input_tokens=0, input_tokens=8192))
    if mutation == "negative_read": _change_response(records, 2, lambda p: p["usage"].update(cache_read_input_tokens=1, input_tokens=8191))
    if mutation == "missing_read": _change_response(records, 1, lambda p: p["usage"].pop("cache_read_input_tokens"))
    if mutation == "bad_type": _change_response(records, 1, lambda p: p["usage"].update(cache_read_input_tokens=True))
    if mutation == "unequal_input": _change_response(records, 1, lambda p: p["usage"].update(input_tokens=8191))
    if mutation == "identity": _change_response(records, 1, lambda p: p.update(model="other"))
    assert not cache.evaluate_cache_plan(plan, records)["cache_effect_verified"]


@pytest.mark.parametrize("mutation", ["version", "policy", "missing_policy", "expectation", "count", "body", "retention"])
def test_rehashed_v2_plan_cannot_change_its_policy_or_wires(snapshot, mutation):
    plan = current(snapshot)
    value = plan.frozen_payload
    if mutation == "version": value["schema_version"] = 1
    if mutation == "policy": value["cache_evaluation_policy"]["official_numeric_reference_required"] = True
    if mutation == "missing_policy": value.pop("cache_evaluation_policy")
    if mutation == "expectation": value["requests"][0]["expectation"]["expected_hit"] = True
    if mutation == "count": value["requests"][0]["kind"] = "count"
    if mutation == "body": value["requests"][1]["body"]["max_tokens"] = 256
    if mutation == "retention": value["report_retention"] = "P1Y"
    value["plan_digest"] = cache.digest({k: v for k, v in value.items() if k != "plan_digest"})
    with pytest.raises(ValueError): cache.build_cache_plan(snapshot, suite_id=plan.suite_id, frozen_plan=value)


def test_frozen_v1_has_original_requests_and_numerical_verdict(snapshot):
    plan = _plan(snapshot)
    historical = legacy.build_cache_plan(snapshot, suite_id=plan.suite_id, frozen_plan=plan.frozen_payload)
    records = _records(plan)
    assert plan.schema_version == 1 and plan.request_cap == 5
    assert cache.evaluate_cache_plan(plan, records) == legacy.evaluate_cache_plan(historical, records)
    _change_response(records, 2, lambda p: p["usage"].update(cache_creation_input_tokens=1, input_tokens=4195))
    assert not cache.evaluate_cache_plan(plan, records)["pass"]
    assert cache.evaluate_cache_plan(plan, records) == legacy.evaluate_cache_plan(historical, records)


@pytest.mark.parametrize("model", cache.MODELS)
def test_current_transport_sends_three_frozen_requests_and_never_counts(snapshots, model, client, tmp_path, monkeypatch):
    plan = current(snapshots[model])
    monkeypatch.setenv("LLM_API_TEST_REPORTS_DIR", str(tmp_path))
    report = retention.cache_report_directory()
    retention.initialize_cache_report(report, plan.suite_id, on_exit=False)
    calls = []
    session_fixture(monkeypatch, [Response(row) for row in controls(plan)], calls)
    actual, observed = runner.execute_cache_plan(client, plan, report)
    assert len(actual) == len(calls) == observed["request_cap"] == 3
    assert observed["cache_effect_verified"] and observed["count_requests_sent"] == 0
    assert [url for _, url, _ in calls] == [cache.ENDPOINT] * 3
    assert [kwargs["data"] for _, _, kwargs in calls] == [r.body_bytes for r in plan.requests]
