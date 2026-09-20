from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.web_console as web_console
from scripts.web_console import _command_for_job


def test_quick_load_command_adds_one_period_warmup_and_graceful_drain() -> None:
    command = _command_for_job(
        "quick_load",
        Path("/tmp/standard-pressure"),
        20,
        20,
        "60s",
        100,
        30,
    )

    assert command[command.index("-t") + 1] == "72s"
    assert command[command.index("--stop-timeout") + 1] == "30"


def test_quick_load_rejects_spawn_rate_below_arrival_rate() -> None:
    with pytest.raises(ValueError, match="spawn_rate"):
        _command_for_job(
            "quick_load",
            Path("/tmp/standard-pressure"),
            20,
            1,
            "60s",
            100,
            30,
        )


def test_every_post_transport_has_an_immediate_second_send_gate() -> None:
    source = (Path(__file__).resolve().parents[1] / "locustfile.py").read_text(
        encoding="utf-8"
    )

    assert source.count(
        "timestamp = _admit_before_send(request_id)\n        with self.client.post("
    ) == 5


def test_legacy_locust_cache_workload_fails_closed() -> None:
    source = (web_console.PROJECT_ROOT / "locustfile.py").read_text(encoding="utf-8")

    assert 'if WORKLOAD == "cache_suite":' in source
    assert "does not provide valid cache-hit evidence" in source
    assert "Use scripts/run_cache.py" in source


def test_attempt_ledger_reconstruction_subtracts_cancelled_before_send(
    tmp_path: Path,
) -> None:
    admitted = {
        "event": "admitted",
        "request_id": "request-1",
        "timestamp": 171,
        "admitted_elapsed_sec": 71,
        "group": "throughput_profiles",
        "is_warmup": False,
        "extra": {
            "target_rpm": 100,
            "configured_users": 20,
            "warmup_sec": 12,
            "measure_duration_sec": 60,
            "measure_started_at": 112,
            "measure_ended_at": 172,
        },
    }
    cancelled = {
        "event": "cancelled_before_send",
        "request_id": "request-1",
        "timestamp": 172,
        "elapsed_sec": 72,
    }

    window = web_console._load_result_window(
        tmp_path, [], [admitted, cancelled]
    )

    assert window is not None
    assert window["admitted_request_count"] == 1
    assert window["cancelled_before_send_count"] == 1
    assert window["started_request_count"] == 0
    assert window["unfinished_after_drain_count"] == 0
    assert window["admission_conservation_ok"] is True
    assert window["completion_conservation_ok"] is True


def test_zero_completion_load_window_still_generates_main_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports_root = tmp_path / "reports"
    report_dir = reports_root / "jobs" / "zero-completion"
    report_dir.mkdir(parents=True)
    load_window = {
        "schema_version": 1,
        "status": "completed",
        "scheduler": "locust.constant_throughput",
        "scheduler_semantics": "closed_loop_capped_throughput",
        "target_rpm": 100,
        "users": 20,
        "per_user_rps": 1 / 12,
        "warmup_sec": 12,
        "measure_sec": 60,
        "measure_started_at": 112,
        "measure_ended_at": 172,
        "planned_start_count": 100,
        "admitted_request_count": 101,
        "cancelled_before_send_count": 1,
        "unresolved_admission_count": 0,
        "started_request_count": 100,
        "completed_request_count": 0,
        "successful_request_count": 0,
        "unfinished_after_drain_count": 100,
        "unscheduled_count": 0,
        "overrun_start_count": 0,
    }
    (report_dir / "load_window.json").write_text(
        json.dumps(load_window), encoding="utf-8"
    )
    monkeypatch.setattr(web_console, "REPORTS_ROOT", reports_root)

    result = web_console._ensure_load_result_for_dir(
        report_dir,
        result_type="quick_load",
        provider="provider-a",
        model="model-a",
        workload="throughput_rpm",
        status="completed",
    )

    assert result is not None
    assert result["load_window"] == load_window
    assert result["summary"]["attempted_business_rpm"] == 100
    assert result["summary"]["started_request_count"] == 100
    assert result["summary"]["completed_business_request_count"] == 0
    assert result["summary"]["unfinished_after_drain_count"] == 100
    assert result["summary"]["admission_conservation_ok"] is True
    assert result["summary"]["completion_conservation_ok"] is True
    assert result["profile_stats"][0]["request_count"] == 0
    assert (report_dir / "load_result.json").exists()
