from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

DATA_DIR = skill_env.ensure_skill_env()
sys.path.insert(0, str(skill_env.APP_ROOT))

import yaml  # noqa: E402

PROVIDERS_PATH = DATA_DIR / "providers.local.yaml"
PROFILES_PATH = DATA_DIR / "model_capability_profiles.local.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists() or not path.stat().st_size:
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _build_provider_overlay(
    provider: str, model: str, family: str, route: str, form: str
) -> dict:
    return {
        "providers": {
            provider: {
                "models": {
                    "candidates_add": [model],
                    "families": {model: family},
                    "routes": {model: {route: {"api_forms": {form: {}}}}},
                    "default_routes": {model: route},
                    "default_api_forms": {model: {route: form}},
                }
            }
        }
    }


def _build_profile_overlay(family: str, model: str, route: str, form: str) -> dict:
    return {
        "schema_version": 4,
        "modalities": {
            "text": {
                "families": {
                    family: {
                        "models": {model: {}},
                        "route_profiles": {
                            route: {
                                "api_forms": {form: {"model_profiles": {model: {}}}}
                            }
                        },
                    }
                }
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register an unverified model so param/load tests accept it "
        "(writes provider wiring + capability overlay into the data dir)."
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--family", required=True, help="existing capability family, e.g. deepseek"
    )
    parser.add_argument("--route-profile", default="dynamic_aggregator")
    parser.add_argument("--api-form", default="openai_chat_completions")
    parser.add_argument(
        "--yes", action="store_true", help="apply (requires explicit user approval)"
    )
    args = parser.parse_args()

    providers_doc = _load_yaml(PROVIDERS_PATH)
    providers = providers_doc.get("providers") or {}
    if args.provider not in providers:
        print(
            json.dumps(
                {
                    "error": f"provider {args.provider!r} not in {PROVIDERS_PATH}; "
                    "add base_url/api_key_env first (workflow acquire_key step)"
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    from lib.reference_specs import load_model_capability_profiles

    registry = load_model_capability_profiles()
    families = (registry.get("modalities") or {}).get("text", {}).get("families") or {}
    if args.family not in families:
        print(
            json.dumps(
                {
                    "error": f"capability family {args.family!r} not registered",
                    "available_families": sorted(families),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    routes = families[args.family].get("route_profiles") or {}
    if args.route_profile not in routes:
        print(
            json.dumps(
                {
                    "error": f"route profile {args.route_profile!r} not in family {args.family}",
                    "available_routes": sorted(routes),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    forms = routes[args.route_profile].get("api_forms") or {}
    if args.api_form not in forms:
        print(
            json.dumps(
                {
                    "error": f"api form {args.api_form!r} not in {args.family}/{args.route_profile}",
                    "available_forms": sorted(forms),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    provider_overlay = _build_provider_overlay(
        args.provider, args.model, args.family, args.route_profile, args.api_form
    )
    profile_overlay = _build_profile_overlay(
        args.family, args.model, args.route_profile, args.api_form
    )
    proposal = {
        str(PROVIDERS_PATH): provider_overlay,
        str(PROFILES_PATH): profile_overlay,
    }
    if not args.yes:
        print("PROPOSAL (re-run with --yes only after explicit user approval):\n")
        for path, overlay in proposal.items():
            print(f"--- merge into {path}")
            print(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False))
        return 0

    existing_provider = providers[args.provider] or {}
    models_cfg = existing_provider.get("models") or {}
    overlay_models = provider_overlay["providers"][args.provider]["models"]
    candidates = list(models_cfg.get("candidates") or [])
    if args.model not in candidates:
        candidates.append(args.model)
    merged_models = _deep_merge(
        models_cfg, {k: v for k, v in overlay_models.items() if k != "candidates_add"}
    )
    merged_models["candidates"] = candidates
    providers_doc.setdefault("providers", {})[args.provider]["models"] = merged_models

    profiles_doc = _load_yaml(PROFILES_PATH)
    if not profiles_doc:
        profiles_doc = {"schema_version": 4, "modalities": {"text": {"families": {}}}}
    profiles_doc = _deep_merge(profiles_doc, profile_overlay)

    for path in (PROVIDERS_PATH, PROFILES_PATH):
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    PROVIDERS_PATH.write_text(
        yaml.safe_dump(providers_doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    PROFILES_PATH.write_text(
        yaml.safe_dump(profiles_doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "applied": True,
                "provider": args.provider,
                "model": args.model,
                "family": args.family,
                "route_profile": args.route_profile,
                "api_form": args.api_form,
                "files": [str(PROVIDERS_PATH), str(PROFILES_PATH)],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
