"""Immutable workflow reference snapshots, separate from old executable policies."""
from __future__ import annotations

import copy

from .common import digest_json
from .compiler import validate_plan


def make_workflow_snapshot(resolved: dict, catalog_info: dict, plan: dict) -> dict:
    binding = copy.deepcopy(resolved)
    reference = binding.pop("reference")
    result = {"workflow_snapshot_schema_version": 1, "catalog": copy.deepcopy(catalog_info),
              "binding": binding, "reference": reference, "target": copy.deepcopy(plan["target"]),
              "plan_digest": plan["plan_digest"]}
    result["snapshot_digest"] = digest_json(result)
    validate_workflow_snapshot(result)
    return result


def validate_workflow_snapshot(snapshot: dict, job: dict | None = None) -> dict:
    """Validate only immutable data; never query current providers or catalog rows."""
    if not isinstance(snapshot, dict) or type(snapshot.get("workflow_snapshot_schema_version")) is not int or snapshot["workflow_snapshot_schema_version"] != 1:
        raise ValueError("Unsupported immutable workflow snapshot")
    frozen = copy.deepcopy(snapshot)
    digest = frozen.pop("snapshot_digest", None)
    if digest_json(frozen) != digest:
        raise ValueError("Workflow snapshot digest mismatch")
    binding, reference, target = (frozen.get(key) for key in ("binding", "reference", "target"))
    if not all(isinstance(row, dict) for row in (binding, reference, target)):
        raise ValueError("Workflow snapshot requires binding, reference and target")
    if binding.get("enabled") is not True:
        raise ValueError("Workflow snapshot references a disabled workflow")
    from lib import model_profile_catalog  # Establish installed/check-out package routing.
    from model_profile_db.compiler import _validate_workflow_binding
    try:
        profile, interface, contract = (reference[key] for key in ("profile", "interface", "contract"))
        binding_id = binding["test_binding_id"]
        payload = {"profiles": {binding["profile_id"]: profile}, "interfaces": {binding["interface_id"]: interface},
                   "contracts": {binding["contract_id"]: contract},
                   "test_bindings": {row["test_binding_id"]: row for row in interface.get("test_bindings", [])}}
        payload["test_bindings"][binding_id] = binding
        declared_binding = {key: value for key, value in binding.items() if key != "test_binding_id"}
        _validate_workflow_binding(binding_id, declared_binding, payload)
    except (KeyError, TypeError) as exc:
        raise ValueError("Incomplete workflow reference snapshot") from exc
    execution = binding["execution_target"]
    if target.get("execution_target") != execution:
        raise ValueError("Workflow snapshot execution target mismatch")
    for key in ("source_id", "profile_id", "interface_id", "contract_id"):
        if target.get(key) != binding[key]:
            raise ValueError("Workflow snapshot reference mismatch: " + key)
    if target.get("test_binding_id") not in (None, binding_id):
        raise ValueError("Workflow snapshot binding mismatch")
    identity = {"provider": execution["provider_id"], "model": execution["request_model_id"],
                "route_profile": target.get("route_profile", ""), "api_form": execution["api_form"],
                "transport": execution["transport_adapter_id"], "source_id": binding["source_id"],
                "profile_id": binding["profile_id"], "interface_id": binding["interface_id"],
                "test_binding_id": binding_id, "reference_contract_id": binding["contract_id"],
                "model_family": profile["family_id"], "modality": profile["modality"],
                "resolution_status": "snapshot", "legacy_unresolved": False, "workflow_snapshot": copy.deepcopy(snapshot)}
    if job is not None:
        plan = validate_plan(job.get("execution_plan"))
        if plan["plan_digest"] != snapshot["plan_digest"] or plan["target"] != target:
            raise ValueError("Workflow snapshot and execution plan disagree")
        if plan["workflow_id"] != binding["workflow_id"]:
            raise ValueError("Workflow snapshot and plan workflow identity disagree")
        descriptor = binding["workflow"]
        if "factory_id" in descriptor:
            factory = plan["definition"].get("factory", {})
            if any(factory.get(key) != descriptor[key] for key in ("factory_id", "version", "source_sha256")):
                raise ValueError("Workflow snapshot and plan factory disagree")
            if [row["id"] for row in plan["definition"]["cases"]] != descriptor["case_ids"]:
                raise ValueError("Workflow snapshot and plan case inventory disagree")
        for key, value in identity.items():
            if key in {"resolution_status", "legacy_unresolved", "workflow_snapshot"}:
                continue
            if key in job and job[key] != value:
                raise ValueError("Workflow snapshot conflicts with Job " + key)
        if job.get("schema_version") != 6:
            raise ValueError("Workflow snapshot requires JobSpec v6")
    return identity
