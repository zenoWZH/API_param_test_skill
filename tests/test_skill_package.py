from __future__ import annotations

import importlib.util
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_runtime_root() -> str:
    executable = Path(sys.executable).absolute()
    if executable.parent.name == "bin" and executable.parent.parent.name == "venv":
        return str(executable.parents[2])
    return os.environ.get("LLM_API_TEST_RUNTIME_DIR") or str(executable.parents[2])


def test_skill_frontmatter_and_openai_interface_are_valid() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, raw, _ = text.split("---", 2)
    frontmatter = yaml.safe_load(raw)
    assert set(frontmatter) == {"name", "description", "license", "metadata"}
    assert frontmatter["name"] == "llm-api-test"
    assert frontmatter["metadata"]["version"] == json.loads((SKILL_ROOT / "_meta.json").read_text())["version"]
    assert frontmatter["metadata"]["openclaw"]["os"] == ["linux"]

    openai = yaml.safe_load(
        (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    assert "$llm-api-test" in openai["interface"]["default_prompt"]
    assert 25 <= len(openai["interface"]["short_description"]) <= 64


def test_launcher_help_is_cwd_independent(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(SKILL_ROOT / "bin" / "llm-api-test"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "doctor" in completed.stdout
    assert "sweep" in completed.stdout


def test_configure_skill_env_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    data = tmp_path / "not-created-data"
    runtime = tmp_path / "not-created-runtime"
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(SKILL_ROOT / 'scripts')!r});"
        "import skill_env; skill_env.configure_skill_env()"
    )
    env = os.environ.copy()
    env.update(
        LLM_API_TEST_DATA_DIR=str(data),
        LLM_API_TEST_RUNTIME_DIR=str(runtime),
    )
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    assert not data.exists()
    assert not runtime.exists()


def test_doctor_verifies_all_bundled_artifacts() -> None:
    doctor = _load_module("skill_doctor", SKILL_ROOT / "scripts" / "doctor.py")
    mpdb, errors = doctor._artifact_audit()
    assert errors == []
    assert mpdb["verified_artifacts"] == mpdb["artifact_count"] == 8
    assert mpdb["catalog_version"] == "0.4.0"


def test_doctor_rejects_database_pin_and_bundled_code_drift(tmp_path, monkeypatch) -> None:
    import hashlib
    doctor = _load_module("skill_doctor_migration", SKILL_ROOT / "scripts" / "doctor.py")
    monkeypatch.setattr(doctor, "SKILL_ROOT", tmp_path)
    path = tmp_path / "engine.py"
    path.write_bytes(b"original")
    expected = {"package_version": "0.3.0", "mpdb_schema_version": 3,
                "catalog_version": "0.4.0", "catalog_digest": "core", "test_extension_digest": "extension"}
    manifest = {"model_profile_database": expected, "bundled_files": {
        "engine.py": {"size": 8, "sha256": hashlib.sha256(b"original").hexdigest()}}}
    (tmp_path / "MIGRATION_MANIFEST.json").write_text(json.dumps(manifest))
    assert doctor._migration_audit(expected)[1] == []
    assert doctor._migration_audit({**expected, "catalog_digest": "stale"})[1] == [
        "migration/database identity mismatch: catalog_digest"]
    path.write_bytes(b"modified")
    assert doctor._migration_audit(expected)[1] == ["bundled file drift: engine.py"]


def test_legacy_live_model_fact_sources_are_absent() -> None:
    assert not (SKILL_ROOT / "app" / "api_reference_specs.yaml").exists()
    assert not (SKILL_ROOT / "app" / "model_capability_profiles.yaml").exists()
    assert not (SKILL_ROOT / "scripts" / "register_model.py").exists()
    workflow = (SKILL_ROOT / "scripts" / "workflow.py").read_text(encoding="utf-8")
    assert "LLM_API_TEST_PROFILES_LOCAL" not in workflow
    assert "model_capability_profiles.local.yaml" not in workflow
    assert "llm-api-test.mpdb-review-proposal.v1" in workflow


def test_provider_discovery_does_not_claim_live_availability(tmp_path: Path) -> None:
    runtime_root = _test_runtime_root()
    env = os.environ.copy()
    env.update(
        LLM_API_TEST_DATA_DIR=str(tmp_path / "data"),
        LLM_API_TEST_RUNTIME_DIR=runtime_root,
        LOADTEST_SKIP_DOTENV="1",
    )
    completed = subprocess.run(
        ["bash", str(SKILL_ROOT / "bin" / "llm-api-test"), "providers"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    providers = json.loads(completed.stdout)
    assert providers
    for provider in providers:
        assert "active" not in provider
        assert isinstance(provider["selected_by_default"], bool)
        assert isinstance(provider["credential_available"], bool)
        assert provider["live_readiness"] == "unknown_until_completed_probe"


def test_console_defaults_to_loopback_and_does_not_auto_download() -> None:
    script = (SKILL_ROOT / "scripts" / "console.sh").read_text(encoding="utf-8")
    assert 'HOST="${WEB_CONSOLE_HOST:-127.0.0.1}"' in script
    assert "releases/latest/download" not in script
    assert "passwd --reveal" in script
    assert "--token is refused" in script


def test_sweep_execution_requires_explicit_bounds_and_confirmation(tmp_path: Path) -> None:
    env = os.environ.copy()
    runtime_root = _test_runtime_root()
    env.update(
        LLM_API_TEST_DATA_DIR=str(tmp_path / "data"),
        LLM_API_TEST_RUNTIME_DIR=runtime_root,
        LOADTEST_SKIP_DOTENV="1",
    )
    completed = subprocess.run(
        [
            "bash",
            str(SKILL_ROOT / "bin" / "llm-api-test"),
            "sweep",
            "--provider",
            "yibu",
            "--models",
            "deepseek-v4-flash",
            "--execute",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert "--yes" in payload["missing"]
    assert "--target-rpm" in payload["missing"]


def test_sweep_execution_rejects_unsafe_values_before_launch(
    monkeypatch,
    capsys,
) -> None:
    sweep = _load_module(
        "unsafe_sweep_values", SKILL_ROOT / "scripts" / "model_sweep.py"
    )
    monkeypatch.setattr(
        sweep,
        "_candidate_plan",
        lambda provider, requested: {
            "schema": "llm-api-test.model-sweep-plan.v1",
            "provider": provider,
            "models": [{"model": requested[0], "eligible": True}],
            "all_requested_eligible": True,
        },
    )
    monkeypatch.setattr(
        sweep.skill_env,
        "ensure_skill_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("unsafe plan reached runtime launch")
        ),
    )
    invalid_options = [
        ["--target-rpm", "-1"],
        ["--target-rpm", "nan"],
        ["--duration", "0s"],
        ["--users", "-1"],
        ["--spawn-rate", "0"],
        ["--workload", "cache_suite"],
    ]
    base = [
        "model_sweep.py",
        "--provider",
        "yibu",
        "--models",
        "deepseek-v4-flash",
        "--target-rpm",
        "60",
        "--duration",
        "60s",
        "--users",
        "2",
        "--spawn-rate",
        "1",
        "--workload",
        "throughput_rpm",
        "--execute",
        "--yes",
    ]
    for replacement in invalid_options:
        argv = list(base)
        flag = replacement[0]
        argv[argv.index(flag) + 1] = replacement[1]
        monkeypatch.setattr(sys, "argv", argv)
        assert sweep.main() == 2
        error = json.loads(capsys.readouterr().err)
        assert error["error"].startswith("invalid sweep execution plan:")


def test_sweep_keeps_mpdb_source_and_reference_contract_separate(
    tmp_path: Path,
) -> None:
    runtime_root = _test_runtime_root()
    env = os.environ.copy()
    env.update(
        LLM_API_TEST_DATA_DIR=str(tmp_path / "data"),
        LLM_API_TEST_RUNTIME_DIR=runtime_root,
        LOADTEST_SKIP_DOTENV="1",
    )
    completed = subprocess.run(
        [
            "bash",
            str(SKILL_ROOT / "bin" / "llm-api-test"),
            "sweep",
            "--provider",
            "yibu",
            "--models",
            "deepseek-v4-flash",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    row = json.loads(completed.stdout)["models"][0]
    assert row["source_id"] == "deepseek"
    assert row["reference_contract_id"] == "deepseek_chat"


def test_sweep_resolves_contract_within_explicit_source(monkeypatch) -> None:
    sweep = _load_module("cross_source_sweep", SKILL_ROOT / "scripts" / "model_sweep.py")
    import lib.config as config_module

    config = copy.deepcopy(config_module.load_config())
    provider = copy.deepcopy(config["providers"]["yibu"])
    provider["reference_source_id"] = "aliyun_maas"
    config["providers"]["cross_source"] = provider
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    row = sweep._candidate_plan("cross_source", ["deepseek-v4-flash"])["models"][0]
    assert row["eligible"] is True
    assert row["source_id"] == "aliyun_maas"
    assert row["profile_id"].startswith("text/aliyun_maas/")
    assert row["reference_contract_id"] == "aliyun_deepseek_v4_openai_compat"


def test_sweep_fails_closed_when_explicit_source_has_no_model_binding(
    monkeypatch,
) -> None:
    sweep = _load_module("missing_source_sweep", SKILL_ROOT / "scripts" / "model_sweep.py")
    import lib.config as config_module

    config = copy.deepcopy(config_module.load_config())
    provider = copy.deepcopy(config["providers"]["yibu"])
    provider["reference_source_id"] = "openai"
    config["providers"]["missing_source"] = provider
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    row = sweep._candidate_plan("missing_source", ["deepseek-v4-flash"])["models"][0]
    assert row["eligible"] is False
    assert "reference_contract_id" not in row
    assert "source='openai'" in row["reason"]


def test_openclaw_managed_bundle_limit_is_explicit() -> None:
    doctor = _load_module("bundle_doctor", SKILL_ROOT / "scripts" / "doctor.py")
    bundle = doctor._bundle_stats()
    assert bundle["openclaw_managed_bundle_compatible"] is False
    assert "packages/model-profile-db/model_profile_db/data/catalog.sqlite3" in bundle[
        "files_over_1_mib"
    ]


def test_missing_job_is_an_error_instead_of_successful_null(tmp_path: Path) -> None:
    runtime_root = _test_runtime_root()
    env = os.environ.copy()
    env.update(
        LLM_API_TEST_DATA_DIR=str(tmp_path / "data"),
        LLM_API_TEST_RUNTIME_DIR=runtime_root,
    )
    completed = subprocess.run(
        [
            "bash",
            str(SKILL_ROOT / "bin" / "llm-api-test"),
            "jobs",
            "--id",
            "not-a-job",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["error"] == "job not found: not-a-job"
