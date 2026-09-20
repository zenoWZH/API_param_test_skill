from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ConstantThroughputPlan:
    """Translate one aggregate RPM target into Locust's per-user pacing."""

    target_rpm: float
    users: int

    @classmethod
    def build(cls, target_rpm: float, users: int | None) -> "ConstantThroughputPlan | None":
        rpm = float(target_rpm or 0)
        if rpm <= 0:
            return None
        if users is None or int(users) <= 0:
            raise ValueError("A positive LOADTEST_USERS value is required when target RPM is set.")
        return cls(target_rpm=rpm, users=int(users))

    @property
    def aggregate_rps(self) -> float:
        return self.target_rpm / 60.0

    @property
    def slot_interval_sec(self) -> float:
        return 1.0 / self.aggregate_rps

    @property
    def per_user_rps(self) -> float:
        return self.target_rpm / (60.0 * self.users)

    @property
    def user_period_sec(self) -> float:
        return 60.0 * self.users / self.target_rpm

    def initial_offset_sec(self, user_index: int) -> float:
        if user_index < 0 or user_index >= self.users:
            raise ValueError(
                f"user_index must be within [0, {self.users}); got {user_index}."
            )
        return user_index * self.slot_interval_sec

    def planned_requests(self, measure_sec: float) -> float:
        if measure_sec <= 0:
            raise ValueError("measure_sec must be positive.")
        return self.aggregate_rps * float(measure_sec)


def validate_fixed_rate_sweep_plan(
    *,
    target_rpm: float,
    duration_sec: float,
    users: int,
    spawn_rate: int,
    workload: str,
) -> ConstantThroughputPlan:
    """Reject an unsafe sweep before any billable request can be dispatched."""

    rpm = float(target_rpm)
    duration = float(duration_sec)
    if not math.isfinite(rpm) or rpm <= 0:
        raise ValueError("target_rpm must be finite and greater than zero")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and greater than zero")
    if int(users) <= 0:
        raise ValueError("users must be greater than zero")
    if int(spawn_rate) <= 0:
        raise ValueError("spawn_rate must be greater than zero")
    if str(workload) != "throughput_rpm":
        raise ValueError("fixed-rate model sweep only supports throughput_rpm")
    plan = ConstantThroughputPlan.build(rpm, int(users))
    assert plan is not None
    if float(spawn_rate) < plan.aggregate_rps:
        raise ValueError("spawn_rate must be at least target_rpm / 60")
    return plan


@dataclass(frozen=True)
class MeasurementWindow:
    """A half-open warmup/measurement window relative to one run epoch."""

    warmup_sec: float
    measure_sec: float

    @classmethod
    def build(
        cls,
        warmup_sec: float,
        measure_sec: float,
    ) -> "MeasurementWindow | None":
        warmup = max(float(warmup_sec or 0), 0.0)
        measure = float(measure_sec or 0)
        if measure <= 0:
            return None
        return cls(warmup_sec=warmup, measure_sec=measure)

    @property
    def measure_start_elapsed_sec(self) -> float:
        return self.warmup_sec

    @property
    def measure_end_elapsed_sec(self) -> float:
        return self.warmup_sec + self.measure_sec

    def phase_for_elapsed(self, elapsed_sec: float) -> str:
        elapsed = float(elapsed_sec)
        if elapsed < self.measure_start_elapsed_sec:
            return "warmup"
        if elapsed < self.measure_end_elapsed_sec:
            return "measure"
        return "closed"

    def admits_elapsed(self, elapsed_sec: float) -> bool:
        return self.phase_for_elapsed(elapsed_sec) != "closed"
