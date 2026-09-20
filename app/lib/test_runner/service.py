"""Shared credential-free workflow selection, preview and job construction."""
from __future__ import annotations

import copy
from typing import Any

from lib.config import (get_active_provider_name, get_model_route_profile, get_provider_config, get_provider_interface,
                        get_selected_model, get_image_provider_config, get_image_model_config, list_image_providers)
from lib.model_profile_catalog import get_model_profile_catalog
from lib.parameter_job_controls import freeze_workflow_job

from . import HandlerRegistry, compile_plan, validate_plan
from .adapters import fable
from .snapshot import make_workflow_snapshot, validate_workflow_snapshot


def _requested_runs(payload: dict) -> int:
    values = [payload[key] for key in ("runs", "run_count", "param_test_runs") if payload.get(key) is not None]
    if any(type(value) is not int or not 1 <= value <= 1000 for value in values) or len(set(values)) > 1:
        raise ValueError("Workflow run selectors must agree on an integer from 1 to 1000")
    return values[0] if values else 1


def registry_for_plan(plan: dict, *, cleanup_only: bool = False, config: dict | None = None, output_dir=None) -> HandlerRegistry:
    registry = HandlerRegistry()
    factory_id = plan.get("definition", {}).get("factory", {}).get("factory_id")
    if factory_id == fable.FACTORY_ID:
        fable.register_handlers(registry)
    elif factory_id in {"image_parameter", "responses_image"}:
        from .adapters.image import registry_for_image_plan
        if config is None:
            raise ValueError("Image workflow registry requires its runtime configuration")
        return registry_for_image_plan(plan, config, output_dir=output_dir, cleanup_only=cleanup_only)
    elif factory_id == "legacy_parameter":
        from .adapters.legacy_parameter import legacy_parameter_registry
        if config is None:
            raise ValueError("Parameter workflow registry requires its runtime configuration")
        registry = legacy_parameter_registry(config)
    elif factory_id == "kimi_minimax_controls":
        from .adapters.kimi_minimax import registry_for_kimi_minimax_plan
        return registry_for_kimi_minimax_plan(plan)
    elif factory_id == "gemini_resources":
        from .adapters.gemini_resources import gemini_resource_registry
        registry = gemini_resource_registry()
    elif factory_id == "aws_bedrock_mantle_json":
        from .adapters.mantle import register_handlers
        register_handlers(registry)
    elif factory_id == "source_fixed_parameter":
        from .adapters.fixed_parameter import registry_for_fixed_parameter_plan
        return registry_for_fixed_parameter_plan(plan)
    elif factory_id == "deepseek_beta_prefix":
        from .adapters.deepseek_beta import register_handlers
        register_handlers(registry)
    elif factory_id == "media_input":
        from .adapters.media_input import registry_for_media_input_plan
        if config is None:
            raise ValueError("Media workflow registry requires its runtime configuration")
        return registry_for_media_input_plan(config, plan)
    else:
        raise ValueError("Workflow factory is not installed: " + str(factory_id))
    validate_plan(plan, None if cleanup_only else registry)
    return registry


def validate_current_workflow_job(config: dict, job: dict, *, cleanup_only: bool = False, catalog: Any = None) -> HandlerRegistry:
    """Rebuild business plans from registered source; historical reads do not use this."""
    validate_workflow_snapshot(job["test_workflow_snapshot"], job=job)
    plan = job["execution_plan"]
    registry = registry_for_plan(plan, cleanup_only=cleanup_only, config=config)
    if cleanup_only:
        return registry
    arguments = plan["definition"].get("factory_arguments", {})
    payload = {"type": job["type"], "provider": job["provider"], "model": job["model"],
               "route_profile": job["route_profile"], "api_form": job["api_form"],
               "workflow_binding_id": job["test_binding_id"], "workflow_id": plan["workflow_id"],
               "cases": plan["requested_cases"], "runs": plan["run_count"],
               "fixture_evidence": arguments.get("fixture_evidence"), "mcp_fixture": arguments.get("mcp_fixture"),
               "tool_validation_mode": job["parameter_execution"]["tool_validation_mode"], "plan_digest": plan["plan_digest"]}
    payload["timeout_sec"] = plan["limits"]["request_timeout_seconds"]
    if plan["definition"]["factory"]["factory_id"] == "aws_bedrock_mantle_json":
        payload["include_count"] = arguments["include_count"]
    current = preview_test_plan(config, payload, catalog=catalog)
    if current["test_workflow_snapshot"] != job["test_workflow_snapshot"]:
        raise ValueError("Current MPDB workflow reference differs from the frozen snapshot")
    return registry


