from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env
from result_validation import classify_result

DATA_DIR = skill_env.configure_skill_env()
sys.path.insert(0, str(skill_env.APP_ROOT))

import yaml  # noqa: E402

WORKFLOW_PATH = skill_env.APP_ROOT / "workflow.yaml"
WORKFLOWS_DIR = DATA_DIR / "workflows"


def _load_workflow_def() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _instance_path(provider: str, model: str) -> Path:
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in s)
    return WORKFLOWS_DIR / f"{safe(provider)}__{safe(model)}.json"


def _load_instance(provider: str, model: str) -> dict | None:
    path = _instance_path(provider, model)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_instance(instance: dict) -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    instance["updated_at"] = time.time()
    path = _instance_path(instance["provider"], instance["model"])
    skill_env.atomic_write_private_text(
        path,
        json.dumps(instance, ensure_ascii=False, indent=2),
    )


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _jobs_root() -> Path:
    from lib.config import default_reports_root

    return default_reports_root() / "jobs"


def _verdict_for_job(job_id: str) -> dict:
    try:
        report_dir = skill_env.resolve_job_dir(_jobs_root(), job_id)
    except ValueError:
        return {}
    spec = _read_json(report_dir / "job_spec.json") or {}
    return _qualification_verdict(spec, _read_json(report_dir / "verdict.json") or {})


def _qualification_verdict(spec: dict, verdict: dict) -> dict:
    validation = classify_result(spec, verdict)
    if validation is None:
        return verdict
    result = {**verdict, "reported_pass": verdict.get("pass"), "result_validation": validation}
    if not validation.get("current"):
        result["pass"] = None
        result["qualification_gap"] = "historical_or_incomplete_parameter_evidence"
    elif (
        ("test_workflow_snapshot" in spec and verdict.get("full_parameter_certification") is not True)
        or verdict.get("full_parameter_matrix_verified") is False
        or (spec.get("execution_plan") or {}).get("definition", {}).get("factory", {}).get("factory_id") == "source_fixed_parameter"
    ):
        result["pass"] = None
        result["qualification_gap"] = "bounded_workflow_is_not_full_parameter_certification"
    else:
        result["pass"] = validation["pass"]
    return result


def _history_tested(provider: str, model: str) -> bool:
    path = _instance_path(provider, model)
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior.get("current_node") == "done":
            return True
    jobs_root = _jobs_root()
    if jobs_root.exists():
        for report_dir in jobs_root.iterdir():
            spec = _read_json(report_dir / "job_spec.json") or {}
            if spec.get("provider") == provider and spec.get("model") == model:
                verdict = _read_json(report_dir / "verdict.json")
                if verdict:
                    return True
    return False


def _auto_outcome(node: dict, instance: dict) -> str | None:
    auto = node.get("auto")
    if auto == "history_lookup":
        return (
            "tested"
            if _history_tested(instance["provider"], instance["model"])
            else "not_tested"
        )
    source_node = node.get("source_node")
    job_id = (instance.get("job_ids") or {}).get(source_node or "")
    if not job_id:
        return None
    verdict = _verdict_for_job(job_id)
    if not verdict:
        return None
    if auto == "verdict_pass":
        if not isinstance(verdict.get("pass"), bool):
            return None
        return "pass" if verdict.get("pass") is True else "fail"
    if auto == "verdict_match":
        passed = verdict.get("pass")
        if passed is None:
            return None
        return "match" if passed is True else "mismatch"
    return None


def _python_prefix() -> list[str]:
    import shutil

    uv = shutil.which("uv")
    if not uv:
        candidate = Path.home() / ".local" / "bin" / "uv"
        uv = str(candidate) if candidate.exists() else None
    if uv:
        return [uv, "run", "--python", str(skill_env.venv_python())]
    return [str(skill_env.venv_python())]


def _run_command_for(node: dict, instance: dict, extra: list[str]) -> list[str]:
    run = node.get("run") or {}
    cmd = _python_prefix() + [
        str(skill_env.SKILL_ROOT / "scripts" / "run_test.py"),
        "--type",
        str(run.get("type")),
        "--provider",
        instance["provider"],
        "--model",
        instance["model"],
    ]
    if node.get("needs_expect") and instance.get("expected_upstream"):
        cmd += ["--expect", str(instance["expected_upstream"])]
    cmd += list(
        (instance.get("node_args") or {}).get(instance.get("current_node")) or []
    )
    cmd += extra
    return cmd


