"""Views of the explicitly selected source-authored fixed suite."""
from .deepseek_fim_reference import API_FORM, CONTRACT_ID, SUITE_ID, build_fim_plan
from . import anthropic_prefill_reference as prefill
from . import anthropic_cache_reference as cache


def fixed_suite_descriptor(suite_id):
    if suite_id == SUITE_ID:
        from .deepseek_fim_reference import ENDPOINT, TRANSPORT, CASE_IDS
        return {"id": SUITE_ID, "provider": "deepseek_official", "model": "deepseek-v4-pro",
            "api_form": API_FORM, "contract_id": CONTRACT_ID, "transport": TRANSPORT, "endpoint": ENDPOINT,
            "request_count": 8, "case_ids": list(CASE_IDS), "observation_key": "bounded_fim_observations",
            "label": "FIM suffix / echo / stop 固定对照", "runs": 1}
    if suite_id in prefill.SUITE_IDS:
        model = prefill.MODELS[prefill.SUITE_IDS.index(suite_id)]
        return {"id": suite_id, "provider": "anthropic_official", "model": model, "api_form": prefill.API_FORM,
            "contract_id": prefill.CONTRACT_ID, "transport": prefill.TRANSPORT, "endpoint": prefill.ENDPOINT,
            "request_count": 3, "case_ids": [suite_id + "/" + name for name in prefill.LABELS],
            "observation_key": "bounded_prefill_observations", "label": "Claude 4.5 prefill / stop / 类型固定对照", "runs": 1}
    if suite_id in cache.SUITE_IDS:
        model = cache.MODELS[cache.SUITE_IDS.index(suite_id)]
        return {"id": suite_id, "provider": "anthropic_official", "model": model, "api_form": cache.API_FORM,
            "contract_id": cache.CONTRACT_ID, "transport": cache.TRANSPORT, "endpoint": cache.ENDPOINT,
            "request_count": 3, "count_request_count": 0, "generation_request_count": 3,
            "case_ids": [suite_id + "/" + name for name in cache.CURRENT_LABELS], "observation_key": "bounded_cache_observations",
            "label": "Claude 缓存固定对照（冷 / 重复 / 负例）", "runs": 1}
    raise ValueError("Unknown fixed parameter suite")


def candidate_fixed_suites(provider, model, api_form, contract_id):
    return [item for suite in (SUITE_ID, *prefill.SUITE_IDS, *cache.SUITE_IDS)
            if (item := fixed_suite_descriptor(suite))["provider"] == provider and item["model"] == model
            and item["api_form"] == api_form and item["contract_id"] == contract_id]


def build_fixed_plan(snapshot, *, suite_id, endpoint=None, runs=1, job_type="param_test", frozen_plan=None, create_runtime_nonce=False):
    descriptor = fixed_suite_descriptor(suite_id)
    if suite_id in cache.SUITE_IDS:
        return cache.build_cache_plan(snapshot, suite_id=suite_id, endpoint=endpoint or descriptor["endpoint"],
            runs=runs, job_type=job_type, frozen_plan=frozen_plan, create_runtime_nonce=create_runtime_nonce)
    if frozen_plan is not None or create_runtime_nonce:
        raise ValueError("Only cache suites accept a job-owned nonce plan")
    build = build_fim_plan if suite_id == SUITE_ID else prefill.build_prefill_plan
    return build(snapshot, suite_id=suite_id, endpoint=endpoint or descriptor["endpoint"], runs=runs, job_type=job_type)