def validate_current_parameter_job(config: dict, job: dict) -> HandlerRegistry:
    """Reject a rehashed, edited legacy job before constructing a credential client."""
    from lib.job_spec import _current_job_snapshot_integrity
    from lib.parameter_job_controls import parameter_execution_from_job
    parameter_execution_from_job(job)
    valid, reasons = _current_job_snapshot_integrity(job)
    if not valid:
        raise ValueError("Invalid frozen parameter identity: " + ", ".join(reasons))
    plan = job["execution_plan"]
    if plan["definition"]["factory"]["factory_id"] != "legacy_parameter":
        raise ValueError("Expected a frozen legacy parameter factory")
    arguments = plan["definition"]["factory_arguments"]
    payload = {key: job.get(key) for key in (
        "type", "provider", "model", "route_profile", "api_form", "reference_contract_id",
        "source_id", "profile_id", "interface_id")}
    payload.update(cases=arguments["profiles"], plan_seed=arguments["sampling_seed"],
        plan_digest=plan["plan_digest"], runs=plan["run_count"],
        tool_validation_mode=plan["definition"]["tool_validation_mode"],
        timeout_sec=plan["limits"]["request_timeout_seconds"])
    current = _preview_parameter_plan(config, payload, provider=job["provider"], model=job["model"])
    if current["job_spec"]["model_profile_database"] != job["model_profile_database"]:
        raise ValueError("Current MPDB parameter reference differs from its frozen snapshot")
    return registry_for_plan(plan, config=config)


def validate_current_image_job(config: dict, job: dict, *, output_dir=None, cleanup_only=False) -> HandlerRegistry:
    from lib.job_spec import _current_job_snapshot_integrity
    from lib.parameter_job_controls import parameter_execution_from_job
    parameter_execution_from_job(job)
    valid, reasons = _current_job_snapshot_integrity(job)
    if not valid:
        raise ValueError("Invalid frozen image identity: " + ", ".join(reasons))
    plan = job["execution_plan"]
    if cleanup_only:
        return registry_for_plan(plan, config=config, output_dir=output_dir, cleanup_only=True)
    payload = {"type": "image_param_test", "provider": job["provider"], "model": job["model"],
        "image_plan": copy.deepcopy(job["image_plan"]), "run_count": plan["run_count"],
        "prompt": plan["definition"].get("factory_arguments", {}).get("prompt"),
        "timeout_sec": plan["limits"]["request_timeout_seconds"], "plan_digest": plan["plan_digest"]}
    current = _preview_image_plan(config, payload)
    if current["job_spec"]["model_profile_database"] != job["model_profile_database"]:
        raise ValueError("Current MPDB image reference differs from its frozen snapshot")
    return registry_for_plan(plan, config=config, output_dir=output_dir)


def _selection(workflow: dict, payload: dict) -> list[str] | None:
    selectors = payload.get("cases")
    if selectors is None:
        suite = payload.get("suite", "full")
        if suite == "full":
            return None
        selected = [row["id"] for row in workflow["cases"] if row.get("phase", row.get("group")) == suite]
        if not selected:
            raise ValueError("Unknown or empty workflow suite: " + str(suite))
        return selected
    if not isinstance(selectors, list) or not selectors or any(not isinstance(item, str) for item in selectors):
        raise ValueError("cases must be a nonempty list of exact IDs or case names")
    aliases = {}
    for row in workflow["cases"]:
        aliases[row["id"]] = row["id"]
        name = row.get("name")
        if name:
            if name in aliases and aliases[name] != row["id"]:
                raise ValueError("Ambiguous case name requires exact case ID")
            aliases[name] = row["id"]
    if set(selectors) - aliases.keys():
        raise ValueError("Unknown case selector: " + repr(sorted(set(selectors) - aliases.keys())))
    return [aliases[value] for value in selectors]


