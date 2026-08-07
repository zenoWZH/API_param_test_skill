from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only before deps install
    raise RuntimeError("PyYAML is required. Install with: pip install -r requirements.txt") from exc

from .config import (
    PROJECT_ROOT,
    api_form_for_transport,
    deep_merge,
    get_model_api_form,
    get_model_reference_source,
    get_model_route_profile,
    get_model_transport,
    infer_model_family,
    transport_for_api_form,
)
from .param_outcome import VALID_EXPECTATIONS, normalize_expectation


REFERENCE_SPECS_PATH = PROJECT_ROOT / "api_reference_specs.yaml"
CAPABILITY_PROFILES_PATH = PROJECT_ROOT / "model_capability_profiles.yaml"
_profiles_local_override = os.getenv("LLM_API_TEST_PROFILES_LOCAL")
LOCAL_CAPABILITY_PROFILES_PATH = (
    Path(_profiles_local_override).expanduser()
    if _profiles_local_override
    else PROJECT_ROOT / "model_capability_profiles.local.yaml"
)
CAPABILITY_SCHEMA_VERSION = 4


def load_reference_specs(path: str | Path | None = None) -> dict[str, Any]:
    specs_path = Path(path) if path else REFERENCE_SPECS_PATH
    if not specs_path.exists():
        raise RuntimeError(f"Missing API reference specs file: {specs_path}")
    payload = copy.deepcopy(
        _read_yaml_cached(str(specs_path.resolve()), specs_path.stat().st_mtime_ns)
    )
    sources = payload.get("reference_sources")
    if not isinstance(sources, dict) or not sources:
        raise RuntimeError(f"{specs_path} must define reference_sources.")
    payload["reference_sources"] = _resolve_reference_source_inheritance(sources)
    return payload


