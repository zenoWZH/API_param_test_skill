"""Optional media workflows must never silently replace parameter defaults."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("prefix", [""])
def test_capability_descriptors_preserve_defaults_and_hide_empty_media(prefix, monkeypatch):
    from lib import model_profile_catalog

    def binding(name, factory):
        return {"test_binding_id": "workflow/" + name, "workflow_id": name,
                "source_id": "official", "profile_id": "profile", "interface_id": "interface",
                "contract_id": "contract", "workflow": {"factory_id": factory},
                "execution_target": {"provider_id": "provider", "request_model_id": "model",
                                     "api_form": "openai_chat_completions", "transport_adapter_id": "chat_completions"}}

    bindings = [binding("media", "media_input"), binding("empty", "media_input"), binding("existing", "fable_research")]
    catalog = SimpleNamespace(list_workflows=lambda **kwargs: bindings)
    monkeypatch.setattr(model_profile_catalog, "get_model_profile_catalog", lambda: catalog)
    # Execute the production function in a small namespace so root and App
    # behavior can be checked without replacing either app's import paths.
    tree = ast.parse((ROOT / prefix / "scripts/web_console.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_add_workflow_capabilities")
    cases = [{"id": "image_red", "name": "Image red", "phase": "input", "group": "image"},
             {"id": "audio_green", "name": "Spoken sentence", "phase": "input", "group": "audio"}]
    calls = []

    def preview(config, payload):
        calls.append(payload)
        selected = [] if payload["workflow_binding_id"] == "workflow/empty" else [row["id"] for row in cases]
        return {"plan": {"selected_cases": selected, "definition": {"cases": cases}},
                "request_cap": len(selected), "cleanup_request_cap": 0}

    namespace = {"Any": Any, "copy": copy, "_preview_workflow": preview,
                 "get_model_route_profiles": lambda *args: ["vendor_direct"],
                 "get_model_api_forms": lambda *args, **kwargs: {"openai_chat_completions": {}},
                 "get_model_family": lambda *args: "family"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / prefix / "scripts/web_console.py"), "exec"), namespace)
    config = {"providers": {"provider": {"models": {"candidates": ["model"]}}}}
    ordinary = {"profile_status": "registered", "parameter_test_enabled": True,
                "pressure_test_runnable": False, "default_route_profile": "vendor_direct", "routes": {
                    "vendor_direct": {"default_api_form": "openai_chat_completions", "api_forms": {
                        "openai_chat_completions": {"profile_status": "registered", "parameter_test_enabled": True,
                                                    "pressure_test_runnable": False}}}}}
    registry = {"text": {"provider": {"model": copy.deepcopy(ordinary)}}, "image": {}}
    # Media alone must not install workflow route/API defaults.
    bindings[:] = bindings[:2]
    result = namespace["_add_workflow_capabilities"](config, registry)
    model = result["text"]["provider"]["model"]
    route = model["routes"]["vendor_direct"]
    leaf = route["api_forms"]["openai_chat_completions"]
    assert "workflow_default_route_profile" not in model
    assert "workflow_default_api_form" not in route
    assert leaf["parameter_test_enabled"] is True and leaf["pressure_test_runnable"] is False
    assert len(result["workflows"]) == 1
    media = result["workflows"][0]
    assert media["optional"] is True and media["factory_id"] == "media_input"
    assert media["label"] == "图片/视频/音频输入"
    assert media["cases"][1]["group"] == "audio"
    # An existing default suite retains its independent defaults and identity.
    bindings.append(binding("existing", "fable_research"))
    result = namespace["_add_workflow_capabilities"](config, {"text": {}, "image": {}})
    assert result["workflows"][-1]["optional"] is False
    assert result["text"]["provider"]["model"]["workflow_default_route_profile"] == "vendor_direct"
    assert all(call["workflow_binding_id"] in {"workflow/media", "workflow/empty", "workflow/existing"} for call in calls)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for browser-independent UI verification")
@pytest.mark.parametrize("prefix", [""])
def test_explicit_media_selection_preserves_normal_and_existing_defaults(prefix):
    source = (ROOT / prefix / "scripts/static/web_console.js").read_text().rsplit("\nbindEvents();", 1)[0]
    probe = r'''
const assert = require("assert");
const nodes = {};
globalThis.document = {getElementById(id) {
  return nodes[id] ||= {value: "", disabled: false, hidden: false, innerHTML: "", textContent: "", className: "", style: {}, classList: {toggle(){}}};
}, querySelectorAll() {return [];}};
const media = {factory_id: "media_input", optional: true, label: "图片/视频/音频输入", workflow_binding_id: "workflow/media",
  workflow_id: "media-suite", reference_contract_id: "official-contract", source_id: "official", profile_id: "profile", interface_id: "interface",
  case_count: 3, cases: [{id: "image_red", name: "red <image>", group: "image"}, {id: "audio_green", name: "Spoken phrase", group: "audio"}, {id: "video_order", name: "Temporal order", group: "video"}]};
const existing = {...media, factory_id: "fable_research", optional: false, label: "Fable 完整参数矩阵",
  workflow_binding_id: "workflow/fable", workflow_id: "fable-suite"};
const leaf = {profile_status: "registered", parameter_test_enabled: true, contract_id: "official-contract", contract_ids: ["official-contract"],
  workflow_available: true, test_workflows: [media]};
appState.config = {defaults: {param_test_runs: 1}, reference_contracts: [{id: "official-contract"}], providers: [],
  model_capabilities: {official: {model: {routes: {vendor_direct: {api_forms: {openai_chat_completions: leaf}}}}}}};
const form = appState.formsByTab.param;
Object.assign(form, {provider: "official", model: "model", routeProfile: "vendor_direct", apiForm: "openai_chat_completions", referenceContractId: "official-contract"});
assert.equal(selectedParamWorkflow(), null, "Optional media must not become the ordinary default");
syncParamWorkflow();
assert.equal($("paramWorkflow").value, "__ordinary__");
assert($("paramWorkflow").innerHTML.includes("图片/视频/音频输入"));
assert.equal(workflowRequestPayload().workflow_binding_id, undefined);
// Even when media is first, preserve the old nonoptional workflow default.
leaf.test_workflows = [media, existing];
assert.equal(selectedParamWorkflow().workflow_binding_id, "workflow/fable");
syncParamWorkflow();
assert.equal($("paramWorkflow").value, "workflow/fable");
leaf.test_workflows = [media];
form.workflowBindingId = "";
const realLoadWorkflowSpecs = loadWorkflowParamSpecs;
renderProviderStatus = () => {};
renderBusyState = () => {};
renderParamResults = () => {};
renderParamRunHint = () => {};
loadParamSpecs = async () => {syncParamWorkflow(); renderToolValidationMode();};
refreshWorkflowPreview = async () => null;
(async () => {
  await changeParamWorkflow("workflow/media");
  assert.equal(selectedParamWorkflow().factory_id, "media_input");
  assert.equal($("paramWorkflow").value, "workflow/media");
  assert($("paramWorkflowCaseOptions").innerHTML.includes("音频输入"));
  assert($("paramWorkflowCaseOptions").innerHTML.includes("视频输入"));
  assert($("paramWorkflowCaseOptions").innerHTML.includes("&lt;image&gt;"));
  assert($("toolValidationHint").textContent.includes("无工具调用"));
  form.workflowCases = "audio_green";
  const payload = workflowRequestPayload();
  assert.equal(payload.workflow_binding_id, "workflow/media");
  assert.equal(payload.source_id, "official");
  assert.deepEqual(payload.cases, ["audio_green"]);
  await realLoadWorkflowSpecs();
  assert($("paramSpecs").innerHTML.includes("图片输入"));
  assert($("paramSpecs").innerHTML.includes("音频输入"));
  assert($("paramSpecs").innerHTML.includes("视频输入"));
  await changeParamWorkflow("__ordinary__");
  assert.equal(selectedParamWorkflow(), null);
  assert.equal(form.workflowCases, "");
  assert.equal(workflowRequestPayload().workflow_binding_id, undefined);
  assert.equal($("referenceContract").disabled, false);
  assert.equal($("toolValidationMode").disabled, false);
  const mediaJob = {type: "param_test", provider: form.provider, model: form.model,
    route_profile: form.routeProfile, api_form: form.apiForm, reference_contract_id: form.referenceContractId,
    tool_validation_mode: "auto", job_spec: {execution_plan: {definition: {factory: {factory_id: "media_input"}}}}};
  assert.equal(matchesParamSelection(mediaJob), false, "Media history must not appear as an ordinary parameter result");
  // An unsupported model exposes no runnable media choice.
  leaf.workflow_available = false; leaf.test_workflows = [];
  syncParamWorkflow();
  assert(!$("paramWorkflow").innerHTML.includes("workflow/media"));
  // Return to a source-owned default suite; unavailable generic matrices are disabled.
  leaf.workflow_available = true; leaf.test_workflows = [media, existing];
  leaf.profile_status = "invalid"; leaf.parameter_test_enabled = false;
  form.workflowBindingId = "";
  syncParamWorkflow();
  assert.equal($("paramWorkflow").value, "workflow/fable");
  assert($("paramWorkflow").innerHTML.includes('value="__ordinary__" disabled'));
  leaf.profile_status = "registered"; leaf.parameter_test_enabled = true;
  syncParamWorkflow();
  assert($("paramWorkflow").innerHTML.includes('value="__ordinary__" disabled'), "Do not offer a generic override the server does not implement");
  process.stdout.write("ok");
})().catch((error) => {console.error(error); process.exit(1);});
'''
    completed = subprocess.run([shutil.which("node"), "-"], input=source + "\n" + probe,
                               text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ok"


def test_flask_preview_keeps_explicit_media_identity_without_dispatch(monkeypatch):
    from scripts import web_console as web

    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    config = {"offline": True}
    monkeypatch.setattr(web, "load_config", lambda: config)
    preview = Mock(return_value={"plan_digest": "offline", "selected_cases": ["image_red"], "request_cap": 1})
    monkeypatch.setattr(web, "_preview_workflow", preview)
    dispatch = Mock(side_effect=AssertionError("preview dispatched a provider request"))
    import requests
    monkeypatch.setattr(requests.Session, "request", dispatch)
    payload = {"type": "param_test", "provider": "official", "model": "model",
               "workflow_binding_id": "workflow/media", "api_form": "openai_chat_completions", "cases": ["image_red"]}
    response = web.app.test_client().post("/api/test-plan/preview", json=payload)
    assert response.status_code == 200 and response.json["request_cap"] == 1
    preview.assert_called_once_with(config, payload)
    dispatch.assert_not_called()


@pytest.mark.parametrize("prefix", [""])
def test_template_has_explicit_matrix_selector(prefix):
    template = (ROOT / prefix / "scripts/templates/web_console.html").read_text()
    assert template.count('id="paramWorkflow"') == 1
    assert 'value="__ordinary__">普通参数测试' in template