def _workflow_candidates(config: dict, payload: dict, catalog: Any) -> tuple[str, str, list[dict]]:
    provider = str(payload.get("provider") or get_active_provider_name(config))
    provider = str(get_provider_config(config, provider)["name"])
    model = str(payload.get("model") or get_selected_model(config, provider))
    candidates = catalog.list_workflows(provider_id=provider, request_model_id=model, enabled=True)
    workflow_id = payload.get("workflow_id")
    binding_id = payload.get("workflow_binding_id")
    if not (workflow_id or binding_id):
        # Media input matrices are deliberately opt-in. Existing ordinary and
        # Fable defaults keep their original selection behavior.
        candidates = [row for row in candidates if (row.get("workflow") or {}).get("factory_id") != "media_input"
                      and row.get("factory_id") != "media_input"
                      and not str(row.get("workflow_id", "")).startswith("media-input/")]
    if workflow_id:
        candidates = [row for row in candidates if row["workflow_id"] == workflow_id]
    if binding_id:
        candidates = [row for row in candidates if row["test_binding_id"] == binding_id]
    for field, key in (("source_id", "source_id"), ("profile_id", "profile_id"),
                       ("interface_id", "interface_id"), ("reference_contract_id", "contract_id")):
        if payload.get(field):
            candidates = [row for row in candidates if row[key] == payload[field]]
    requested_form = payload.get("api_form")
    if not requested_form and candidates and not (workflow_id or binding_id):
        # An optional workflow for another API must not replace the configured
        # default parameter route merely because it shares a provider/model.
        from lib.config import get_model_api_form
        route = get_model_route_profile(config, model, provider, route_profile=payload.get("route_profile") or None)
        requested_form = get_model_api_form(config, model, provider, route_profile=route)
    if requested_form:
        candidates = [row for row in candidates if row["execution_target"]["api_form"] == requested_form]
    return provider, model, candidates


def maybe_preview_test_plan(config: dict, payload: dict, *, catalog: Any = None) -> dict | None:
    if payload.get("type", "param_test") != "param_test":
        return None
    if payload.get("parameter_suite"):
        if payload.get("workflow_id") or payload.get("workflow_binding_id"):
            raise ValueError("Choose a fixed parameter suite or a registered workflow")
        return None
    catalog = catalog or get_model_profile_catalog()
    _, _, candidates = _workflow_candidates(config, payload, catalog)
    if not candidates and not (payload.get("workflow_id") or payload.get("workflow_binding_id")):
        return None
    return preview_test_plan(config, payload, catalog=catalog)