def _resolve_reference_source_inheritance(
    sources: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Expand concise route wrappers while keeping route metadata exact."""
    resolved: dict[str, dict[str, Any]] = {}
    visiting: list[str] = []

    def resolve(source_id: str) -> dict[str, Any]:
        if source_id in resolved:
            return resolved[source_id]
        if source_id in visiting:
            chain = " -> ".join([*visiting, source_id])
            raise RuntimeError(f"Reference source inheritance cycle: {chain}.")
        raw = sources.get(source_id)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Reference source {source_id!r} must be an object.")
        visiting.append(source_id)
        current = copy.deepcopy(raw)
        parent_id = str(current.pop("extends", None) or "").strip()
        if parent_id:
            if parent_id not in sources:
                raise RuntimeError(
                    f"Reference source {source_id!r} extends unknown source "
                    f"{parent_id!r}."
                )
            merged = deep_merge(resolve(parent_id), current)
            merged["contract_reference_source"] = parent_id
        else:
            merged = current
        visiting.pop()
        resolved[source_id] = merged
        return merged

    for source_id in sources:
        resolve(str(source_id))
    return resolved


def get_reference_source(source_id: str | None = None) -> dict[str, Any]:
    specs = load_reference_specs()
    sources = specs["reference_sources"]
    if source_id:
        if source_id not in sources:
            raise KeyError(f"Reference source {source_id!r} not found in api_reference_specs.yaml")
        return _source_payload(source_id, sources[source_id])
    first_id = next(iter(sources))
    return _source_payload(first_id, sources[first_id])


def list_reference_sources() -> list[dict[str, Any]]:
    sources = load_reference_specs()["reference_sources"]
    result: list[dict[str, Any]] = []
    for source_id, raw in sources.items():
        param_rows = reference_param_rows(source_id)
        tested_param_count = sum(1 for row in param_rows if row["coverage_mode"] != "not_tested")
        result.append(
            {
                "id": source_id,
                "label": str(raw.get("label") or source_id),
                "official_sources": list(raw.get("official_sources") or []),
                "families": list(raw.get("families") or raw.get("default_for_families") or []),
                "default_for_families": list(raw.get("default_for_families") or []),
                "model_family": str(raw.get("model_family") or ""),
                "api_form": str(raw.get("api_form") or ""),
                "route_profile": str(raw.get("route_profile") or ""),
                "contract_reference_source": str(
                    raw.get("contract_reference_source") or ""
                ),
                "certification_scope": str(
                    raw.get("certification_scope") or "raw_route_contract"
                ),
                "route_stability_required": bool(
                    raw.get("route_stability_required", False)
                ),
                "executable": raw.get("executable", True) is not False,
                "test_profile_count": len(raw.get("test_profiles") or []),
                "param_count": len(raw.get("params") or {}),
                "tested_param_count": tested_param_count,
                "untested_param_count": len(param_rows) - tested_param_count,
            }
        )
    return result


def default_reference_source_for_family(
    family: str,
    route_profile: str | None = None,
    api_form: str | None = None,
) -> str:
    family_key = str(family).strip()
    route_key = str(route_profile or "").strip()
    form_key = str(api_form or "").strip()
    for source in list_reference_sources():
        if source.get("executable") is False:
            continue
        if family_key not in source.get("default_for_families", []):
            continue
        if route_key and source.get("route_profile") != route_key:
            continue
        if form_key and source.get("api_form") != form_key:
            continue
        return str(source["id"])
    candidates = [
        source
        for source in list_reference_sources()
        if source.get("executable") is not False
        and source.get("model_family") == family_key
        and (not route_key or source.get("route_profile") == route_key)
        and (not form_key or source.get("api_form") == form_key)
    ]
    if candidates:
        return str(candidates[0]["id"])
    raise RuntimeError(
        f"No reference source configured for family={family_key!r}, "
        f"route_profile={route_key or None!r}, api_form={form_key or None!r}."
    )


def default_reference_source_for_model(
    config: dict[str, Any],
    family: str,
    model: str | None = None,
    provider: str | None = None,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    selected_route = route_profile or get_model_route_profile(
        config, model, provider
    )
    selected_form = api_form or get_model_api_form(
        config, model, provider, route_profile=selected_route
    )
    override = get_model_reference_source(
        config,
        model,
        provider,
        route_profile=selected_route,
        api_form=selected_form,
    )
    if override:
        source = get_reference_source(override)
        _validate_reference_context(source, family, selected_route, selected_form)
        return override
    if model:
        try:
            capability = load_model_capability_profile(
                "text",
                family,
                model,
                api_form=selected_form,
                route_profile=selected_route,
            )
            source = capability.get("default_reference_source")
            if source:
                get_reference_source(str(source))
                return str(source)
        except (KeyError, ValueError):
            pass
    return default_reference_source_for_family(
        family, selected_route, selected_form
    )


def reference_sources_for_model(
    config: dict[str, Any],
    family: str,
    model: str,
    provider: str | None = None,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> list[str]:
    selected_route = route_profile or get_model_route_profile(
        config, model, provider
    )
    selected_form = api_form or get_model_api_form(
        config, model, provider, route_profile=selected_route
    )
    default_source = default_reference_source_for_model(
        config,
        family,
        model,
        provider,
        api_form=selected_form,
        route_profile=selected_route,
    )
    capability = load_model_capability_profile(
        "text",
        family,
        model,
        api_form=selected_form,
        route_profile=selected_route,
    )
    declared = [str(item) for item in capability.get("allowed_reference_sources") or []]
    if default_source not in declared:
        raise ValueError(
            f"Reference source {default_source!r} selected for {family}/{model} "
            "is not declared by its family/model capability suite."
        )
    result = [default_source, *declared]
    unique = list(dict.fromkeys(result))
    for source_id in unique:
        source = get_reference_source(source_id)
        _validate_reference_context(source, family, selected_route, selected_form)
    return unique


def comparison_reference_source_for_model(
    modality: str,
    family: str,
    model: str,
    *,
    path: str | Path | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    """Return the provider-independent source used for fair model comparisons."""
    capability = load_model_capability_profile(
        modality,
        family,
        model,
        path=path,
        api_form=api_form,
        route_profile=route_profile,
    )
    if capability.get("known_model") is not True:
        raise ValueError(
            f"Missing registered {modality} model capability profile for "
            f"{family}/{model}."
        )
    source_id = str(capability.get("comparison_reference_source") or "").strip()
    if not source_id:
        raise ValueError(
            f"Missing comparison_reference_source for {modality}/{family}/{model}."
        )
    declared = set(capability.get("allowed_reference_sources") or [])
    if source_id not in declared:
        raise ValueError(
            f"Comparison reference source {source_id!r} is not declared by "
            f"{modality}/{family}/{model}; declared={sorted(declared)}."
        )
    get_reference_source(source_id)
    return source_id


def reference_param_rows(source_id: str) -> list[dict[str, Any]]:
    source = get_reference_source(source_id)
    source_profiles = list(source.get("test_profiles") or [])
    rows: list[dict[str, Any]] = []
    for name, raw in (source.get("params") or {}).items():
        cfg = raw if isinstance(raw, dict) else {}
        coverage = str(cfg.get("coverage") or "")
        matched_profiles = [
            profile for profile in source_profiles if _coverage_mentions_profile(coverage, profile)
        ]
        coverage_mode = "profiles"
        if "all profiles" in coverage.casefold():
            matched_profiles = source_profiles
            coverage_mode = "all_profiles"
        elif "provider/model selection" in coverage.casefold():
            coverage_mode = "selection"
        elif not matched_profiles:
            coverage_mode = "not_tested"
        rows.append(
            {
                "parameter": str(name),
                "official": (
                    "required"
                    if cfg.get("required")
                    else "unsupported"
                    if cfg.get("supported") is False
                    else "supported"
                ),
                "local": "reference",
                "coverage": coverage,
                "coverage_mode": coverage_mode,
                "test_profiles": matched_profiles,
            }
        )
    return rows


def reference_spec_payload(source_id: str) -> dict[str, Any]:
    source = get_reference_source(source_id)
    params = reference_param_rows(source["id"])
    return {
        "reference_source": source["id"],
        "label": source["label"],
        "official_sources": source["official_sources"],
        "model_family": source.get("model_family"),
        "api_form": source.get("api_form"),
        "route_profile": source.get("route_profile"),
        "contract_reference_source": source.get("contract_reference_source"),
        "certification_scope": source.get("certification_scope"),
        "route_stability_required": source.get("route_stability_required"),
        "evidence": source.get("evidence"),
        "test_profiles": source["test_profiles"],
        "params": params,
        "comparison": params,
        "param_count": len(params),
        "tested_params": [row["parameter"] for row in params if row["coverage_mode"] != "not_tested"],
        "untested_params": [row["parameter"] for row in params if row["coverage_mode"] == "not_tested"],
    }


def test_profiles_for_reference(source_id: str) -> list[str]:
    return list(get_reference_source(source_id).get("test_profiles") or [])


def tested_params_for_reference(source_id: str) -> list[str]:
    return [
        str(row["parameter"])
        for row in reference_param_rows(source_id)
        if row["coverage_mode"] != "not_tested"
    ]


def untested_params_for_reference(source_id: str) -> list[str]:
    return [
        str(row["parameter"])
        for row in reference_param_rows(source_id)
        if row["coverage_mode"] == "not_tested"
    ]


def family_for_reference(source_id: str) -> str:
    source = get_reference_source(source_id)
    family = str(source.get("model_family") or "").strip()
    if family:
        return family
    families = source.get("families") or source.get("default_for_families") or []
    legacy = str(families[0]) if families else ""
    return "gpt" if legacy == "openai" else legacy


def parameter_label_for_profile(source_id: str, profile: str) -> str:
    matches = parameters_for_profile(source_id, profile)
    return ", ".join(matches) if matches else profile


def parameters_for_profile(source_id: str, profile: str) -> list[str]:
    matches: list[str] = []
    for row in reference_param_rows(source_id):
        if profile in (row.get("test_profiles") or []):
            matches.append(str(row["parameter"]))
    return matches


def _source_payload(source_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    params = raw.get("params") or {}
    profiles = raw.get("test_profiles") or []
    if not isinstance(params, dict) or not params:
        raise RuntimeError(f"Reference source {source_id!r} must define params.")
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError(f"Reference source {source_id!r} must define test_profiles.")
    return {
        "id": source_id,
        "label": str(raw.get("label") or source_id),
        "official_sources": list(raw.get("official_sources") or []),
        "families": list(raw.get("families") or raw.get("default_for_families") or []),
        "default_for_families": list(raw.get("default_for_families") or []),
        "model_family": str(
            raw.get("model_family")
            or next(iter(raw.get("families") or raw.get("default_for_families") or []), "")
        ),
        "api_form": str(raw.get("api_form") or ""),
        "route_profile": str(raw.get("route_profile") or ""),
        "contract_reference_source": str(
            raw.get("contract_reference_source") or ""
        ),
        "certification_scope": str(
            raw.get("certification_scope") or "raw_route_contract"
        ),
        "route_stability_required": bool(
            raw.get("route_stability_required", False)
        ),
        "evidence": str(raw.get("evidence") or "official_contract"),
        "executable": raw.get("executable", True) is not False,
        "params": params,
        "test_profiles": [str(profile) for profile in profiles],
    }


def _validate_reference_context(
    source: dict[str, Any],
    family: str,
    route_profile: str,
    api_form: str,
) -> None:
    source_family = str(source.get("model_family") or "")
    source_route = str(source.get("route_profile") or "")
    source_form = str(source.get("api_form") or "")
    if source_family and source_family != family:
        raise ValueError(
            f"Reference source {source['id']!r} belongs to family {source_family!r}, "
            f"not {family!r}."
        )
    if source_form and source_form != api_form:
        raise ValueError(
            f"Reference source {source['id']!r} belongs to API form {source_form!r}, "
            f"not {api_form!r}."
        )
    if source_route and source_route != route_profile:
        raise ValueError(
            f"Reference source {source['id']!r} belongs to route profile "
            f"{source_route!r}, not {route_profile!r}."
        )


def _coverage_mentions_profile(coverage: str, profile: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(profile)}(?![A-Za-z0-9_.-])", coverage))


def _merge_v3_migration_override(
    base: dict[str, Any],
    override: dict[str, Any],
    *,
    base_path: str,
    override_path: str,
) -> dict[str, Any]:
    conflicts = _v3_migration_conflicts(base, override)
    if conflicts:
        details = "; ".join(
            f"{base_path}/{field}={base_value!r} conflicts with "
            f"{override_path}/{field}={override_value!r}"
            for field, base_value, override_value in conflicts
        )
        raise RuntimeError(f"schema v3 migration conflict: {details}.")
    return deep_merge(base, override)


def _v3_migration_conflicts(
    base: dict[str, Any],
    override: dict[str, Any],
    *,
    prefix: str = "",
) -> list[tuple[str, Any, Any]]:
    """Return every overlapping v3 leaf whose values disagree."""
    conflicts: list[tuple[str, Any, Any]] = []
    for key in sorted(set(base).intersection(override), key=str):
        field = f"{prefix}/{key}" if prefix else str(key)
        base_value = base[key]
        override_value = override[key]
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            conflicts.extend(
                _v3_migration_conflicts(
                    base_value,
                    override_value,
                    prefix=field,
                )
            )
        elif base_value != override_value:
            conflicts.append((field, base_value, override_value))
    return conflicts


def _migrate_v3_capability_payload(
    payload: dict[str, Any],
    *,
    source_label: str,
) -> dict[str, Any]:
    """Transpose schema v3 form-first capabilities to route-first schema v4."""
    migrated = copy.deepcopy(payload)
    modalities = migrated.get("modalities") or {}
    if not isinstance(modalities, dict):
        raise RuntimeError(f"{source_label} must define modalities as an object.")
    for modality, modality_cfg in modalities.items():
        if not isinstance(modality_cfg, dict):
            continue
        families = modality_cfg.get("families") or {}
        if not isinstance(families, dict):
            continue
        for family, raw_family_cfg in list(families.items()):
            if not isinstance(raw_family_cfg, dict):
                continue
            family_cfg = deep_merge({}, raw_family_cfg)
            api_forms = family_cfg.pop("api_forms", None)
            default_form = str(family_cfg.pop("default_api_form", None) or "").strip()
            if not isinstance(api_forms, dict) or not api_forms:
                raise RuntimeError(
                    f"{source_label} schema v3 family {modality}/{family} must define api_forms."
                )
            routes: dict[str, dict[str, Any]] = {}
            for api_form, raw_form_cfg in api_forms.items():
                if not isinstance(raw_form_cfg, dict):
                    raise RuntimeError(
                        f"{source_label} schema v3 path {modality}/{family}/"
                        f"api_forms/{api_form} must be an object."
                    )
                form_cfg = deep_merge({}, raw_form_cfg)
                form_routes = form_cfg.pop("route_profiles", None) or {}
                form_default_route = str(
                    form_cfg.pop("default_route_profile", None) or ""
                ).strip()
                model_profiles = form_cfg.pop("model_profiles", None) or {}
                if not isinstance(form_routes, dict):
                    raise RuntimeError(
                        f"{source_label} schema v3 path {modality}/{family}/"
                        f"api_forms/{api_form}/route_profiles must be an object."
                    )
                if not isinstance(model_profiles, dict):
                    raise RuntimeError(
                        f"{source_label} schema v3 path {modality}/{family}/"
                        f"api_forms/{api_form}/model_profiles must be an object."
                    )
                route_names = set(str(item) for item in form_routes)
                if form_default_route:
                    route_names.add(form_default_route)
                for model_cfg in model_profiles.values():
                    if isinstance(model_cfg, dict):
                        route_names.update(
                            str(item)
                            for item in (model_cfg.get("route_profiles") or {})
                        )
                if not route_names:
                    raise RuntimeError(
                        f"{source_label} schema v3 path {modality}/{family}/"
                        f"api_forms/{api_form} cannot be migrated without a route profile."
                    )
                for route in sorted(route_names):
                    route_override = form_routes.get(route) or {}
                    if not isinstance(route_override, dict):
                        raise RuntimeError(
                            f"{source_label} schema v3 route override {modality}/"
                            f"{family}/{api_form}/{route} must be an object."
                        )
                    form_path = (
                        f"{source_label}:{modality}/{family}/api_forms/{api_form}"
                    )
                    route_path = f"{form_path}/route_profiles/{route}"
                    migrated_form = _merge_v3_migration_override(
                        form_cfg,
                        route_override,
                        base_path=form_path,
                        override_path=route_path,
                    )
                    migrated_models: dict[str, Any] = {}
                    for model, raw_model_cfg in model_profiles.items():
                        if not isinstance(raw_model_cfg, dict):
                            raise RuntimeError(
                                f"{source_label} schema v3 model profile {modality}/"
                                f"{family}/{api_form}/{model} must be an object."
                            )
                        model_cfg = deep_merge({}, raw_model_cfg)
                        model_routes = model_cfg.pop("route_profiles", None) or {}
                        model_cfg.pop("default_route_profile", None)
                        model_override = model_routes.get(route) or {}
                        if not isinstance(model_override, dict):
                            raise RuntimeError(
                                f"{source_label} schema v3 model route override "
                                f"{modality}/{family}/{api_form}/{model}/{route} "
                                "must be an object."
                            )
                        migrated_models[str(model)] = _merge_v3_migration_override(
                            model_cfg,
                            model_override,
                            base_path=f"{form_path}/model_profiles/{model}",
                            override_path=(
                                f"{form_path}/model_profiles/{model}/"
                                f"route_profiles/{route}"
                            ),
                        )
                    migrated_form["model_profiles"] = migrated_models
                    route_cfg = routes.setdefault(
                        route, {"api_forms": {}}
                    )
                    target_forms = route_cfg["api_forms"]
                    if api_form in target_forms and target_forms[api_form] != migrated_form:
                        raise RuntimeError(
                            f"{source_label} schema v3 migration conflict at "
                            f"{modality}/{family}/route_profiles/{route}/"
                            f"api_forms/{api_form}."
                        )
                    target_forms[str(api_form)] = migrated_form
            for route, route_cfg in routes.items():
                route_forms = route_cfg["api_forms"]
                if default_form and default_form in route_forms:
                    route_cfg["default_api_form"] = default_form
                elif len(route_forms) == 1:
                    route_cfg["default_api_form"] = next(iter(route_forms))
                else:
                    raise RuntimeError(
                        f"{source_label} schema v3 family {modality}/{family} route "
                        f"{route!r} exposes multiple API forms but has no unambiguous default."
                    )
            family_cfg["route_profiles"] = routes
            families[family] = family_cfg
    migrated["schema_version"] = CAPABILITY_SCHEMA_VERSION
    return migrated


def _normalize_capability_payload(
    payload: dict[str, Any],
    *,
    source_label: str,
) -> dict[str, Any]:
    schema_version = int(payload.get("schema_version") or 1)
    if schema_version == 3:
        return _migrate_v3_capability_payload(payload, source_label=source_label)
    if schema_version not in {1, 2, CAPABILITY_SCHEMA_VERSION}:
        raise RuntimeError(
            f"{source_label} has unsupported schema_version={schema_version}."
        )
    return copy.deepcopy(payload)


def load_model_capability_profiles(path: str | Path | None = None) -> dict[str, Any]:
    specs_path = Path(path) if path else CAPABILITY_PROFILES_PATH
    if not specs_path.exists():
        raise RuntimeError(f"Missing model capability profiles file: {specs_path}")
    payload = _normalize_capability_payload(
        _read_yaml_cached(str(specs_path.resolve()), specs_path.stat().st_mtime_ns),
        source_label=str(specs_path),
    )
    if path is None and LOCAL_CAPABILITY_PROFILES_PATH.exists():
        local_payload = _read_yaml_cached(
            str(LOCAL_CAPABILITY_PROFILES_PATH.resolve()),
            LOCAL_CAPABILITY_PROFILES_PATH.stat().st_mtime_ns,
        )
        if "schema_version" in local_payload:
            local_payload = _normalize_capability_payload(
                local_payload,
                source_label=str(LOCAL_CAPABILITY_PROFILES_PATH),
            )
        payload = deep_merge(payload, local_payload)
    schema_version = int(payload.get("schema_version") or 1)
    modalities = payload.get("modalities")
    if not isinstance(modalities, dict) or not modalities:
        raise RuntimeError(f"{specs_path} must define modalities.")
    return payload


def load_model_capability_profile(
    modality: str,
    family: str,
    model: str,
    *,
    path: str | Path | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
    reference_source: str | None = None,
    provider_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load one explicit family/API-form/model/route capability profile."""
    payload = load_model_capability_profiles(path)
    modalities = payload["modalities"]
    modality_key = str(modality).strip().casefold()
    family_key = str(family).strip()
    model_key = str(model).strip()

    modality_cfg = modalities.get(modality_key)
    if not isinstance(modality_cfg, dict):
        raise KeyError(f"Unknown capability modality {modality!r}")
    families = modality_cfg.get("families") or {}
    if family_key == "openai":
        inferred = infer_model_family(model_key)
        if inferred in families:
            family_key = inferred
    if not isinstance(families, dict) or family_key not in families:
        raise KeyError(f"Unknown capability family {family!r} for modality {modality_key!r}")
    family_cfg = families[family_key] if isinstance(families[family_key], dict) else {}
    schema_version = int(payload.get("schema_version") or 1)
    models = family_cfg.get("canonical_models") or family_cfg.get("models") or {}
    if not isinstance(models, dict):
        models = {}
    profile_id, canonical_cfg = _match_model_profile(models, model_key)
    if canonical_cfg is None:
        canonical_cfg = {}
    if not isinstance(canonical_cfg, dict):
        raise RuntimeError(
            f"Capability model entry for {modality_key}/{family_key}/{model_key} must be a mapping."
        )
    route_cfg: dict[str, Any] = {}
    form_cfg: dict[str, Any] = {}
    form_model_cfg: dict[str, Any] = {}
    known_api_form = False
    known_api_profile = False
    route_known = False
    selected_form = str(api_form or "").strip()
    selected_route = str(route_profile or "").strip()
    if schema_version >= 4:
        routes = family_cfg.get("route_profiles") or {}
        if not isinstance(routes, dict) or not routes:
            raise RuntimeError(
                f"Capability family {modality_key}/{family_key} must define route_profiles."
            )
        selected_route = _select_capability_route(
            routes,
            selected_route or None,
            reference_source,
        )
        raw_route_cfg = routes.get(selected_route)
        route_known = isinstance(raw_route_cfg, dict)
        route_cfg = raw_route_cfg if isinstance(raw_route_cfg, dict) else {}
        api_forms = route_cfg.get("api_forms") or {}
        if route_known and (not isinstance(api_forms, dict) or not api_forms):
            raise RuntimeError(
                f"Capability route {modality_key}/{family_key}/{selected_route} "
                "must define api_forms."
            )
        if route_known:
            selected_form = _select_capability_api_form(
                route_cfg,
                api_forms,
                selected_form or None,
                reference_source,
            )
        elif not selected_form and reference_source:
            selected_form = str(
                get_reference_source(reference_source).get("api_form") or ""
            )
        raw_form_cfg = api_forms.get(selected_form) if isinstance(api_forms, dict) else None
        known_api_form = isinstance(raw_form_cfg, dict)
        form_cfg = raw_form_cfg if isinstance(raw_form_cfg, dict) else {}
        form_models = form_cfg.get("model_profiles") or {}
        if known_api_form and not isinstance(form_models, dict):
            raise RuntimeError(
                f"{modality_key}/{family_key}/{selected_route}/{selected_form}."
                "model_profiles must be a mapping."
            )
        raw_form_model = (
            form_models.get(str(profile_id))
            if isinstance(form_models, dict) and profile_id
            else None
        )
        form_model_cfg = raw_form_model if isinstance(raw_form_model, dict) else {}
        known_api_profile = (
            profile_id is not None
            and isinstance(form_models, dict)
            and str(profile_id) in form_models
        )
    else:
        legacy_sources = _normalize_reference_sources(
            family_cfg.get("reference_sources") or {}
        )
        selected_form = selected_form or _legacy_api_form(
            legacy_sources,
            reference_source,
            modality_key,
        )
        form_cfg = {
            "transport": transport_for_api_form(selected_form)
            if modality_key == "text"
            else None,
            "reference_sources": list(legacy_sources.values()),
            "default_reference_source": legacy_sources.get(
                transport_for_api_form(selected_form)
            ) if modality_key == "text" else None,
        }
        selected_route = selected_route or "legacy"
        route_known = True
        known_api_form = True
        known_api_profile = profile_id is not None

    provider_layer = deep_merge({}, provider_override or {})
    ordered_layers = [
        family_cfg,
        canonical_cfg,
        route_cfg,
        form_cfg,
        form_model_cfg,
        provider_layer,
    ]
    default_layers = [family_cfg, route_cfg, form_cfg, provider_layer]
    model_layers = [canonical_cfg, form_model_cfg]
    default_expectations = _merge_expectation_layers(
        default_layers, "default_expectations"
    )
    model_expectations = _merge_expectation_layers(model_layers, "expectations")
    merged: dict[str, str] = {}
    for layer in ordered_layers:
        merged.update(
            _normalize_expectation_map(
                layer.get("default_expectations") or {},
                label="expectations",
            )
        )
        merged.update(
            _normalize_expectation_map(
                layer.get("expectations") or {},
                label="expectations",
            )
        )
    default_parameter_expectations = _merge_expectation_layers(
        default_layers,
        "default_parameter_expectations",
        label="parameter_expectations",
    )
    model_parameter_expectations = _merge_expectation_layers(
        model_layers,
        "parameter_expectations",
        label="parameter_expectations",
    )
    merged_parameter_expectations: dict[str, str] = {}
    for layer in ordered_layers:
        merged_parameter_expectations.update(
            _normalize_expectation_map(
                layer.get("default_parameter_expectations") or {},
                label="parameter_expectations",
            )
        )
        merged_parameter_expectations.update(
            _normalize_expectation_map(
                layer.get("parameter_expectations") or {},
                label="parameter_expectations",
            )
        )
    allowed_sources = _resolve_allowed_reference_sources(
        family_cfg,
        form_cfg,
        canonical_cfg,
        form_model_cfg,
        route_cfg,
        schema_version=schema_version,
    )
    invalid_route_or_form = schema_version >= 4 and (
        not route_known or not known_api_form
    )
    default_reference_source = (
        ""
        if invalid_route_or_form
        else str(
            reference_source
            or provider_layer.get("reference_source")
            or form_model_cfg.get("default_reference_source")
            or form_cfg.get("default_reference_source")
            or route_cfg.get("default_reference_source")
            or canonical_cfg.get("comparison_reference_source")
            or family_cfg.get("comparison_reference_source")
            or (allowed_sources[0] if allowed_sources else "")
        ).strip()
    )
    if default_reference_source and default_reference_source not in allowed_sources:
        raise ValueError(
            f"Reference source {default_reference_source!r} is not allowed for "
            f"{family_key}/{selected_form}/{profile_id or model_key}/{selected_route}."
        )
    comparison_reference_source = (
        ""
        if invalid_route_or_form
        else str(
            form_model_cfg.get("comparison_reference_source")
            or form_cfg.get("comparison_reference_source")
            or route_cfg.get("comparison_reference_source")
            or canonical_cfg.get("comparison_reference_source")
            or family_cfg.get("comparison_reference_source")
            or default_reference_source
        ).strip()
    )
    if comparison_reference_source and comparison_reference_source not in allowed_sources:
        comparison_reference_source = default_reference_source

    pressure_profiles: dict[str, list[str]] = {}
    pressure_omit: list[str] = []
    pressure_parameter_aliases: dict[str, str] = {}
    pressure_overrides: dict[str, Any] = {}
    pressure_transport_overrides: dict[str, dict[str, Any]] = {}
    for layer in ordered_layers:
        pressure_profiles.update(
            _normalize_pressure_profiles(layer.get("pressure_profiles") or {})
        )
        pressure_omit.extend(
            _normalize_name_list(
                layer.get("pressure_omit_params") or [],
                label="pressure_omit_params",
            )
        )
        pressure_parameter_aliases.update(
            _normalize_parameter_aliases(
                layer.get("pressure_parameter_aliases") or {}
            )
        )
        pressure_overrides = deep_merge(
            pressure_overrides,
            _normalize_pressure_overrides(layer.get("pressure_overrides") or {}),
        )
        pressure_transport_overrides = deep_merge(
            pressure_transport_overrides,
            _normalize_pressure_transport_overrides(
                layer.get("pressure_transport_overrides") or {}
            ),
        )

    transport = str(
        provider_layer.get("transport") or form_cfg.get("transport") or ""
    ).strip()
    if not transport and modality_key == "text" and selected_form:
        transport = transport_for_api_form(selected_form)
    effective = {}
    for layer in ordered_layers:
        effective = deep_merge(effective, layer)
    legacy_reference_sources = {transport: default_reference_source} if transport and default_reference_source else {}

    return {
        "modality": modality_key,
        "family": family_key,
        "model": model_key,
        "profile_id": profile_id,
        "model_api_profile_id": (
            f"{family_key}/{profile_id}@{selected_route}/{selected_form}"
            if profile_id and selected_route and selected_form
            else None
        ),
        "api_form": selected_form,
        "transport": transport,
        "route_profile": selected_route,
        "route_profile_known": route_known,
        "reference_source": default_reference_source or None,
        "default_reference_source": default_reference_source or None,
        "comparison_reference_source": comparison_reference_source or None,
        "reference_sources": legacy_reference_sources,
        "allowed_reference_sources": allowed_sources,
        "alternate_sources": [
            source for source in allowed_sources if source != default_reference_source
        ],
        "suite": effective.get("suite"),
        "default_expectation": normalize_expectation(
            effective.get("default_expectation"),
            default="supported",
        ),
        "default_expectations": default_expectations,
        "model_expectations": model_expectations,
        "expectations": merged,
        "default_parameter_expectations": default_parameter_expectations,
        "model_parameter_expectations": model_parameter_expectations,
        "parameter_expectations": merged_parameter_expectations,
        "pressure_profiles": pressure_profiles,
        "pressure_omit_params": list(dict.fromkeys(pressure_omit)),
        "pressure_parameter_aliases": pressure_parameter_aliases,
        "pressure_overrides": pressure_overrides,
        "pressure_transport_overrides": pressure_transport_overrides,
        "parameter_test_enabled": bool(
            effective.get("parameter_test_enabled", True)
        ),
        "pressure_test_enabled": bool(
            effective.get("pressure_test_enabled", True)
        ),
        "disabled_reason": effective.get("disabled_reason"),
        "known_model": profile_id is not None,
        "known_api_form": known_api_form,
        "known_api_profile": known_api_profile,
        "profile_status": (
            "registered"
            if profile_id is not None and known_api_profile and route_known
            else "unregistered_route"
            if profile_id is not None and not route_known
            else "unregistered_api_form_for_route"
            if profile_id is not None and not known_api_form
            else "unregistered_model_profile"
            if profile_id is not None and not known_api_profile
            else "unregistered_model"
        ),
        "evidence": str(
            effective.get("evidence")
            or "family_contract"
        ),
        "certification_scope": str(
            effective.get("certification_scope") or "raw_route_contract"
        ),
        "route_stability_required": bool(
            effective.get("route_stability_required", False)
        ),
        "identity": deep_merge({}, effective.get("identity") or {}),
        "response_validators": list(effective.get("response_validators") or []),
        "usage_schema": deep_merge({}, effective.get("usage_schema") or {}),
        "cache_policy": deep_merge({}, effective.get("cache_policy") or {}),
    }


def _select_capability_route(
    route_profiles: dict[str, Any],
    requested: str | None,
    reference_source: str | None,
) -> str:
    if requested:
        return requested
    if reference_source:
        source_route = str(
            get_reference_source(reference_source).get("route_profile") or ""
        )
        if source_route:
            return source_route
    if len(route_profiles) == 1:
        return next(iter(route_profiles))
    raise ValueError(
        f"Family exposes multiple route profiles {sorted(route_profiles)}; "
        "route_profile is required."
    )


def _select_capability_api_form(
    family_cfg: dict[str, Any],
    api_forms: dict[str, Any],
    requested: str | None,
    reference_source: str | None,
) -> str:
    if requested:
        return requested
    if reference_source:
        source_form = str(get_reference_source(reference_source).get("api_form") or "")
        if source_form in api_forms:
            return source_form
    default_form = str(family_cfg.get("default_api_form") or "").strip()
    if default_form:
        if default_form not in api_forms:
            raise RuntimeError(
                f"default_api_form {default_form!r} is not declared in api_forms."
            )
        return default_form
    if len(api_forms) == 1:
        return next(iter(api_forms))
    raise ValueError(
        f"Family exposes multiple API forms {sorted(api_forms)}; api_form is required."
    )


def _legacy_api_form(
    reference_sources: dict[str, str],
    reference_source: str | None,
    modality: str,
) -> str:
    if reference_source:
        source_form = str(get_reference_source(reference_source).get("api_form") or "")
        if source_form:
            return source_form
    if reference_sources:
        return api_form_for_transport(next(iter(reference_sources)))
    if modality == "image":
        return "openai_images_generations"
    return "openai_chat_completions"


def _reference_source_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        values = list(raw.values())
    else:
        raise RuntimeError("reference_sources must be a list or mapping")
    result = [str(item).strip() for item in values if str(item).strip()]
    return list(dict.fromkeys(result))


def _resolve_allowed_reference_sources(
    family_cfg: dict[str, Any],
    form_cfg: dict[str, Any],
    canonical_cfg: dict[str, Any],
    form_model_cfg: dict[str, Any],
    route_cfg: dict[str, Any],
    *,
    schema_version: int,
) -> list[str]:
    if schema_version < 4:
        result = _reference_source_ids(family_cfg.get("reference_sources") or {})
        result.extend(
            item
            for item in _reference_source_ids(canonical_cfg.get("reference_sources") or {})
            if item not in result
        )
        for layer in (family_cfg, canonical_cfg):
            for item in layer.get("alternate_sources") or []:
                source_id = str(item).strip()
                if source_id and source_id not in result:
                    result.append(source_id)
        return result

    result = _reference_source_ids(route_cfg.get("reference_sources") or [])
    for layer in (form_cfg, form_model_cfg):
        if "reference_sources" not in layer:
            continue
        restricted = _reference_source_ids(layer.get("reference_sources"))
        if result:
            allowed = set(restricted)
            result = [item for item in result if item in allowed]
        else:
            result = restricted
    return result


def resolve_profile_expectation(
    modality: str,
    family: str,
    model: str,
    profile: str,
    *,
    path: str | Path | None = None,
    capability_profile: dict[str, Any] | None = None,
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    """Resolve supported/unsupported for one probe within a family suite."""
    profile_key = str(profile).strip()
    if not profile_key:
        raise ValueError("profile is required")
    cap = capability_profile or load_model_capability_profile(
        modality,
        family,
        model,
        path=path,
        api_form=api_form,
        route_profile=route_profile,
        reference_source=reference_source,
    )
    expectations = cap.get("expectations")
    if not isinstance(expectations, dict):
        # Snapshots may expose resolved_expectations instead of the merged map.
        expectations = cap.get("resolved_expectations") or {}
    if not isinstance(expectations, dict):
        expectations = {}
    if profile_key in expectations:
        return normalize_expectation(expectations[profile_key])
    # Rebuild from default + model overrides when a snapshot omits the probe.
    merged = dict(cap.get("default_expectations") or {})
    merged.update(dict(cap.get("model_expectations") or {}))
    if profile_key in merged:
        return normalize_expectation(merged[profile_key])
    if reference_source:
        parameter_expectations = [
            resolve_parameter_expectation(
                modality,
                family,
                model,
                parameter,
                capability_profile=cap,
            )
            for parameter in parameters_for_profile(reference_source, profile_key)
        ]
        if "unsupported" in parameter_expectations:
            return "unsupported"
    return normalize_expectation(
        cap.get("default_expectation"),
        default="supported",
    )


def resolve_parameter_expectation(
    modality: str,
    family: str,
    model: str,
    parameter: str,
    *,
    path: str | Path | None = None,
    capability_profile: dict[str, Any] | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    parameter_key = str(parameter).strip()
    if not parameter_key:
        raise ValueError("parameter is required")
    cap = capability_profile or load_model_capability_profile(
        modality,
        family,
        model,
        path=path,
        api_form=api_form,
        route_profile=route_profile,
    )
    expectations = cap.get("parameter_expectations")
    if not isinstance(expectations, dict):
        expectations = {}
    if parameter_key in expectations:
        return normalize_expectation(expectations[parameter_key])
    merged = dict(cap.get("default_parameter_expectations") or {})
    merged.update(dict(cap.get("model_parameter_expectations") or {}))
    if parameter_key in merged:
        return normalize_expectation(merged[parameter_key])
    return normalize_expectation(
        cap.get("default_expectation"),
        default="supported",
    )


def resolve_suite_expectations(
    modality: str,
    family: str,
    model: str,
    profiles: list[str],
    *,
    path: str | Path | None = None,
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> dict[str, str]:
    cap = load_model_capability_profile(
        modality,
        family,
        model,
        path=path,
        api_form=api_form,
        route_profile=route_profile,
        reference_source=reference_source,
    )
    return {
        str(profile): resolve_profile_expectation(
            modality,
            family,
            model,
            str(profile),
            capability_profile=cap,
            reference_source=reference_source,
        )
        for profile in profiles
    }


def capability_profile_snapshot(
    modality: str,
    family: str,
    model: str,
    profiles: list[str] | None = None,
    *,
    path: str | Path | None = None,
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
    provider_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cap = load_model_capability_profile(
        modality,
        family,
        model,
        path=path,
        api_form=api_form,
        route_profile=route_profile,
        reference_source=reference_source,
        provider_override=provider_override,
    )
    profile_list = [str(item) for item in (profiles or [])]
    resolved = {
        profile: resolve_profile_expectation(
            modality,
            family,
            model,
            profile,
            capability_profile=cap,
            reference_source=reference_source,
        )
        for profile in profile_list
    }
    resolved_parameters: dict[str, str] = {}
    if reference_source:
        resolved_parameters = {
            str(row["parameter"]): _resolved_parameter_expectation_for_row(
                cap,
                modality,
                family,
                model,
                row,
                resolved,
            )
            for row in reference_param_rows(reference_source)
        }
    for parameter, expectation in (
        cap.get("parameter_expectations") or {}
    ).items():
        resolved_parameters.setdefault(
            str(parameter),
            normalize_expectation(expectation),
        )
    # Always expose the full merged exception map so runtime resolution works
    # even for profiles not present in the current suite snapshot.
    expectations = dict(cap.get("expectations") or {})
    expectations.update(resolved)
    return {
        "modality": cap["modality"],
        "family": cap["family"],
        "model": cap["model"],
        "profile_id": cap.get("profile_id"),
        "model_api_profile_id": cap.get("model_api_profile_id"),
        "profile_status": cap.get("profile_status"),
        "api_form": cap.get("api_form"),
        "transport": cap.get("transport"),
        "route_profile": cap.get("route_profile"),
        "route_profile_known": cap.get("route_profile_known"),
        "reference_source": cap.get("reference_source"),
        "comparison_reference_source": cap.get("comparison_reference_source"),
        "selected_reference_source": reference_source,
        "reference_sources": dict(cap.get("reference_sources") or {}),
        "allowed_reference_sources": list(
            cap.get("allowed_reference_sources") or []
        ),
        "alternate_sources": list(cap.get("alternate_sources") or []),
        "suite": cap.get("suite"),
        "known_model": cap.get("known_model"),
        "known_api_profile": cap.get("known_api_profile"),
        "evidence": cap.get("evidence"),
        "certification_scope": cap.get("certification_scope"),
        "route_stability_required": cap.get("route_stability_required"),
        "default_expectation": cap.get("default_expectation"),
        "default_expectations": dict(cap.get("default_expectations") or {}),
        "model_expectations": dict(cap.get("model_expectations") or {}),
        "expectations": expectations,
        "resolved_expectations": resolved,
        "supported_profiles": sorted(
            profile for profile, expectation in resolved.items()
            if expectation == "supported"
        ),
        "unsupported_profiles": sorted(
            profile for profile, expectation in resolved.items()
            if expectation == "unsupported"
        ),
        "default_parameter_expectations": dict(
            cap.get("default_parameter_expectations") or {}
        ),
        "model_parameter_expectations": dict(
            cap.get("model_parameter_expectations") or {}
        ),
        "parameter_expectations": dict(cap.get("parameter_expectations") or {}),
        "resolved_parameter_expectations": resolved_parameters,
        "supported_parameters": sorted(
            parameter
            for parameter, expectation in resolved_parameters.items()
            if expectation == "supported"
        ),
        "unsupported_parameters": sorted(
            parameter
            for parameter, expectation in resolved_parameters.items()
            if expectation == "unsupported"
        ),
        "pressure_profiles": dict(cap.get("pressure_profiles") or {}),
        "pressure_omit_params": list(cap.get("pressure_omit_params") or []),
        "pressure_parameter_aliases": dict(
            cap.get("pressure_parameter_aliases") or {}
        ),
        "pressure_overrides": deep_merge(
            {},
            cap.get("pressure_overrides") or {},
        ),
        "pressure_transport_overrides": deep_merge(
            {},
            cap.get("pressure_transport_overrides") or {},
        ),
        "parameter_test_enabled": cap.get("parameter_test_enabled"),
        "pressure_test_enabled": cap.get("pressure_test_enabled"),
        "disabled_reason": cap.get("disabled_reason"),
        "identity": deep_merge({}, cap.get("identity") or {}),
        "response_validators": list(cap.get("response_validators") or []),
        "usage_schema": deep_merge({}, cap.get("usage_schema") or {}),
        "cache_policy": deep_merge({}, cap.get("cache_policy") or {}),
    }


def model_reference_spec_payload(
    modality: str,
    family: str,
    model: str,
    source_id: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
    provider_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = reference_spec_payload(source_id)
    profiles = list(payload.get("test_profiles") or [])
    capability = capability_profile_snapshot(
        modality,
        family,
        model,
        profiles,
        reference_source=source_id,
        api_form=api_form,
        route_profile=route_profile,
        provider_override=provider_override,
    )
    resolved_profiles = capability["resolved_expectations"]
    resolved_parameters = capability["resolved_parameter_expectations"]
    rows: list[dict[str, Any]] = []
    for raw in payload["comparison"]:
        row = dict(raw)
        row_profiles = list(row.get("test_profiles") or [])
        row["model_expectation"] = resolved_parameters.get(
            str(row["parameter"]),
            "supported",
        )
        row["profile_expectations"] = {
            profile: resolved_profiles.get(profile, "supported")
            for profile in row_profiles
        }
        row["local"] = row["model_expectation"]
        rows.append(row)
    payload["params"] = rows
    payload["comparison"] = rows
    payload["model_capability_profile"] = capability
    return payload


def pressure_profiles_for_model(
    family: str,
    model: str,
    reference_source: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> list[str]:
    capability = load_model_capability_profile(
        "text",
        family,
        model,
        reference_source=reference_source,
        api_form=api_form,
        route_profile=route_profile,
    )
    if capability.get("known_model") is not True:
        raise ValueError(
            f"Missing registered text model capability profile for {family}/{model}."
        )
    if capability.get("pressure_test_enabled") is not True:
        raise ValueError(
            f"Pressure testing is disabled for {family}/{model}: "
            f"{capability.get('disabled_reason') or 'model profile policy'}."
        )
    configured = (capability.get("pressure_profiles") or {}).get(reference_source)
    suite_profiles = test_profiles_for_reference(reference_source)
    candidates = list(configured) if configured is not None else suite_profiles
    unknown = sorted(set(candidates) - set(suite_profiles))
    if unknown:
        raise RuntimeError(
            f"Capability pressure_profiles.{reference_source} contains unknown "
            f"profiles: {unknown}."
        )
    return [
        profile
        for profile in candidates
        if resolve_profile_expectation(
            "text",
            family,
            model,
            profile,
            capability_profile=capability,
            reference_source=reference_source,
        )
        == "supported"
    ]


def _resolved_parameter_expectation_for_row(
    capability: dict[str, Any],
    modality: str,
    family: str,
    model: str,
    row: dict[str, Any],
    resolved_profiles: dict[str, str],
) -> str:
    parameter = str(row["parameter"])
    explicit = dict(capability.get("parameter_expectations") or {})
    if parameter in explicit:
        return normalize_expectation(explicit[parameter])
    if str(row.get("official") or "") == "unsupported":
        return "unsupported"
    profile_expectations = [
        resolved_profiles[profile]
        for profile in (row.get("test_profiles") or [])
        if profile in resolved_profiles
    ]
    if profile_expectations and all(
        expectation == "unsupported" for expectation in profile_expectations
    ):
        return "unsupported"
    return resolve_parameter_expectation(
        modality,
        family,
        model,
        parameter,
        capability_profile=capability,
    )


def _match_model_profile(
    models: dict[str, Any],
    model: str,
) -> tuple[str | None, dict[str, Any] | None]:
    if model in models:
        raw = models[model]
        return model, raw if isinstance(raw, dict) else raw
    folded = model.casefold()
    for profile_id, raw in models.items():
        if str(profile_id).casefold() == folded:
            return str(profile_id), raw if isinstance(raw, dict) else raw
        cfg = raw if isinstance(raw, dict) else {}
        aliases = cfg.get("aliases") or []
        if any(str(alias).casefold() == folded for alias in aliases):
            return str(profile_id), cfg
    return None, None


def _normalize_reference_sources(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RuntimeError("reference_sources must map transport -> source ID")
    result: dict[str, str] = {}
    for transport, source in raw.items():
        transport_key = str(transport).strip()
        source_id = str(source).strip()
        if not transport_key or not source_id:
            raise RuntimeError("reference_sources requires non-empty transport/source IDs")
        result[transport_key] = source_id
    return result


def _normalize_pressure_profiles(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise RuntimeError("pressure_profiles must map reference source -> profile list")
    return {
        str(source): _normalize_name_list(
            profiles,
            label=f"pressure_profiles.{source}",
        )
        for source, profiles in raw.items()
    }


def _normalize_parameter_aliases(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RuntimeError(
            "pressure_parameter_aliases must map source parameter -> target parameter"
        )
    result: dict[str, str] = {}
    for source, target in raw.items():
        source_name = str(source).strip()
        target_name = str(target).strip()
        if not source_name or not target_name:
            raise RuntimeError(
                "pressure_parameter_aliases requires non-empty parameter names"
            )
        result[source_name] = target_name
    return result


def _normalize_pressure_overrides(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError("pressure_overrides must be a mapping")
    result: dict[str, Any] = {}
    for parameter, value in raw.items():
        name = str(parameter).strip()
        if not name:
            raise RuntimeError(
                "pressure_overrides requires non-empty parameter names"
            )
        result[name] = value
    return deep_merge({}, result)


def _normalize_pressure_transport_overrides(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise RuntimeError(
            "pressure_transport_overrides must map transport -> pressure policy"
        )
    result: dict[str, dict[str, Any]] = {}
    for transport, raw_policy in raw.items():
        transport_name = str(transport).strip()
        if not transport_name or not isinstance(raw_policy, dict):
            raise RuntimeError(
                "pressure_transport_overrides requires non-empty transport policy mappings"
            )
        unknown = sorted(
            set(raw_policy) - {"omit_params", "parameter_aliases", "overrides"}
        )
        if unknown:
            raise RuntimeError(
                f"pressure_transport_overrides.{transport_name} contains unknown keys: "
                f"{unknown}"
            )
        result[transport_name] = {
            "omit_params": _normalize_name_list(
                raw_policy.get("omit_params") or [],
                label=f"pressure_transport_overrides.{transport_name}.omit_params",
            ),
            "parameter_aliases": _normalize_parameter_aliases(
                raw_policy.get("parameter_aliases") or {}
            ),
            "overrides": _normalize_pressure_overrides(
                raw_policy.get("overrides") or {}
            ),
        }
    return result


def _normalize_name_list(raw: Any, *, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise RuntimeError(f"{label} must be a list")
    result = [str(item).strip() for item in raw]
    if any(not item for item in result):
        raise RuntimeError(f"{label} must not contain empty names")
    if len(set(result)) != len(result):
        raise RuntimeError(f"{label} must not contain duplicates")
    return result


def _merge_expectation_layers(
    layers: list[Any],
    key: str,
    *,
    label: str | None = None,
) -> dict[str, str]:
    """Merge expectation maps from config layers; later layers override earlier ones."""
    map_label = label or key
    merged: dict[str, str] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        raw = layer.get(key)
        if raw is None:
            continue
        merged.update(_normalize_expectation_map(raw, label=map_label))
    return merged


def _normalize_expectation_map(
    raw: Any,
    *,
    label: str = "expectations",
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"{label} must be a mapping of name -> supported|unsupported"
        )
    result: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        expected = str(value).strip().casefold()
        if expected not in VALID_EXPECTATIONS:
            raise RuntimeError(
                f"Invalid expectation for {name!r}: {value!r} "
                f"(expected one of {sorted(VALID_EXPECTATIONS)})"
            )
        result[name] = expected
    return result


@lru_cache(maxsize=16)
def _read_yaml_cached(path: str, _mtime_ns: int) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML object.")
    return payload
