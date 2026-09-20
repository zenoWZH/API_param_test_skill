from __future__ import annotations

import copy
import re
from typing import Any

from .config import (
    deep_merge,
    get_active_provider_name,
    get_model_api_form,
    get_model_reference_source,
    get_model_route_profile,
    get_selected_model,
    get_provider_config,
)
from .param_outcome import normalize_expectation
from .parameter_reference_policy import non_thinking_probe_policy

_BEHAVIOR_METADATA_FIELDS = ("deprecated", "effect_support", "request_acceptance", "http_rejection_expected")


def catalog_model_id_for_runtime(
    config: dict[str, Any],
    model: str | None,
    provider: str | None,
) -> str:
    """Map a provider request id to the official MPDB model slug when configured."""
    runtime_id = str(model or "")
    if not runtime_id or not provider:
        return runtime_id
    mapping = (get_provider_config(config, provider).get("models") or {}).get(
        "reference_model_ids"
    ) or {}
    if isinstance(mapping, dict) and mapping.get(runtime_id):
        return str(mapping[runtime_id])
    return runtime_id


def _app_transport_capability_view(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("api_form") != "deepseek_beta_chat_prefix":
        return record
    result = dict(record)
    result["catalog_execution_flags"] = copy.deepcopy(record.get("catalog_execution_flags")) or {
        key: record.get(key) for key in ("enabled", "executable", "parameter_test_enabled", "pressure_test_enabled")
    }
    result.update(app_transport_available=False, executable=False,
                  parameter_test_enabled=False, pressure_test_enabled=False)
    from .deepseek_beta_reference import (
        CONTRACT_ID, MODEL_SLUG, ENDPOINT, CASE_IDS, canonical_bytes,
        build_deepseek_beta_reference_plan,
    )
    contract_id = record.get("reference_contract_id") or record.get("contract_id") or record.get("id")
    if (contract_id != CONTRACT_ID or record.get("source_id") != "deepseek" or record.get("disabled_reason")
            or any(record.get(flag) is False for flag in ("enabled", "executable", "parameter_test_enabled"))):
        return result
    try:
        from .model_profile_catalog import catalog_capability_profile
        import hashlib
        snapshot = copy.deepcopy(record.get("model_profile_database"))
        if not snapshot:
            snapshot = catalog_capability_profile("text", "deepseek", MODEL_SLUG,
                api_form="deepseek_beta_chat_prefix", route_profile="vendor_direct",
                reference_contract_id=CONTRACT_ID)["model_profile_database"]
        # This projection describes the installed official-source runner. The
        # dispatcher separately validates the actual configured execution target.
        snapshot = copy.deepcopy(snapshot)
        snapshot["execution_target"] = {"provider_id": "deepseek_official", "request_model_id": "deepseek-v4-pro",
                                        "route_profile": "vendor_direct", "api_form": "deepseek_beta_chat_prefix"}
        snapshot.pop("snapshot_digest", None)
        snapshot["snapshot_digest"] = hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
        build_deepseek_beta_reference_plan(snapshot, endpoint=ENDPOINT)
        result.update(app_transport_available=True, executable=True, parameter_test_enabled=True,
                      test_profiles=list(CASE_IDS), fixed_case_suite=True, fixed_request_count=5,
                      param_test_runs=1, identity_probe_requests=0, generic_profile_runner_available=False)
    except (KeyError, ValueError, RuntimeError):
        result["disabled_reason"] = "The installed Pro beta observations or fixed case definitions failed validation."
    return result


def load_reference_specs() -> dict[str, Any]:
    """Return the shared MPDB Contract/Test Binding projection."""
    from .model_profile_catalog import get_model_profile_catalog, reference_specs_projection

    projection = reference_specs_projection()
    sources = dict(projection["reference_sources"])
    for contract in get_model_profile_catalog().list_contracts():
        contract_id = contract["contract_id"]
        metadata = {
            name: {key: copy.deepcopy(capability[key]) for key in _BEHAVIOR_METADATA_FIELDS if key in capability}
            for name, capability in (contract.get("parameter_capabilities") or {}).items()
            if isinstance(capability, dict) and any(key in capability for key in _BEHAVIOR_METADATA_FIELDS)
        }
        if not metadata or contract_id not in sources:
            continue
        source = dict(sources[contract_id])
        source["params"] = {name: {**value, **metadata.get(name, {})} for name, value in source["params"].items()}
        sources[contract_id] = source
    return {**projection, "reference_sources": {key: _app_transport_capability_view(value) for key, value in sources.items()}}


def get_reference_source(source_id: str | None = None) -> dict[str, Any]:
    if source_id:
        # Resolve one exact Contract instead of repeatedly copying every
        # reference and its archived observations for each parameter row.
        from .model_profile_catalog import get_model_profile_catalog, reference_contract_payload
        try:
            source = reference_contract_payload(source_id)
            contract = get_model_profile_catalog().get_contract(source_id)
        except KeyError as exc:
            raise KeyError(f"Reference Contract {source_id!r} not found in the shared MPDB projection") from exc
        if not source.get("test_profiles") or not source.get("params"):
            raise KeyError(f"Reference Contract {source_id!r} not found in the shared MPDB projection")
        source["params"] = {
            name: {**value, **{key: copy.deepcopy(capability[key])
                              for key in _BEHAVIOR_METADATA_FIELDS if key in capability}}
            for name, value in source["params"].items()
            for capability in [(contract.get("parameter_capabilities") or {}).get(name) or {}]
        }
        return _source_payload(source_id, _app_transport_capability_view(source))
    specs = load_reference_specs()
    sources = specs["reference_sources"]
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
                "reference_contract_id": str(
                    raw.get("reference_contract_id") or source_id
                ),
                "source_id": str(raw.get("source_id") or ""),
                "source_ids": [
                    str(value) for value in raw.get("source_ids") or []
                ],
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
                **{key: copy.deepcopy(raw[key]) for key in ("app_transport_available", "catalog_execution_flags", "fixed_case_suite", "fixed_request_count", "param_test_runs") if key in raw},
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
    from .model_profile_catalog import default_reference_contract_for_family

    return default_reference_contract_for_family(
        str(family).strip(),
        route_profile=route_profile,
        api_form=api_form,
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
    selected_provider = provider or get_active_provider_name(config)
    selected_model = str(model or get_selected_model(config, selected_provider))
    selected_route = route_profile or get_model_route_profile(
        config, selected_model, selected_provider
    )
    selected_form = api_form or get_model_api_form(
        config, selected_model, selected_provider, route_profile=selected_route
    )
    override = get_model_reference_source(
        config,
        selected_model,
        selected_provider,
        route_profile=selected_route,
        api_form=selected_form,
    )
    if override:
        get_reference_source(override)
    from .model_profile_catalog import resolve_runtime_parameter_config

    parameter_config = resolve_runtime_parameter_config(
        config,
        selected_provider,
        selected_model,
        family,
        selected_route,
        selected_form,
        modality="text",
        contract_id=override,
    )
    return str(parameter_config["contract_id"])


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
    selected_provider = provider or get_active_provider_name(config)
    from .model_profile_catalog import resolve_runtime_parameter_config
    parameter_config = resolve_runtime_parameter_config(
        config,
        selected_provider,
        model,
        family,
        selected_route,
        selected_form,
        modality="text",
        contract_id=get_model_reference_source(
            config, model, selected_provider, route_profile=selected_route, api_form=selected_form
        ),
    )
    return reference_sources_from_parameter_config(parameter_config, family, selected_form)


def reference_sources_from_parameter_config(
    parameter_config: dict[str, Any], family: str, api_form: str,
) -> list[str]:
    """List contracts from one already resolved source-local parameter snapshot."""
    default_source = str(parameter_config["contract_id"])
    database = parameter_config["model_profile_database"]
    if (database.get("suite_family_id") != family or database.get("api_form") != api_form
            or database.get("reference_contract_id") != default_source):
        raise ValueError("Reference list conflicts with its resolved parameter snapshot")
    declared = [
        str(item)
        for item in (
            (parameter_config.get("test_binding") or {}).get(
                "reference_contract_ids"
            )
            or []
        )
    ]
    if default_source not in declared:
        raise ValueError(
            f"Reference source {default_source!r} selected for {family} "
            "is not declared by its family/model capability suite."
        )
    result = [default_source, *declared]
    unique = list(dict.fromkeys(result))
    for source_id in unique:
        source = get_reference_source(source_id)
        if str(source.get("model_family") or "") != family:
            raise ValueError(
                f"Reference Contract {source_id!r} belongs to family "
                f"{source.get('model_family')!r}, not {family!r}."
            )
        if str(source.get("api_form") or "") != api_form:
            raise ValueError(
                f"Reference Contract {source_id!r} belongs to API form "
                f"{source.get('api_form')!r}, not {api_form!r}."
            )
    return unique


def comparison_reference_source_for_model(
    modality: str,
    family: str,
    model: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> str:
    """Return the provider-independent source used for fair model comparisons."""
    capability = load_model_capability_profile(
        modality,
        family,
        model,
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
    return _reference_param_rows_from_source(source)


def _reference_param_rows_from_source(
    source: dict[str, Any],
) -> list[dict[str, Any]]:
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
                **{key: copy.deepcopy(cfg[key]) for key in _BEHAVIOR_METADATA_FIELDS if key in cfg},
            }
        )
    return rows


def _reference_param_rows_from_snapshot(
    capability: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild parameter rows only from the immutable Contract/Test Binding."""

    contract = capability.get("reference_contract")
    parameter_binding = capability.get("parameter_test_binding")
    if not isinstance(contract, dict):
        return []
    parameter_binding = (
        parameter_binding if isinstance(parameter_binding, dict) else {}
    )
    coverage = parameter_binding.get("parameter_coverage") or {}
    source = {
        "test_profiles": list(parameter_binding.get("test_cases") or []),
        "params": {
            str(name): {
                "required": bool(
                    raw.get("required", False)
                    if isinstance(raw, dict)
                    else False
                ),
                "supported": (
                    str(raw.get("state") or "supported") != "unsupported"
                    if isinstance(raw, dict)
                    else True
                ),
                "coverage": str(
                    coverage.get(name) or "not tested"
                    if isinstance(coverage, dict)
                    else "not tested"
                ),
                **{
                    key: copy.deepcopy(raw[key])
                    for key in _BEHAVIOR_METADATA_FIELDS
                    if isinstance(raw, dict) and key in raw
                },
            }
            for name, raw in (contract.get("parameter_capabilities") or {}).items()
        },
    }
    return _reference_param_rows_from_source(source)


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
        "reference_contract_id": str(
            raw.get("reference_contract_id") or source_id
        ),
        "source_id": str(raw.get("source_id") or ""),
        "source_ids": [str(value) for value in raw.get("source_ids") or []],
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
        **{key: copy.deepcopy(raw[key]) for key in ("enabled", "profile_eligible", "parameter_test_enabled", "pressure_test_enabled", "app_transport_available", "catalog_execution_flags", "fixed_case_suite", "fixed_request_count", "param_test_runs") if key in raw},
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
        # dynamic_aggregator may run an origin vendor_direct/vendor_compat
        # matrix as a comparison suite. Capability profiles already restrict
        # which origin sources are allowed; do not relabel the live route.
        aggregator_comparison = str(route_profile) == "dynamic_aggregator" and source_route in {
            "vendor_direct",
            "vendor_compat",
        }
        if not aggregator_comparison:
            raise ValueError(
                f"Reference source {source['id']!r} belongs to route profile "
                f"{source_route!r}, not {route_profile!r}."
            )


def _coverage_mentions_profile(coverage: str, profile: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(profile)}(?![A-Za-z0-9_.-])", coverage))


def load_model_capability_profiles() -> dict[str, Any]:
    """Return the shared MPDB Profile/Interface/Test Binding projection."""
    from .model_profile_catalog import capability_profiles_projection

    return capability_profiles_projection()


def _read_only_gc_observation_capability(
    modality: str, family: str, model: str, *, api_form: str | None,
    route_profile: str | None, reference_source: str | None,
    provider_override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Read exact core observations without manufacturing an execution binding."""
    source_id, contract_id = "google_ai_studio", "gemini_native_generate_content"
    form, namespace = "gemini_generate_content", "bounded_schema_reference_by_api_version_20260907"
    variant_namespace = "bounded_schema_reference_request_variants_20260907"
    if (modality != "text" or family != "gemini" or reference_source != contract_id
            or api_form not in (None, form) or route_profile not in (None, source_id)
            or provider_override not in (None, {})):
        return None
    from .model_profile_catalog import get_model_profile_catalog

    catalog = get_model_profile_catalog()
    contract, source = catalog.get_contract(contract_id), catalog.get_source(source_id)
    if (contract.get("source_id") != source_id or contract.get("source_ids") != [source_id]
            or contract.get("family_id") != family or contract.get("api_form") != form
            or contract.get("routing_mode") != source_id or source.get("source_type") != "official_direct"
            or source.get("authority") != "origin_vendor"):
        raise ValueError("Read-only observations require the exact official source/Contract.")
    selected = []
    for raw in catalog.list_profiles(modality=modality, source=source_id, family=family):
        profile = catalog.get_profile(raw["profile_id"])
        if model not in {profile["model_slug"], *profile.get("request_model_ids", [])}:
            continue
        iid = profile["profile_id"] + "#gemini-generate-content-default"
        if iid not in profile.get("interface_ids", []):
            continue
        interface = catalog.get_interface(iid)
        conflicts = interface.get("source_conflicts") or {}
        observations = conflicts.get(namespace) or {}
        observed_ids = {entry.get("request_model_id") for entry in observations.values() if isinstance(entry, dict)} if isinstance(observations, dict) else set()
        variant_projection = model != profile["model_slug"] and model not in observed_ids
        if variant_projection:
            variants = conflicts.get(variant_namespace) or {}
            observations = variants.get(model) if isinstance(variants, dict) else None
        if not isinstance(observations, dict) or not observations:
            continue  # An unobserved request ID cannot borrow its parent's samples.
        if (interface.get("source_id") != source_id or interface.get("api_form") != form
                or interface.get("routing_mode") != source_id or interface.get("profile_id") != profile["profile_id"]
                or interface.get("default_contract_id") != contract_id or contract_id not in interface.get("contract_ids", [])):
            raise ValueError("Read-only observations crossed exact Profile/Interface identity.")
        for version, entry in observations.items():
            if (version not in {"v1", "v1beta"} or not isinstance(entry, dict)
                    or entry.get("source_id") != source_id or entry.get("api_form") != form
                    or entry.get("ordinary_interface_id") != iid or entry.get("api_version") != version
                    or entry.get("request_model_id") not in profile.get("request_model_ids", [])
                    or variant_projection and (entry.get("request_model_id") != model or entry.get("no_parent_request_id_success_inference") is not True)
                    or entry.get("no_cross_version_success_inference") is not True
                    or interface.get("api_versions", {}).get(version, {}).get("path_template") != f"/{version}/models/{{model}}:generateContent"):
                raise ValueError("Read-only observations lost their exact version or identity.")
        policies = catalog.list_test_bindings(interface_id=iid, extension_type="model_test_policy")
        if (len(policies) == 1 and policies[0].get("parameter_test_enabled") is True
                and interface.get("test_binding_status") == "required"
                and interface.get("enabled") is True and interface.get("executable") is True):
            return None  # Certified displays continue through the normal strict chain.
        if len(policies) > 1 or any(p.get("source_id") != source_id for p in policies):
            raise ValueError("Read-only observations cannot resolve ambiguous or cross-source policies.")
        selected.append((profile, interface, observations, variant_projection))
    if len(selected) != 1:
        return None
    profile, interface, observations, variant_projection = selected[0]
    # A selected request variant exposes only its own version observations.
    # The parent Interface's stored metadata remains unchanged in the catalog.
    visible_conflicts = ({namespace: copy.deepcopy(observations), variant_namespace: {model: copy.deepcopy(observations)}}
                         if variant_projection else copy.deepcopy(interface.get("source_conflicts") or {}))
    capabilities = deep_merge(contract.get("parameter_capabilities") or {}, interface.get("parameter_capabilities") or {})
    return {
        "storage_source": "model_profile_database", "legacy_yaml_read": False,
        "read_only_core_facts": True, "reference_only": True, "modality": modality, "family": family,
        "suite_family_id": family, "canonical_family_id": family, "model": model,
        "source_id": source_id, "profile_id": profile["profile_id"], "interface_id": interface["interface_id"],
        "profile_status": interface.get("test_binding_status"), "test_binding_status": interface.get("test_binding_status"),
        "test_binding_id": None, "parameter_test_binding_id": None,
        "enabled": False, "executable": False, "runner_enabled": False, "parameter_test_enabled": False, "pressure_test_enabled": False,
        "test_policy_parameter_test_enabled": False, "test_policy_pressure_test_enabled": False,
        "reference_source_enabled": False, "reference_source_executable": False,
        "known_model": True, "known_api_profile": True, "api_form": form,
        "transport": interface.get("transport_adapter_id"), "route_profile": source_id,
        "reference_source": contract_id, "reference_contract_id": contract_id,
        "allowed_reference_sources": [], "allowed_reference_contract_ids": [],
        "default_api_version": interface.get("default_api_version"), "api_versions": copy.deepcopy(interface.get("api_versions") or {}),
        "parameter_capabilities": copy.deepcopy(capabilities),
        "parameter_constraints": deep_merge(contract.get("parameter_constraints") or {}, interface.get("parameter_constraints") or {}),
        "source_conflicts": visible_conflicts,
        **({"observation_request_model_id": model, "observation_projection": "request_variant_only"} if variant_projection else {}),
        "default_expectation": "reference_only", "expectations": {}, "parameter_expectations": {},
        "execution_target": None, "execution_target_boundary": {"included": False, "used_for_reference_resolution": False},
        "test_scope": "read_only_core_observations", "certification_scope": "no_execution_binding_created",
        "disabled_reason": "Reference display only; the ordinary parameter binding is absent, disabled or uncertified.",
        "catalog_digest": catalog.payload["catalog_digest"], "test_extension_digest": catalog.payload["test_extension_digest"],
    }


def _read_only_recorded_observation_capability(
    modality, family, model, *, api_form, route_profile, reference_source, provider_override,
):
    from .model_profile_catalog import get_model_profile_catalog
    from model_profile_db.recorded_reference import CLOSED_RECORDED_CONTRACTS, recorded_reference_capability
    if reference_source not in CLOSED_RECORDED_CONTRACTS:
        return None
    return recorded_reference_capability(get_model_profile_catalog(), modality=modality, family=family,
        model=model, reference_contract_id=reference_source, api_form=api_form,
        route_profile=route_profile, provider_override=provider_override)


def _read_only_observation_snapshot(cap: dict[str, Any], reference_source: str | None) -> dict[str, Any]:
    result = copy.deepcopy(cap)
    parameters = (list(cap.get("parameter_capabilities") or {}) if cap.get("read_only_recorded_reference") is True
                  else [row["parameter"] for row in reference_param_rows(reference_source)])
    result.update(selected_reference_source=reference_source, resolved_expectations={}, supported_profiles=[], unsupported_profiles=[],
                  resolved_parameter_expectations={parameter: "reference_only" for parameter in parameters},
                  supported_parameters=[], unsupported_parameters=[])
    return result


def load_model_capability_profile(
    modality: str,
    family: str,
    model: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
    reference_source: str | None = None,
    provider_override: dict[str, Any] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Load one exact Profile/Interface/Test Binding/Contract chain from MPDB."""
    if type(read_only) is not bool:
        raise ValueError("read_only must be boolean")
    if read_only:
        recorded = _read_only_recorded_observation_capability(modality, family, model,
            api_form=api_form, route_profile=route_profile, reference_source=reference_source,
            provider_override=provider_override)
        if recorded is not None:
            return recorded
        core = _read_only_gc_observation_capability(
            str(modality).strip().casefold(), str(family).strip(), str(model).strip(),
            api_form=api_form, route_profile=route_profile, reference_source=reference_source,
            provider_override=provider_override,
        )
        if core is not None:
            return core
    from .model_profile_catalog import catalog_capability_profile

    capability = catalog_capability_profile(
        str(modality).strip().casefold(),
        str(family).strip(),
        str(model).strip(),
        api_form=api_form,
        route_profile=route_profile,
        reference_contract_id=reference_source,
        provider_override=provider_override,
    )
    snapshot = capability.get("model_profile_database") or {}
    capability["parameter_capabilities"] = deep_merge(
        (snapshot.get("reference_contract") or {}).get("parameter_capabilities") or {},
        (snapshot.get("interface") or {}).get("parameter_capabilities") or {},
    )
    return _app_transport_capability_view(_project_gemini37_rejection_expectations(capability))


def _project_gemini37_rejection_expectations(capability, parameter_rows=None):
    """Project exact model field rejections without requiring duplicate overrides."""
    contract_id = (capability.get("reference_contract_id") or capability.get("reference_source")
                   or capability.get("default_reference_source"))
    if (capability.get("source_id") != "google_ai_studio"
            or capability.get("profile_id") != "text/google_ai_studio/gemini/gemini-3.7-flash"
            or capability.get("api_form") != "gemini_generate_content"
            or contract_id != "gemini_3_7_flash_generate_content"):
        return capability
    result = copy.deepcopy(capability)
    unsupported = {name for name, value in result.get("parameter_capabilities", {}).items()
                   if value.get("state") == "unsupported" and value.get("http_rejection_expected") is not False}
    for name in unsupported:
        result.setdefault("parameter_expectations", {}).setdefault(name, "unsupported")
    for row in (reference_param_rows(contract_id) if parameter_rows is None else parameter_rows):
        if row["parameter"] in unsupported:
            for case in row["test_profiles"]:
                result.setdefault("expectations", {}).setdefault(case, "unsupported")
    return result


def pressure_test_runnable(capability: dict[str, Any]) -> bool:
    """Return true only for explicitly pressure-enabled text capabilities."""
    return (
        str(capability.get("modality") or "") == "text"
        and capability.get("pressure_test_enabled") is True
    )


def resolve_profile_expectation(
    modality: str,
    family: str,
    model: str,
    profile: str,
    *,
    capability_profile: dict[str, Any] | None = None,
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
    parameter_probe: bool = True,
) -> str:
    """Resolve supported/unsupported for one probe within a family suite."""
    profile_key = str(profile).strip()
    if not profile_key:
        raise ValueError("profile is required")
    cap = capability_profile or load_model_capability_profile(
        modality,
        family,
        model,
        api_form=api_form,
        route_profile=route_profile,
        reference_source=reference_source,
    )
    if cap.get("read_only_core_facts") is True:
        raise ValueError("Read-only core observations cannot resolve execution expectations")
    source_policy = non_thinking_probe_policy(cap, profile_key) if parameter_probe else {}
    if source_policy:
        return source_policy["expectation"]
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
        api_form=api_form,
        route_profile=route_profile,
    )
    if cap.get("read_only_core_facts") is True:
        raise ValueError("Read-only core observations cannot resolve execution expectations")
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
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
) -> dict[str, str]:
    cap = load_model_capability_profile(
        modality,
        family,
        model,
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
    reference_source: str | None = None,
    api_form: str | None = None,
    route_profile: str | None = None,
    provider_override: dict[str, Any] | None = None,
    model_profile_database: dict[str, Any] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    immutable_snapshot = isinstance(model_profile_database, dict)
    if immutable_snapshot:
        from .model_profile_catalog import capability_profile_from_database_snapshot

        cap = capability_profile_from_database_snapshot(model_profile_database)
        cap["parameter_capabilities"] = deep_merge(
            (cap.get("reference_contract") or {}).get("parameter_capabilities") or {},
            (cap.get("interface") or {}).get("parameter_capabilities") or {},
        )
        cap = _project_gemini37_rejection_expectations(
            cap, parameter_rows=_reference_param_rows_from_snapshot(cap)
        )
        expected = {
            "modality": modality,
            "family": family,
            "model": model,
            "api_form": api_form,
            "route_profile": route_profile,
            "reference_contract_id": reference_source,
        }
        conflicts = [
            field
            for field, value in expected.items()
            if value not in (None, "") and str(cap.get(field) or "") != str(value)
        ]
        if conflicts:
            raise ValueError(
                "Capability request conflicts with immutable MPDB snapshot: "
                + ", ".join(conflicts)
            )
    else:
        cap = load_model_capability_profile(
            modality,
            family,
            model,
            api_form=api_form,
            route_profile=route_profile,
            reference_source=reference_source,
            provider_override=provider_override,
            read_only=read_only,
        )
    selected_reference_source = reference_source or str(
        cap.get("reference_contract_id") or cap.get("reference_source") or ""
    )
    if cap.get("read_only_core_facts") is True:
        return _read_only_observation_snapshot(cap, reference_source)
    profile_list = [str(item) for item in (profiles or [])]
    resolved = {
        profile: resolve_profile_expectation(
            modality,
            family,
            model,
            profile,
            capability_profile=cap,
            reference_source=None if immutable_snapshot else reference_source,
        )
        for profile in profile_list
    }
    resolved_parameters: dict[str, str] = {}
    if selected_reference_source:
        parameter_rows = (
            _reference_param_rows_from_snapshot(cap)
            if immutable_snapshot
            else reference_param_rows(selected_reference_source)
        )
        resolved_parameters = {
            str(row["parameter"]): _resolved_parameter_expectation_for_row(
                cap,
                modality,
                family,
                model,
                row,
                resolved,
            )
            for row in parameter_rows
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
        "suite_family_id": cap.get("suite_family_id"),
        "canonical_family_id": cap.get("canonical_family_id"),
        "model": cap["model"],
        "canonical_model_slug": cap.get("canonical_model_slug"),
        "profile_id": cap.get("profile_id"),
        "model_api_profile_id": cap.get("model_api_profile_id"),
        "interface_id": cap.get("interface_id"),
        "source_id": cap.get("source_id"),
        "test_binding_id": cap.get("test_binding_id"),
        "reference_contract_id": cap.get("reference_contract_id"),
        "parameter_test_binding_id": cap.get("parameter_test_binding_id"),
        "reference_identity": copy.deepcopy(cap.get("reference_identity") or {}),
        "profile_status": cap.get("profile_status"),
        "api_form": cap.get("api_form"),
        "transport": cap.get("transport"),
        "route_profile": cap.get("route_profile"),
        "route_profile_known": cap.get("route_profile_known"),
        "reference_source": cap.get("reference_source"),
        "comparison_reference_source": cap.get("comparison_reference_source"),
        "selected_reference_source": selected_reference_source,
        "source_id": cap.get("source_id"),
        "interface_id": cap.get("interface_id"),
        "parameter_constraints": copy.deepcopy(cap.get("parameter_constraints") or {}),
        "parameter_capabilities": copy.deepcopy(cap.get("parameter_capabilities") or {}),
        "source_conflicts": copy.deepcopy(cap.get("source_conflicts") or {}),
        **{key: copy.deepcopy(cap[key]) for key in ("app_transport_available", "catalog_execution_flags") if key in cap},
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
        "image_case_expectations": copy.deepcopy(cap.get("image_case_expectations") or {}),
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
        "test_policy_parameter_test_enabled": cap.get("test_policy_parameter_test_enabled"),
        "validation_api_version": cap.get("validation_api_version"),
        "image_case_expectations": copy.deepcopy(cap.get("image_case_expectations") or {}),
        "pressure_test_enabled": cap.get("pressure_test_enabled"),
        "test_policy_pressure_test_enabled": cap.get(
            "test_policy_pressure_test_enabled"
        ),
        "disabled_reason": cap.get("disabled_reason"),
        "identity": deep_merge({}, cap.get("identity") or {}),
        "response_validators": list(cap.get("response_validators") or []),
        "usage_schema": deep_merge({}, cap.get("usage_schema") or {}),
        "cache_policy": deep_merge({}, cap.get("cache_policy") or {}),
        "execution_target": copy.deepcopy(cap.get("execution_target") or {}),
        "model_profile_database": copy.deepcopy(
            cap.get("model_profile_database")
        ),
    }


def _model_reference_param_rows(
    payload: dict[str, Any], capability: dict[str, Any],
) -> list[dict[str, Any]]:
    """Add exact Interface declarations without assigning test coverage."""
    rows = copy.deepcopy(payload["comparison"])
    by_parameter = {row["parameter"]: row for row in rows}
    constraints = capability.get("parameter_constraints") or {}
    for parameter, raw in (capability.get("parameter_capabilities") or {}).items():
        declaration = raw if isinstance(raw, dict) else {}
        parameter = str(parameter)
        state = str(declaration.get("state") or "unknown")
        row = by_parameter.get(parameter)
        if row is None:
            row = {
                "parameter": parameter,
                "local": "reference",
                "coverage": "not tested",
                "coverage_mode": "not_tested",
                "test_profiles": [],
            }
            rows.append(row)
            by_parameter[parameter] = row
        row.update(
            state=state,
            official=(
                "required"
                if state == "supported" and declaration.get("required")
                else state
            ),
            source_id=capability.get("source_id"),
            interface_id=capability.get("interface_id"),
            official_sources=copy.deepcopy(
                declaration.get("official_sources") or payload.get("official_sources") or []
            ),
        )
        for key in _BEHAVIOR_METADATA_FIELDS:
            if key in declaration:
                row[key] = copy.deepcopy(declaration[key])
        if parameter in constraints:
            row["parameter_constraints"] = copy.deepcopy(constraints[parameter])
    return rows


def model_reference_spec_payload(
    modality: str,
    family: str,
    model: str,
    source_id: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
    provider_override: dict[str, Any] | None = None,
    model_profile_database: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recorded = _read_only_recorded_observation_capability(modality, family, model,
        api_form=api_form, route_profile=route_profile, reference_source=source_id,
        provider_override=provider_override)
    if recorded is not None:
        from model_profile_db.recorded_reference import recorded_reference_payload
        capability = _read_only_observation_snapshot(recorded, source_id)
        payload = recorded_reference_payload(capability)
    else:
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
            read_only=True,
            model_profile_database=model_profile_database,
        )
    comparison_rows = _model_reference_param_rows(payload, capability)
    payload["param_count"] = len(comparison_rows)
    if capability.get("read_only_core_facts") is True:
        rows = []
        for raw in comparison_rows:
            row = dict(raw)
            row.update(test_profiles=[], profile_expectations={}, coverage_mode="reference_only", coverage="reference_only",
                       model_expectation="reference_only", local="reference_only", execution_coverage="not_executed")
            rows.append(row)
        payload.update(reference_only=True, read_only_core_facts=True, test_profiles=[],
                       enabled=False, executable=False, runner_enabled=False, parameter_test_enabled=False, pressure_test_enabled=False,
                       test_binding_status=capability["test_binding_status"], disabled_reason=capability["disabled_reason"],
                       params=rows, comparison=rows, tested_params=[],
                       untested_params=[row["parameter"] for row in rows], model_capability_profile=capability)
        return payload
    resolved_profiles = capability["resolved_expectations"]
    resolved_parameters = capability["resolved_parameter_expectations"]
    rows: list[dict[str, Any]] = []
    for raw in comparison_rows:
        row = dict(raw)
        row_profiles = list(row.get("test_profiles") or [])
        constraint_controls = {
            profile: non_thinking_probe_policy(capability, profile)["rejection_parameter"]
            for profile in row_profiles
            if non_thinking_probe_policy(capability, profile).get("rejection_parameter")
            and str(row["parameter"]) in {"temperature", "top_p"}
        }
        if constraint_controls:
            row_profiles = [profile for profile in row_profiles if profile not in constraint_controls]
            row["test_profiles"] = row_profiles
            row["not_exercised_controls"] = constraint_controls
            if not row_profiles:
                row["coverage_mode"] = "not_tested"
        state = row.get("state")
        row["model_expectation"] = (
            state if state and state != "supported"
            else resolved_parameters.get(str(row["parameter"]), "supported")
        )
        row["profile_expectations"] = {
            profile: resolved_profiles.get(profile, "supported")
            for profile in row_profiles
        }
        row["local"] = row["model_expectation"]
        rows.append(row)
    payload["params"] = rows
    payload["comparison"] = rows
    payload["tested_params"] = [row["parameter"] for row in rows if row["coverage_mode"] != "not_tested"]
    payload["untested_params"] = [row["parameter"] for row in rows if row["coverage_mode"] == "not_tested"]
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
    if not pressure_test_runnable(capability):
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
            parameter_probe=False,
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
        and not (
            non_thinking_probe_policy(capability, profile).get("rejection_parameter")
            and non_thinking_probe_policy(capability, profile)["rejection_parameter"] != parameter
        )
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