def preview_test_plan(config: dict, payload: dict, *, catalog: Any = None) -> dict:
    """Resolve and compile the exact selected workflow without keys or HTTP."""
    if not isinstance(payload, dict):
        raise ValueError("Test plan selection must be an object")
    if payload.get("type") == "image_param_test":
        if payload.get("workflow_id") or payload.get("workflow_binding_id"):
            raise ValueError("Image workflows use their exact image policy binding")
        return _preview_image_plan(config, payload)
    catalog = catalog or get_model_profile_catalog()
    provider, model, candidates = _workflow_candidates(config, payload, catalog)
    if payload.get("parameter_suite"):
        if payload.get("workflow_id") or payload.get("workflow_binding_id"):
            raise ValueError("Choose a fixed parameter suite or a registered workflow")
        return _preview_parameter_plan(config, payload, provider=provider, model=model)
    if not candidates and not (payload.get("workflow_id") or payload.get("workflow_binding_id")):
        if payload.get("type", "param_test") != "param_test":
            raise ValueError("Test plans require a functional parameter or image job")
        return _preview_parameter_plan(config, payload, provider=provider, model=model)
    if len(candidates) != 1:
        raise ValueError(f"Expected one executable workflow for {provider}/{model}; found {len(candidates)}")
    candidate = candidates[0]
    resolved = catalog.resolve_workflow(**{key: candidate[key] for key in (
        "source_id", "profile_id", "interface_id", "contract_id", "execution_target", "workflow_id", "test_binding_id")})
    descriptor = resolved["workflow"]
    registry = HandlerRegistry()
    if descriptor.get("factory_id") == fable.FACTORY_ID:
        factory = fable
        workflow = fable.build_workflow(provider, model, fixture_evidence=payload.get("fixture_evidence"), mcp_fixture=payload.get("mcp_fixture"))
        fable.register_handlers(registry)
        interface = get_provider_interface(config, resolved["execution_target"]["transport_adapter_id"], provider)
        if (str(interface.get("base_url") or "").rstrip("/") != workflow["target"]["base_url"]
                or interface.get("path") != "/messages" or interface.get("auth") != "anthropic"):
            raise ValueError("Configured route differs from the registered Fable workflow endpoint")
    elif descriptor.get("factory_id") == "aws_bedrock_mantle_json":
        from .adapters import mantle
        factory = mantle
        original, registry = mantle.prepare_mantle_plan(config, model, catalog,
            include_count=payload.get("include_count", False), runs=_requested_runs(payload))
        workflow = copy.deepcopy(original["definition"])
        if provider != mantle.PROVIDER:
            raise ValueError("Mantle workflow requires its exact official provider")
    elif descriptor.get("factory_id") == "deepseek_beta_prefix":
        from .adapters import deepseek_beta
        factory = deepseek_beta
        workflow = deepseek_beta.build_definition(config, model, catalog=catalog)
        # The MPDB execution identity has four fields; route is separately frozen.
        workflow["target"]["execution_target"].pop("route_profile", None)
        deepseek_beta.register_handlers(registry)
        if provider != "deepseek_official":
            raise ValueError("Beta workflow requires its exact official provider")
    elif descriptor.get("factory_id") == "media_input":
        from .adapters import media_input
        factory = media_input
        workflow = media_input.build_definition(config, resolved)
        media_input.register_handlers(registry)
    else:
        raise ValueError("Workflow factory is not installed: " + str(descriptor.get("factory_id")))
    if descriptor["version"] != factory.FACTORY_VERSION or descriptor["source_sha256"] != factory.source_digest():
        raise ValueError("Workflow factory source differs from its registered MPDB descriptor")
    if [row["id"] for row in workflow["cases"]] != descriptor["case_ids"]:
        raise ValueError("Workflow factory case inventory differs from MPDB")
    reference_route = workflow["target"].get("route_profile") if factory.FACTORY_ID == "media_input" else None
    route = get_model_route_profile(config, model, provider, route_profile=payload.get("route_profile") or reference_route or None)
    if reference_route and route != reference_route:
        raise ValueError("Selected media workflow route differs from its exact reference")
    workflow["target"]["route_profile"] = route
    workflow["target"]["test_binding_id"] = resolved["test_binding_id"]
    timeout = payload.get("timeout_sec")
    if timeout not in (None, ""):
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("Workflow timeout must be an integer from 1 to 3600 seconds")
        workflow["limits"]["request_timeout_seconds"] = (min(timeout, workflow["limits"]["request_timeout_seconds"])
            if factory.FACTORY_ID in {"aws_bedrock_mantle_json", "deepseek_beta_prefix"} else timeout)
    selected = _selection(workflow, payload)
    runs = _requested_runs(payload)
    plan = compile_plan(workflow, registry, selected_cases=selected, run_count=runs)
    # Bounds describe the selected closure, not the unselected full factory.
    workflow["limits"]["max_requests"] = max(1, sum(step.get("request_cap", 1) for step in plan["ordered_steps"]))
    cleanup_cap = sum(step.get("cleanup_request_cap", 0) for step in plan["ordered_steps"])
    workflow["limits"]["cleanup_max_requests"] = cleanup_cap
    workflow["limits"]["cleanup_deadline_seconds"] = max(15, cleanup_cap * 15)
    workflow["limits"]["deadline_seconds"] = workflow["limits"]["max_requests"] * workflow["limits"]["request_timeout_seconds"]
    plan = compile_plan(workflow, registry, selected_cases=selected, run_count=runs)
    expected = payload.get("plan_digest")
    if expected is not None and expected != plan["plan_digest"]:
        raise ValueError("The selected plan changed since preview")
    snapshot = make_workflow_snapshot(resolved, catalog.database_info(), plan)
    result = {"plan": plan, "plan_digest": plan["plan_digest"], "target": copy.deepcopy(plan["target"]),
              "selected_cases": plan["selected_cases"], "ordered_steps": plan["ordered_steps"],
              "request_cap": plan["limits"]["max_requests"] * runs,
              "cleanup_request_cap": cleanup_cap * runs, "resolved_workflow": resolved,
              "test_workflow_snapshot": snapshot,
              "preconditions": [{"case_id": row["id"], "requirements": row.get("preconditions", [])}
                                for row in workflow["cases"] if row["id"] in plan["selected_cases"] and row.get("preconditions")]}
    result["job_spec"] = build_workflow_job_spec(config, result, payload)
    return result


