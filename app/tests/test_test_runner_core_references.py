from __future__ import annotations

import copy

import pytest

from lib.test_runner import HandlerRegistry, PlanValidationError, compile_plan, execute_plan


def echo(context, inputs):
    return {"status": "passed", "accepted": True, "value": inputs}


def test_recursive_tool_schemas_and_wrong_type_refs_are_literal_while_result_refs_resolve(tmp_path):
    schema = {"$defs": {"node": {"type": "object", "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/node"}}}}},
              "$ref": "#/$defs/node", "$dynamicRef": "#node", "negative_variants": [{"$ref": 42}, {"$ref": None},
              {"$ref": {"step": "raw-schema-wrong-type"}}, {"$ref": ["wrong-type"]}]}
    original = copy.deepcopy(schema)
    registry = HandlerRegistry()
    registry.register("echo", echo)
    workflow = {"workflow_schema_version": 1, "id": "json-schema", "target": {"provider": "offline"}, "cases": [{"id": "c"}],
                "steps": [{"id": "seed", "case_id": "c", "handler": "echo", "inputs": {"signature": "opaque"}},
                          {"id": "schema", "case_id": "c", "handler": "echo", "inputs": {
                              "tools": [{"type": "function", "function": {"name": "tree", "parameters": schema}}],
                              "structured_output": {"schema": schema},
                              "prior": {"$result_ref": {"step": "seed", "path": ["value"], "type": "object"}}}}]}
    plan = compile_plan(workflow, registry)
    assert plan["ordered_steps"][1]["depends_on"] == ["seed"]
    result = execute_plan(plan, registry, lambda *a, **kw: pytest.fail("No HTTP expected"), evidence_dir=tmp_path)
    value = result["runs"][0]["steps"]["schema"]["value"]
    assert value["tools"][0]["function"]["parameters"] == original
    assert value["structured_output"]["schema"] == original
    assert value["prior"] == {"signature": "opaque"}


def test_explicit_literal_escape_preserves_a_body_that_resembles_a_workflow_reference(tmp_path):
    raw = {"$result_ref": {"step": "does-not-exist", "path": [], "type": "object"}}
    registry = HandlerRegistry();registry.register("echo", echo)
    workflow = {"workflow_schema_version": 1, "id": "literal", "target": {"provider": "offline"}, "cases": [{"id": "c"}],
                "steps": [{"id": "s", "case_id": "c", "handler": "echo", "inputs": {"body": {"$result_literal": raw}}}]}
    result = execute_plan(compile_plan(workflow, registry), registry, lambda *a, **kw: None, evidence_dir=tmp_path)
    assert result["runs"][0]["steps"]["s"]["value"]["body"] == raw


@pytest.mark.parametrize("value", ["step", {"step": "s", "type": []}, {"step": "s", "path": [True], "type": "object"}, {"path": [], "type": "object"}])
def test_malformed_namespaced_result_reference_fails_preflight(value):
    registry = HandlerRegistry();registry.register("echo", echo)
    workflow = {"workflow_schema_version": 1, "id": "invalid", "target": {"provider": "offline"}, "cases": [{"id": "c"}],
                "steps": [{"id": "s", "case_id": "c", "handler": "echo", "inputs": {"x": {"$result_ref": value}}}]}
    with pytest.raises(PlanValidationError):
        compile_plan(workflow, registry)
