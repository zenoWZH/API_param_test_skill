from __future__ import annotations

import pytest

from lib.load_rate import ConstantThroughputPlan, MeasurementWindow


def test_100_rpm_plan_uses_native_per_user_throughput() -> None:
    plan = ConstantThroughputPlan.build(100, 20)

    assert plan is not None
    assert plan.aggregate_rps == 100 / 60
    assert plan.slot_interval_sec == 0.6
    assert plan.per_user_rps == pytest.approx(1 / 12)
    assert plan.user_period_sec == 12
    assert plan.initial_offset_sec(0) == 0
    assert plan.initial_offset_sec(19) == 11.4
    assert plan.planned_requests(60) == 100


def test_one_period_warmup_produces_exact_steady_measurement_cohort() -> None:
    plan = ConstantThroughputPlan.build(100, 20)
    assert plan is not None
    warmup_sec = plan.user_period_sec
    measure_end = warmup_sec + 60
    starts: list[float] = []

    for user_index in range(plan.users):
        started_at = plan.initial_offset_sec(user_index)
        while started_at < measure_end - 1e-9:
            if warmup_sec - 1e-9 <= started_at < measure_end - 1e-9:
                starts.append(started_at)
            started_at += plan.user_period_sec

    assert len(starts) == 100
    assert min(starts) == 12
    assert max(starts) == 71.4


def test_disabled_target_needs_no_user_count() -> None:
    assert ConstantThroughputPlan.build(0, None) is None


def test_measurement_window_is_half_open_and_closes_at_exact_end() -> None:
    window = MeasurementWindow.build(12, 60)

    assert window is not None
    assert window.phase_for_elapsed(11.999) == "warmup"
    assert window.phase_for_elapsed(12) == "measure"
    assert window.phase_for_elapsed(71.999) == "measure"
    assert window.phase_for_elapsed(72) == "closed"
    assert window.admits_elapsed(71.999)
    assert not window.admits_elapsed(72)