def _preview_parameter_plan(config: dict, payload: dict, *, provider: str, model: str) -> dict:
    """Freeze existing text bindings with their original identity/token contract."""
    from scripts import param_test as domain
    from lib.config import get_model_family, get_timeout_sec, get_model_api_form, get_model_api_forms
    from lib import model_profile_catalog as catalog_api
    from lib.job_spec import make_job_spec, build_result_validation_contract
    from lib.parameter_job_controls import make_parameter_execution
    from .adapters.legacy_parameter import prepare_legacy_parameter_plan
    runtime = copy.deepcopy(config)
    runs = _requested_runs(payload)
    mode = str(payload.get("tool_validation_mode") or "auto")
    runtime["_parameter_execution_controls"] = make_parameter_execution(runs, mode)
    timeout = payload.get("timeout_sec", get_timeout_sec(runtime))
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("Workflow timeout must be an integer from 1 to 3600 seconds")
    runtime.setdefault("api", {})["timeout_sec"] = timeout
    family = get_model_family(runtime, model, provider)
    route = domain.get_model_route_profile(runtime, model, provider, route_profile=payload.get("route_profile") or None)
    resolve_api = getattr(catalog_api, "resolve_parameter_test_api_form", None)
    api_form = (resolve_api(runtime, provider, model, family, route, explicit_api_form=payload.get("api_form") or None)
        if resolve_api else get_model_api_form(runtime, model, provider, route_profile=route, api_form=payload.get("api_form") or None))
    fixed_selected = bool(payload.get("parameter_suite")) or api_form == "deepseek_beta_chat_prefix"
    binding = domain.resolve_runtime_profile_binding(runtime, provider, model, family, route, api_form, modality="text")
    catalog_api.require_official_reference_binding(binding)
    canonical = str(payload.get("reference_contract_id") or "").strip()
    legacy = str(payload.get("reference_source") or "").strip()
    if canonical and legacy and canonical != legacy:
        raise ValueError("reference_contract_id and deprecated reference_source alias must be equal")
    reference_source = canonical or legacy or domain.default_reference_source_for_model(
        runtime, family, model, provider, api_form=api_form, route_profile=route)
    reference_family = domain.family_for_reference(reference_source)
    if reference_family != family:
        raise ValueError("Selected reference contract belongs to another model family")
    for field in ("source_id", "profile_id", "interface_id"):
        if payload.get(field) not in (None, "", binding[field]):
            raise ValueError("Selected reference identity conflicts with the runtime binding: " + field)
    parameter_config = domain.resolve_runtime_parameter_config(runtime, provider, model, family, route, api_form,
                                                              modality="text", contract_id=reference_source)
    all_profiles = list(parameter_config.get("test_cases") or [])
    if not all_profiles:
        raise ValueError("The enabled parameter binding has no test cases")
    if payload.get("suite", "full") != "full":
        raise ValueError("Text parameter subsets require exact case IDs")
    selectors = payload.get("cases")
    if not fixed_selected and selectors is not None and (not isinstance(selectors, list) or not selectors
            or any(not isinstance(value, str) or value not in all_profiles for value in selectors)
            or len(set(selectors)) != len(selectors)):
        raise ValueError("cases must select unique IDs from the enabled parameter binding")
    profiles = all_profiles if fixed_selected or selectors is None else [value for value in all_profiles if value in selectors]
    configured_form = get_model_api_forms(runtime, model, provider, route_profile=route)[api_form]
    database = copy.deepcopy(parameter_config.get("model_profile_database") or catalog_api.database_snapshot(binding))
    capability = domain.capability_profile_snapshot("text", family, model, profiles,
        reference_source=reference_source, api_form=api_form, route_profile=route, provider_override=configured_form,
        model_profile_database=database)
    if reference_source not in (capability.get("allowed_reference_sources") or []):
        raise ValueError("The selected contract is outside this model's enabled reference suite")
    if any(capability.get(field) is not True for field in ("known_model", "known_api_profile", "route_profile_known")):
        raise ValueError("The parameter target has no registered model/API/route profile")
    domain._require_parameter_matrix_enabled(capability, family, model)
    capability["model_profile_database"] = copy.deepcopy(database)
    capability["runtime_parameter_config"] = {key: copy.deepcopy(parameter_config.get(key)) for key in (
        "source_id", "profile_id", "interface_id", "test_binding_id", "contract_id",
        "parameter_test_binding_id", "execution_target", "execution_target_boundary")}
    if fixed_selected:
        from .fixed_service import preview_fixed_parameter_plan
        return preview_fixed_parameter_plan(runtime, payload, model_profile_database=database,
            model_capability_profile=capability, provider=provider, model=model, model_family=family,
            api_form=api_form, route_profile=route, reference_contract_id=reference_source)
    plan, _ = prepare_legacy_parameter_plan(runtime, provider, model, family, reference_source, reference_family,
        runs=runs, profiles=profiles, capability_profile=capability, sampling_seed=payload.get("plan_seed"),
        route_profile=route)
    if payload.get("plan_digest") is not None and payload["plan_digest"] != plan["plan_digest"]:
        raise ValueError("The selected parameter plan changed since preview")
    job = make_job_spec(job_type="param_test", provider=provider, model=model, model_family=family,
        api_form=api_form, route_profile=route, model_profile_id=capability.get("model_api_profile_id"),
        source_id=binding["source_id"], profile_id=binding["profile_id"], interface_id=binding["interface_id"],
        test_binding_id=database["test_binding_id"], reference_contract_id=reference_source,
        model_profile_database=database,
        transport=capability.get("transport"), workload="compatibility_profiles", request_mode="fixed",
        target_rpm=0.0, target_tpm=0.0, model_capability_profile=capability,
        result_contract=build_result_validation_contract(runtime), param_test_runs=runs,
        tool_validation_mode=mode, execution_plan=plan)
    job["timeout_sec"] = timeout
    return {"plan": plan, "plan_digest": plan["plan_digest"], "target": copy.deepcopy(plan["target"]),
        "plan_seed": plan["definition"]["factory_arguments"]["sampling_seed"],
        "selected_cases": plan["selected_cases"], "ordered_steps": plan["ordered_steps"],
        "request_cap": plan["limits"]["max_requests"] * runs,
        "cleanup_request_cap": plan["limits"]["cleanup_max_requests"] * runs,
        "preconditions": [], "job_spec": job}


