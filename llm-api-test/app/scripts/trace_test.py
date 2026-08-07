from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_DATA_DIR = Path(
    os.getenv("LLM_API_TEST_DATA_DIR") or (Path.home() / ".config" / "llm-api-test")
).expanduser()
os.environ.setdefault("LLM_API_TEST_DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("LLM_API_TEST_DOTENV", str(_DATA_DIR / ".env"))
os.environ.setdefault(
    "LLM_API_TEST_PROVIDERS_LOCAL", str(_DATA_DIR / "providers.local.yaml")
)
os.environ.setdefault(
    "LLM_API_TEST_PROFILES_LOCAL",
    str(_DATA_DIR / "model_capability_profiles.local.yaml"),
)
os.environ.setdefault("LLM_API_TEST_REPORTS_DIR", str(_DATA_DIR / "reports"))
os.environ.setdefault(
    "LLM_API_TEST_UPSTREAM_CORPUS", str(_DATA_DIR / "upstream_fingerprints.json")
)

from lib.config import (
    default_reports_root,
    ensure_dir,
    get_active_provider_name,
    get_selected_model,
    load_config,
)
from lib.metrics import write_json
from lib.upstream_fingerprint import (
    append_to_corpus,
    collect_fingerprint,
    compare_against_corpus,
    load_corpus,
)

PASS_SCORE_THRESHOLD = 0.6


def _corpus_path() -> Path:
    override = os.getenv("LLM_API_TEST_UPSTREAM_CORPUS")
    if override:
        return Path(override).expanduser()
    return _DATA_DIR / "upstream_fingerprints.json"


def _report_dir(raw: str | None, provider: str, model: str) -> Path:
    if raw:
        return ensure_dir(Path(raw))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in model)
    return ensure_dir(
        default_reports_root() / "trace" / f"{stamp}_{provider}_{safe}"
    )


def cmd_compare(args: argparse.Namespace) -> int:
    config = load_config()
    provider = args.provider or get_active_provider_name(config)
    model = args.model or get_selected_model(config, provider)
    corpus = load_corpus(_corpus_path())
    if not corpus:
        print(
            json.dumps(
                {
                    "error": "upstream corpus is empty",
                    "corpus_path": str(_corpus_path()),
                    "hint": "collect reference fingerprints first: trace_test.py collect "
                    "--provider <official-or-cloud-direct> --model <model> "
                    "--save-upstream <name>",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    fingerprint = collect_fingerprint(config, provider, model, timeout=args.timeout)
    results = compare_against_corpus(fingerprint, _corpus_path())
    best = results[0] if results else None
    expected = args.expect or None
    best_id = str((best or {}).get("candidate_id") or "")
    best_score = float((best or {}).get("score") or 0.0)
    if expected:
        passed = best_id == expected and best_score >= PASS_SCORE_THRESHOLD
    else:
        passed = bool(best) and best_score >= PASS_SCORE_THRESHOLD
    verdict = {
        "schema_version": 1,
        "type": "trace_test",
        "provider": provider,
        "model": model,
        "pass": passed,
        "best_match": best_id or None,
        "best_match_label": (best or {}).get("candidate_label"),
        "best_score": best_score,
        "expected": expected,
        "match_expected": (best_id == expected) if expected else None,
        "threshold": PASS_SCORE_THRESHOLD,
        "corpus_path": str(_corpus_path()),
        "corpus_entries": [
            str(entry.get("entry_id") or "") for entry in corpus
        ],
        "per_candidate_scores": [
            {
                "candidate_id": item.get("candidate_id"),
                "label": item.get("candidate_label"),
                "score": item.get("score"),
            }
            for item in results
        ],
        "evidence": (best or {}).get("details") or {},
        "created_at": time.time(),
    }
    report_dir = _report_dir(args.report_dir, provider, model)
    write_json(report_dir / "verdict.json", verdict)
    write_json(report_dir / "fingerprint.json", fingerprint)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if passed else 1


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config()
    provider = args.provider or get_active_provider_name(config)
    model = args.model or get_selected_model(config, provider)
    fingerprint = collect_fingerprint(config, provider, model, timeout=args.timeout)
    if args.save_upstream:
        corpus_path = append_to_corpus(
            args.save_upstream,
            args.label or args.save_upstream,
            fingerprint,
            notes=args.notes or "",
            path=_corpus_path(),
        )
        print(
            json.dumps(
                {
                    "saved": args.save_upstream,
                    "corpus_path": str(corpus_path),
                    "provider": provider,
                    "model": model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(fingerprint, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trace which upstream a provider's tokens come from via response fingerprinting."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("compare", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--provider", default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--timeout", type=int, default=120)
    sub.choices["compare"].add_argument("--expect", default=None)
    sub.choices["compare"].add_argument("--report-dir", default=None)
    sub.choices["collect"].add_argument("--save-upstream", default=None)
    sub.choices["collect"].add_argument("--label", default=None)
    sub.choices["collect"].add_argument("--notes", default=None)
    args = parser.parse_args()
    if args.command == "compare":
        return cmd_compare(args)
    return cmd_collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