def cmd_start(args: argparse.Namespace) -> int:
    existing = _load_instance(args.provider, args.model)
    if existing and not args.restart:
        print(
            json.dumps(
                {
                    "error": "instance already exists; use status/next to resume, or --restart",
                    "current_node": existing.get("current_node"),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    wf = _load_workflow_def()
    instance = {
        "workflow_version": wf.get("version"),
        "provider": args.provider,
        "model": args.model,
        "expected_upstream": args.expect_upstream,
        "current_node": args.entry or wf.get("entry"),
        "history": [],
        "job_ids": {},
        "human_gates": {},
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_instance(instance)
    print(json.dumps(instance, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    instances = []
    if WORKFLOWS_DIR.exists():
        for path in sorted(WORKFLOWS_DIR.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            instances.append(
                {
                    "provider": item.get("provider"),
                    "model": item.get("model"),
                    "current_node": item.get("current_node"),
                    "updated_at": item.get("updated_at"),
                }
            )
    print(json.dumps(instances, ensure_ascii=False, indent=2))
    return 0


def _describe_node(wf: dict, instance: dict) -> dict:
    node_id = instance["current_node"]
    node = (wf.get("nodes") or {}).get(node_id) or {}
    description = {
        "provider": instance["provider"],
        "model": instance["model"],
        "current_node": node_id,
        "label": node.get("label"),
        "kind": node.get("kind"),
        "history": instance.get("history") or [],
    }
    kind = node.get("kind")
    if kind == "human_gate":
        description["prompt"] = node.get("prompt")
        description["valid_outcomes"] = (
            list((node.get("outcomes") or {}).keys()) or None
        )
    elif kind == "auto_test":
        description["command"] = _run_command_for(node, instance, ["--background"])
        description["after"] = (
            "任务完成后运行 result.py --id <job_id> 查看结果，然后 "
            "workflow.py advance --auto 进入下一节点"
        )
    elif kind == "decision":
        description["auto"] = node.get("auto")
        description["valid_outcomes"] = list((node.get("outcomes") or {}).keys())
        description["hint"] = "可用 advance --auto 自动判定"
    elif kind == "onboard":
        description["hint"] = (
            "运行 workflow.py onboard-propose 生成 MPDB 审核证据；模型事实经独立审核并"
            "同步为 bundled MPDB 源码快照后，使用 onboard-apply --yes --review-ref <ref> 验证"
            "当前 binding 并结束流程。该命令不写模型数据库。"
        )
    return description


def cmd_status(args: argparse.Namespace) -> int:
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(
            json.dumps({"error": "no instance; use start"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            _describe_node(_load_workflow_def(), instance), ensure_ascii=False, indent=2
        )
    )
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(
            json.dumps({"error": "no instance; use start"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    wf = _load_workflow_def()
    description = _describe_node(wf, instance)
    node = (wf.get("nodes") or {}).get(instance["current_node"]) or {}
    if node.get("kind") == "auto_test" and args.execute:
        cmd = _run_command_for(
            node, instance, [] if args.foreground else ["--background"]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            payload = {"stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
        if payload.get("job_id"):
            instance.setdefault("job_ids", {})[instance["current_node"]] = payload[
                "job_id"
            ]
            _save_instance(instance)
        description["executed"] = payload
        description["returncode"] = proc.returncode
    print(json.dumps(description, ensure_ascii=False, indent=2))
    return int(description.get("returncode") or 0)


def cmd_advance(args: argparse.Namespace) -> int:
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(json.dumps({"error": "no instance"}, ensure_ascii=False), file=sys.stderr)
        return 2
    wf = _load_workflow_def()
    node_id = instance["current_node"]
    node = (wf.get("nodes") or {}).get(node_id)
    if node is None:
        print(
            json.dumps({"error": f"unknown node {node_id}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    outcome = args.outcome
    kind = node.get("kind")
    if (
        kind == "auto_test"
        and not args.job_id
        and not (instance.get("job_ids") or {}).get(node_id)
    ):
        print(
            json.dumps(
                {
                    "error": "auto_test node requires a launched job first",
                    "hint": "run: workflow.py next --execute (or advance --job-id <id>)",
                    "node": node_id,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if args.job_id:
        try:
            report_dir = skill_env.resolve_job_dir(_jobs_root(), args.job_id)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        if not report_dir.is_dir():
            print(
                json.dumps({"error": f"job not found: {args.job_id}"}),
                file=sys.stderr,
            )
            return 2
        record_node = (
            node.get("source_node")
            if kind == "decision" and node.get("source_node")
            else node_id
        )
        instance.setdefault("job_ids", {})[record_node] = args.job_id
    if args.auto and (kind == "decision" or node.get("auto")):
        outcome = _auto_outcome(node, instance)
        if outcome is None:
            print(
                json.dumps(
                    {
                        "error": "cannot auto-resolve outcome (missing verdict?)",
                        "node": node_id,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    if kind == "decision" or (node.get("outcomes") and kind == "human_gate"):
        outcomes = node.get("outcomes") or {}
        if outcome not in outcomes:
            print(
                json.dumps(
                    {"error": f"invalid outcome {outcome!r}", "valid": list(outcomes)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        next_node = outcomes[outcome]
    elif kind == "terminal":
        print(
            json.dumps({"error": "instance is done"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    else:
        next_node = node.get("next")
        if not next_node:
            print(
                json.dumps({"error": "node has no next"}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
    entry = {
        "node": node_id,
        "outcome": outcome,
        "notes": args.notes,
        "job_id": args.job_id,
        "ts": time.time(),
    }
    instance.setdefault("history", []).append(entry)
    if kind == "human_gate":
        instance.setdefault("human_gates", {})[node_id] = {
            "outcome": outcome,
            "notes": args.notes,
            "ts": time.time(),
        }
    instance["current_node"] = next_node
    _save_instance(instance)
    print(json.dumps(_describe_node(wf, instance), ensure_ascii=False, indent=2))
    return 0


class OnboardPrerequisiteError(ValueError):
    pass


def _last_history_entry(instance: dict, node_id: str) -> dict | None:
    for entry in reversed(instance.get("history") or []):
        if entry.get("node") == node_id:
            return entry
    return None


def _job_verdict(instance: dict, node_id: str) -> dict:
    job_id = (instance.get("job_ids") or {}).get(node_id)
    if not job_id:
        raise OnboardPrerequisiteError(f"missing {node_id} job")
    try:
        report_dir = skill_env.resolve_job_dir(_jobs_root(), str(job_id))
    except ValueError as exc:
        raise OnboardPrerequisiteError(f"invalid {node_id} job id") from exc
    spec = _read_json(report_dir / "job_spec.json") or {}
    expected_type = {
        "param_test": "param_test",
        "trace_test": "trace_test",
        "concurrency_test": "staircase",
    }.get(node_id)
    if (
        spec.get("provider") != instance.get("provider")
        or spec.get("model") != instance.get("model")
        or spec.get("type") != expected_type
    ):
        raise OnboardPrerequisiteError(
            f"{node_id} job does not match this provider/model/test type"
        )
    verdict = _read_json(report_dir / "verdict.json") or {}
    if not verdict:
        raise OnboardPrerequisiteError(f"missing {node_id} verdict")
    verified = _qualification_verdict(spec, verdict)
    if not isinstance(verified.get("pass"), bool):
        raise OnboardPrerequisiteError(
            f"{node_id} evidence cannot qualify this workflow: "
            + str(verified.get("qualification_gap") or "unverified result")
        )
    return verified


def _onboard_evidence(instance: dict) -> str:
    if instance.get("current_node") != "onboard":
        raise OnboardPrerequisiteError(
            "onboard proposal/apply is allowed only at the onboard node"
        )

    history = instance.get("history") or []
    last_entry = history[-1] if history else {}
    maintenance = _last_history_entry(instance, "profile_maintenance")
    maintenance_gate = (instance.get("human_gates") or {}).get(
        "profile_maintenance"
    )
    if (
        last_entry.get("node") == "profile_maintenance"
        and maintenance
        and maintenance.get("outcome") == "onboarded"
        and maintenance_gate
        and maintenance_gate.get("outcome") == "onboarded"
    ):
        return "profile_maintenance_user_approved"

    concurrency_decision = _last_history_entry(instance, "concurrency_decision")
    if not concurrency_decision or concurrency_decision.get("outcome") != "pass":
        raise OnboardPrerequisiteError("concurrency decision has not passed")
    if _job_verdict(instance, "concurrency_test").get("pass") is not True:
        raise OnboardPrerequisiteError("concurrency verdict is not pass")

    history_check = _last_history_entry(instance, "check_history")
    supplier_quote = (instance.get("human_gates") or {}).get("supplier_quote")
    if history_check and history_check.get("outcome") == "tested":
        if not supplier_quote or not str(supplier_quote.get("notes") or "").strip():
            raise OnboardPrerequisiteError(
                "existing-provider route requires a recorded supplier quote"
            )
        return "existing_profile_concurrency_revalidated"

    param_verdict = _job_verdict(instance, "param_test")
    param_decision = _last_history_entry(instance, "param_decision")
    if not param_decision:
        raise OnboardPrerequisiteError("parameter decision is missing")
    if param_verdict.get("pass") is True:
        if param_decision.get("outcome") != "pass":
            raise OnboardPrerequisiteError("parameter decision does not match verdict")
        evidence = "supplier_onboarding_param_and_concurrency_passed"
    elif param_verdict.get("pass") is False:
        if param_decision.get("outcome") != "fail":
            raise OnboardPrerequisiteError("parameter decision does not match verdict")
        trace_decision = _last_history_entry(instance, "trace_decision")
        if not trace_decision or trace_decision.get("outcome") != "match":
            raise OnboardPrerequisiteError("trace decision has not matched")
        if _job_verdict(instance, "trace_test").get("pass") is not True:
            raise OnboardPrerequisiteError("trace verdict is not a match")
        evidence = "supplier_onboarding_trace_match_and_concurrency_passed"
    else:
        raise OnboardPrerequisiteError("parameter verdict has no boolean pass result")

    price_gate = (instance.get("human_gates") or {}).get("price_check")
    if not price_gate or price_gate.get("outcome") != "pass":
        raise OnboardPrerequisiteError("price approval has not passed")
    return evidence


def _build_profile_proposal(instance: dict) -> dict:
    evidence = _onboard_evidence(instance)
    provider = instance["provider"]
    model = instance["model"]
    verdict = {}
    for node_id in ("param_test",):
        job_id = (instance.get("job_ids") or {}).get(node_id)
        if job_id:
            verdict = _verdict_for_job(job_id)
            if verdict:
                break
    capability = verdict.get("model_capability_profile") or {}
    try:
        from lib.config import (
            get_model_api_form,
            get_model_family,
            get_model_route_profile,
            load_config,
        )

        config = load_config()
        configured_family = get_model_family(config, model, provider)
        route_profile = str(
            capability.get("route_profile")
            or get_model_route_profile(config, model, provider)
        )
        api_form = str(
            capability.get("api_form")
            or get_model_api_form(
                config,
                model,
                provider,
                route_profile=route_profile,
            )
        )
    except Exception as exc:
        raise OnboardPrerequisiteError(
            f"configured family/route/API form cannot be resolved: {exc}"
        ) from exc
    family = str(
        verdict.get("model_family")
        or capability.get("model_family")
        or configured_family
    )
    if not family or family == "unknown":
        raise OnboardPrerequisiteError("model family cannot be resolved")
    if family != configured_family:
        raise OnboardPrerequisiteError(
            f"evidence family {family!r} conflicts with configured family "
            f"{configured_family!r}"
        )
    reference_contract_id = verdict.get("reference_contract_id") or verdict.get(
        "reference_source"
    )
    existing_binding = None
    binding_error = None
    try:
        existing_binding = _resolve_existing_mpdb_binding(instance)
    except Exception as exc:
        binding_error = str(exc)
    return {
        "schema": "llm-api-test.mpdb-review-proposal.v1",
        "mutates_database": False,
        "review_status": "pending_independent_review",
        "execution_target": {
            "provider": provider,
            "request_model": model,
            "modality": "text",
            "family": family,
            "route_profile": route_profile,
            "api_form": api_form,
        },
        "evidence": {
            "qualification_path": evidence,
            "reference_contract_id": reference_contract_id,
            "job_ids": instance.get("job_ids") or {},
            "generated_at": time.time(),
        },
        "existing_mpdb_binding": existing_binding,
        "existing_mpdb_binding_error": binding_error,
        "required_catalog_entities": [
            "Source",
            "CanonicalModel",
            "Profile",
            "Interface",
            "Contract",
            "TestBinding",
        ],
        "next_action": (
            "If no exact executable binding exists, review official evidence in the "
            "api_pressure model-profile workflow, freeze an approved MPDB source snapshot, "
            "migrate it with its matching consumer into this skill, rerun setup, then "
            "run onboard-apply with the external review reference."
        ),
    }


def _resolve_existing_mpdb_binding(instance: dict) -> dict:
    from lib.config import (
        get_model_api_form,
        get_model_family,
        get_model_route_profile,
        load_config,
    )
    from lib.model_profile_catalog import resolve_runtime_parameter_config

    config = load_config()
    provider = instance["provider"]
    model = instance["model"]
    family = get_model_family(config, model, provider)
    route_profile = get_model_route_profile(config, model, provider)
    api_form = get_model_api_form(
        config,
        model,
        provider,
        route_profile=route_profile,
    )
    parameter_config = resolve_runtime_parameter_config(
        config,
        provider,
        model,
        family,
        route_profile,
        api_form,
    )
    policy = parameter_config["test_binding"]
    contract = parameter_config["contract"]
    database = parameter_config.get("model_profile_database") or {}
    return {
        "source_id": parameter_config.get("source_id"),
        "profile_id": parameter_config.get("profile_id"),
        "interface_id": parameter_config.get("interface_id"),
        "reference_contract_id": contract.get("contract_id"),
        "test_binding_id": parameter_config.get("test_binding_id"),
        "parameter_test_binding_id": (
            (parameter_config.get("parameter_test_binding") or {}).get(
                "test_binding_id"
            )
        ),
        "api_form": (parameter_config.get("interface") or {}).get("api_form"),
        "route_profile": route_profile,
        "parameter_test_enabled": policy.get("parameter_test_enabled"),
        "pressure_test_enabled": policy.get("pressure_test_enabled"),
        "catalog_version": database.get("catalog_version"),
        "catalog_digest": database.get("catalog_digest"),
    }


def cmd_onboard_propose(args: argparse.Namespace) -> int:
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(json.dumps({"error": "no instance"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        proposal = _build_profile_proposal(instance)
    except OnboardPrerequisiteError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(proposal, ensure_ascii=False, indent=2))
    return 0


def cmd_onboard_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            json.dumps(
                {
                    "error": "refused: re-run with --yes only after explicit user approval"
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(json.dumps({"error": "no instance"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        _onboard_evidence(instance)
        binding = _resolve_existing_mpdb_binding(instance)
    except OnboardPrerequisiteError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": "exact executable MPDB binding is still unavailable",
                    "detail": str(exc),
                    "hint": "publish and migrate an independently reviewed MPDB artifact first",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if not str(args.review_ref or "").strip():
        print(
            json.dumps(
                {"error": "--review-ref is required to record the independent MPDB review"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    instance.setdefault("history", []).append(
        {
            "node": "onboard",
            "outcome": "verified_existing_mpdb_binding",
            "review_ref": args.review_ref,
            "notes": args.notes,
            "ts": time.time(),
        }
    )
    instance["current_node"] = "done"
    _save_instance(instance)
    print(
        json.dumps(
            {
                "applied": False,
                "database_mutated": False,
                "verified": True,
                "review_ref": args.review_ref,
                "binding": binding,
                "current_node": "done",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_set_args(args: argparse.Namespace) -> int:
    import shlex

    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(json.dumps({"error": "no instance"}, ensure_ascii=False), file=sys.stderr)
        return 2
    instance.setdefault("node_args", {})[args.node] = shlex.split(args.args)
    _save_instance(instance)
    print(
        json.dumps(
            {"node": args.node, "args": instance["node_args"][args.node]},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Supplier onboarding workflow state machine."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pm(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", required=True)
        p.add_argument("--model", required=True)

    p = sub.add_parser("start")
    add_pm(p)
    p.add_argument("--entry", default=None)
    p.add_argument("--expect-upstream", default=None)
    p.add_argument("--restart", action="store_true")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status")
    add_pm(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next")
    add_pm(p)
    p.add_argument(
        "--execute", action="store_true", help="run auto_test nodes immediately"
    )
    p.add_argument("--foreground", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("advance")
    add_pm(p)
    p.add_argument("--outcome", default=None)
    p.add_argument("--auto", action="store_true")
    p.add_argument("--notes", default=None)
    p.add_argument("--job-id", default=None)
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("set-args")
    add_pm(p)
    p.add_argument("--node", required=True, help="node id, e.g. concurrency_test")
    p.add_argument("--args", required=True, help="extra run_test.py args, shell-style")
    p.set_defaults(func=cmd_set_args)

    p = sub.add_parser("onboard-propose")
    add_pm(p)
    p.set_defaults(func=cmd_onboard_propose)

    p = sub.add_parser("onboard-apply")
    add_pm(p)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--review-ref", default=None)
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_onboard_apply)

    args = parser.parse_args()
    skill_env.ensure_skill_env()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