def _preview_image_plan(config: dict, payload: dict) -> dict:
    from lib.job_spec import make_job_spec, build_result_validation_contract
    from .adapters.image import prepare_image_plan
    providers = list_image_providers(config)
    provider = str(payload.get("provider") or (providers[0]["name"] if providers else ""))
    if not provider:
        raise ValueError("No image provider is configured")
    provider = str(get_image_provider_config(config, provider).get("name") or get_provider_config(config, provider)["name"])
    model = str(payload.get("model") or get_image_provider_config(config, provider).get("default") or "")
    plan, _ = prepare_image_plan(config, {**payload, "provider": provider, "model": model, "run_count": _requested_runs(payload)}, provider=provider, model=model)
    expected = payload.get("plan_digest")
    if expected is not None and expected != plan["plan_digest"]:
        raise ValueError("The selected image plan changed since preview")
    image_plan = copy.deepcopy(plan["definition"]["image_plan"])
    database = image_plan["model_profile_database"]
    model_config = get_image_model_config(config, provider, model, route_profile=image_plan.get("route_profile"), api_form=image_plan["api_form"])
    target = plan["target"]
    job = make_job_spec(
        job_type="image_param_test", provider=provider, model=model, model_family=model_config["family"],
        api_form=image_plan["api_form"], route_profile=image_plan["route_profile"],
        model_profile_id=target["interface_id"], source_id=target["source_id"], profile_id=target["profile_id"],
        interface_id=target["interface_id"], test_binding_id=database["test_binding_id"],
        reference_contract_id=target["contract_id"], reference_source=target["contract_id"],
        model_profile_database=database, transport=target["execution_target"]["transport_adapter_id"],
        workload="image_param", request_mode="fixed", target_rpm=0.0, target_tpm=0.0,
        image_plan=image_plan, model_capability_profile=image_plan.get("model_capability_profile"),
        result_contract=build_result_validation_contract(config, image_plan=image_plan),
        param_test_runs=plan["run_count"], execution_plan=plan)
    return {"plan": plan, "plan_digest": plan["plan_digest"], "target": copy.deepcopy(target),
            "selected_cases": plan["selected_cases"], "ordered_steps": plan["ordered_steps"],
            "request_cap": plan["limits"]["max_requests"] * plan["run_count"],
            "cleanup_request_cap": plan["limits"]["cleanup_max_requests"] * plan["run_count"],
            "preconditions": [], "job_spec": job, "image_plan": image_plan}


