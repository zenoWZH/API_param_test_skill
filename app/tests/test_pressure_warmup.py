from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def _rpm_request_mode_guard(project_root: Path):
    """Execute only Locust's real workload guard, without importing its runner."""
    module = ast.parse((project_root / "locustfile.py").read_text(encoding="utf-8"))
    guards = [
        node
        for node in module.body
        if isinstance(node, ast.If)
        and {
            child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)
        } == {"WORKLOAD", "REQUEST_MODE"}
    ]
    assert len(guards) == 1
    return compile(ast.Module(body=guards, type_ignores=[]), "<rpm-mode-guard>", "exec")


@pytest.mark.parametrize("runner_name", ["run_staircase", "run_soak"])
@pytest.mark.parametrize("requested_mode", [None, "unique", "fixed"])
@pytest.mark.parametrize("workload", ["throughput_rpm", "throughput_balanced"])
def test_warmup_mode_does_not_change_measurement_or_caller_environment(
    tmp_path, monkeypatch, runner_name, requested_mode, workload
):
    runner = importlib.import_module(f"scripts.{runner_name}")
    inherited = {} if requested_mode is None else {"LOADTEST_REQUEST_MODE": requested_mode}
    original = dict(inherited)
    calls = []
    monkeypatch.setattr(runner, "get_active_provider_name", lambda config: "offline")
    monkeypatch.setattr(
        runner, "build_provider_child_env", lambda config, provider: dict(inherited)
    )

    def capture_child(command, **kwargs):
        calls.append(kwargs["env"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", capture_child)
    # Prevent ambient load targets from affecting the isolated Staircase check.
    for name in ("LOADTEST_TARGET_RPM", "LOADTEST_TARGET_TPM"):
        monkeypatch.delenv(name, raising=False)
    extra = {"staircase_step": 1} if runner_name == "run_staircase" else {}
    for phase in ("warmup", "measure"):
        runner.run_locust(
            config={},
            report_dir=tmp_path / phase,
            users=10,
            spawn_rate=2,
            duration="1m",
            workload=workload,
            phase=phase,
            **extra,
        )

    warmup_env, measure_env = calls
    assert inherited == original
    assert measure_env.get("LOADTEST_REQUEST_MODE") == requested_mode
    guard = _rpm_request_mode_guard(runner.PROJECT_ROOT)
    if workload == "throughput_rpm":
        # The default RPM warmup must satisfy the unchanged Locust guard.
        exec(guard, {
            "WORKLOAD": workload,
            "REQUEST_MODE": warmup_env.get("LOADTEST_REQUEST_MODE", "unique"),
        })
        assert warmup_env["LOADTEST_REQUEST_MODE"] == "fixed"
        if requested_mode != "fixed":
            # An invalid measurement choice remains an error, not a silent
            # conversion of the user's measurement traffic to fixed bodies.
            with pytest.raises(RuntimeError, match="must be fixed"):
                exec(guard, {
                    "WORKLOAD": workload,
                    "REQUEST_MODE": measure_env.get("LOADTEST_REQUEST_MODE", "unique"),
                })
    else:
        assert warmup_env.get("LOADTEST_REQUEST_MODE") == requested_mode