def fixed_spec_payload(plan):
    if type(plan) in (cache.CacheSuiteDescription, cache.CachePlan):
        snapshot = plan.snapshot
        suite = cache._suite(snapshot, plan.suite_id)
        current = plan.schema_version == 2
        caps = {**snapshot["reference_contract"].get("parameter_capabilities", {}),
                **snapshot["interface"].get("parameter_capabilities", {})}
        case_ids = [r.case_id for r in plan.requests]
        rows = [{"parameter": name, "official": capability.get("state", "unknown"), "local": capability.get("state", "unknown"),
            "coverage": ("冷 / 重复 / 负例命中预期对照" if current else "历史计数前置与冷 / 重复 / 负例固定对照") if name == "cache_control" else "本套件未覆盖",
            "coverage_mode": "profiles" if name == "cache_control" else "not_tested",
            "test_profiles": case_ids if name == "cache_control" else []} for name, capability in caps.items()]
        return {"reference_source": cache.CONTRACT_ID, "reference_contract_id": cache.CONTRACT_ID, "source_id": "anthropic",
            "label": "Claude 自动缓存：冷 / 重复 / 负例" if current else "历史 Claude 自动缓存：2 次官方输入估算 + 冷 / 重复 / 负例", "model_family": "claude",
            "api_form": cache.API_FORM, "route_profile": "vendor_direct", "parameter_suite": plan.suite_id,
            "test_profiles": case_ids, "params": rows, "comparison": rows, "param_count": len(rows),
            "tested_params": [r["parameter"] for r in rows if r["test_profiles"]],
            "untested_params": [r["parameter"] for r in rows if not r["test_profiles"]],
            "fixed_request_count": plan.request_cap, "count_request_count": 0 if current else 2, "generation_request_count": 3, "param_test_runs": 1,
            "identity_probe_requests": 0, "full_parameter_matrix_verified": False, "token_exact_proof": False,
            "count_precision": "not_requested" if current else "official_estimate", "minimum_prefix_tokens": None if current else suite["minimum_prefix_tokens"],
            "plan_schema_version": plan.schema_version, "official_numeric_reference_required": not current,
            "nonce_generation_time": "job_creation_only", "report_retention": "P1M", "snapshot_digest": snapshot["snapshot_digest"]}
    if type(plan) is prefill.PrefillPlan:
        snapshot = plan.snapshot
        caps = {**snapshot["reference_contract"].get("parameter_capabilities", {}),
                **snapshot["interface"].get("parameter_capabilities", {})}
        requests = plan.requests
        rows = []
        for name, capability in caps.items():
            selected = [r.case_id for r in requests if r.target_parameter == name or r.target_parameter.startswith(name + "[")]
            state = capability.get("state", "unknown")
            rows.append({"parameter": name, "official": state, "local": state,
                "coverage": "固定对照：" + ", ".join(selected) if selected else "本套件未覆盖",
                "coverage_mode": "profiles" if selected else "not_tested", "test_profiles": selected})
        return {"reference_source": prefill.CONTRACT_ID, "reference_contract_id": prefill.CONTRACT_ID,
            "source_id": "anthropic", "label": "Claude 4.5：prefill / stop / 类型固定对照", "model_family": "claude",
            "api_form": prefill.API_FORM, "route_profile": "vendor_direct", "parameter_suite": plan.suite_id,
            "test_profiles": [r.case_id for r in plan.requests], "params": rows, "comparison": rows, "param_count": len(rows),
            "tested_params": [r["parameter"] for r in rows if r["test_profiles"]],
            "untested_params": [r["parameter"] for r in rows if not r["test_profiles"]],
            "fixed_request_count": 3, "param_test_runs": 1, "identity_probe_requests": 0,
            "full_parameter_matrix_verified": False, "snapshot_digest": snapshot["snapshot_digest"]}
    return fim_spec_payload(plan)


def fim_spec_payload(plan):
    snapshot = plan.snapshot
    contract, interface = snapshot["reference_contract"], snapshot["interface"]
    caps = {**contract.get("parameter_capabilities", {}), **interface.get("parameter_capabilities", {})}
    requests = plan.requests
    rows = []
    for name, capability in caps.items():
        selected = [r.case_id for r in requests if name in r.target_parameter.split("+")
                    or r.target_parameter.startswith(name + ".")]
        state = capability.get("state", "unknown")
        rows.append({"parameter": name, "official": state, "local": state,
            "coverage": "固定成对对照：" + ", ".join(selected) if selected else "本套件未覆盖",
            "coverage_mode": "profiles" if selected else "not_tested", "test_profiles": selected})
    return {"reference_source": CONTRACT_ID, "reference_contract_id": CONTRACT_ID, "source_id": "deepseek",
        "label": "DeepSeek Pro 0813 FIM：suffix / echo / stop 固定对照", "model_family": "deepseek",
        "api_form": API_FORM, "route_profile": "vendor_direct", "parameter_suite": SUITE_ID,
        "test_profiles": [r.case_id for r in requests], "params": rows, "comparison": rows,
        "param_count": len(rows), "tested_params": [r["parameter"] for r in rows if r["test_profiles"]],
        "untested_params": [r["parameter"] for r in rows if not r["test_profiles"]],
        "fixed_request_count": 8, "param_test_runs": 1, "identity_probe_requests": 0,
        "full_parameter_matrix_verified": False, "snapshot_digest": snapshot["snapshot_digest"]}


def validate_selected_fim_suite(snapshot, suite_id, *, endpoint, runs=1, job_type="param_test"):
    return build_fim_plan(snapshot, suite_id=suite_id, endpoint=endpoint, runs=runs, job_type=job_type)