def image_cli_arguments(image_plan: dict, output_dir, *, api_key_env: str | None = None) -> list[str]:
    """Both command launchers project the same reviewed image selection."""
    arguments = ["--output-dir", str(output_dir)]
    fields = {"endpoint": "base-url", "provider": "provider", "model": "model", "family": "family",
              "route_profile": "route-profile", "api_form": "api-form", "transport": "transport",
              "auth_mode": "auth-mode", "suite": "suite", "timeout_sec": "timeout"}
    for field, flag in fields.items():
        arguments.extend(["--" + flag, str(image_plan[field])])
    if api_key_env is None:
        arguments.extend(["--credential-provider", image_plan["provider"]])
    else:
        arguments.extend(["--api-key-env", api_key_env])
    for field in ("quality", "output_format", "api_version"):
        if image_plan.get(field) is not None:
            arguments.extend(["--" + field.replace("_", "-"), str(image_plan[field])])
    for field in ("include_2k", "include_4k", "no_negative", "no_cross_control"):
        if image_plan.get(field):
            arguments.append("--" + field.replace("_", "-"))
    if image_plan.get("visual_forensics") is False:
        arguments.append("--no-visual-forensics")
    for case in image_plan.get("cases", []):
        arguments.extend(["--case", case])
    return arguments


def build_workflow_job_spec(config: dict, preview: dict, payload: dict) -> dict:
    plan = preview["plan"]
    identity = validate_workflow_snapshot(preview["test_workflow_snapshot"])
    expected_type = "image_param_test" if identity["modality"] == "image" else "param_test"
    if payload.get("type", expected_type) != expected_type:
        raise ValueError("Workflow job type conflicts with its reference modality")
    job = {key: identity[key] for key in ("provider", "model", "model_family", "modality", "route_profile", "api_form",
                                          "transport", "source_id", "profile_id", "interface_id", "test_binding_id", "reference_contract_id")}
    job.update(type=expected_type, workload="compatibility_profiles", request_mode="fixed",
               model_profile_id=job["interface_id"], reference_source=job["reference_contract_id"],
               test_workflow_snapshot=copy.deepcopy(preview["test_workflow_snapshot"]),
               provider_label=str(get_provider_config(config, job["provider"]).get("label") or job["provider"]),
               timeout_sec=plan["limits"]["request_timeout_seconds"], param_test_runs=plan["run_count"],
               result_contract={"schema_version": 1, "proof_scope": "workflow_execution_and_domain_validators"})
    return freeze_workflow_job(job, plan, str(payload.get("tool_validation_mode") or "auto"))
