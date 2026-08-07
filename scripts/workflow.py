from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

DATA_DIR = skill_env.ensure_skill_env()
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
    _instance_path(instance["provider"], instance["model"]).write_text(
        json.dumps(instance, ensure_ascii=False, indent=2), encoding="utf-8"
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
    return _read_json(_jobs_root() / job_id / "verdict.json") or {}


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
            "运行 workflow.py onboard-propose 生成注册提案，经用户明确批准后 "
            "onboard-apply --yes 写入数据目录注册表"
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
    return 0


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


def _build_profile_proposal(instance: dict) -> dict:
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
    family = verdict.get("model_family") or capability.get("model_family") or ""
    if not family:
        try:
            from lib.config import get_model_family, load_config

            family = get_model_family(load_config(), model, provider)
        except Exception:
            family = "unknown"
    api_form = capability.get("api_form") or "openai_chat_completions"
    route_profile = capability.get("route_profile") or "dynamic_aggregator"
    reference_source = verdict.get("reference_source")
    model_profile: dict = {"evidence": "onboarded_tests_passed"}
    if reference_source:
        model_profile["reference_sources"] = [reference_source]
        model_profile["default_reference_source"] = reference_source
    return {
        "schema_version": 4,
        "modalities": {
            "text": {
                "families": {
                    family: {
                        "models": {model: {}},
                        "route_profiles": {
                            route_profile: {
                                "api_forms": {
                                    api_form: {
                                        "model_profiles": {model: model_profile},
                                    }
                                },
                                "default_api_form": api_form,
                            }
                        },
                    }
                }
            }
        },
        "_meta": {
            "provider": provider,
            "model": model,
            "generated_at": time.time(),
            "based_on_jobs": instance.get("job_ids") or {},
        },
    }


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def cmd_onboard_propose(args: argparse.Namespace) -> int:
    instance = _load_instance(args.provider, args.model)
    if not instance:
        print(json.dumps({"error": "no instance"}, ensure_ascii=False), file=sys.stderr)
        return 2
    proposal = _build_profile_proposal(instance)
    print(yaml.safe_dump(proposal, allow_unicode=True, sort_keys=False))
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
    target = Path(
        __import__("os").getenv(
            "LLM_API_TEST_PROFILES_LOCAL",
            str(DATA_DIR / "model_capability_profiles.local.yaml"),
        )
    )
    existing = {}
    if target.exists() and target.stat().st_size:
        existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    proposal = _build_profile_proposal(instance)
    proposal.pop("_meta", None)
    if not existing:
        existing = {"schema_version": 4, "modalities": {"text": {"families": {}}}}
    merged = _deep_merge(existing, proposal)
    if target.exists():
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    instance.setdefault("history", []).append(
        {
            "node": "onboard",
            "outcome": "applied",
            "notes": args.notes,
            "ts": time.time(),
        }
    )
    instance["current_node"] = "done"
    _save_instance(instance)
    print(
        json.dumps(
            {"applied": True, "registry": str(target), "current_node": "done"},
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
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_onboard_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
