"""Exact workflow availability stays separate from native/pressure gates and UI previews freeze selection."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from lib.config import load_config
from lib.model_profile_catalog import get_model_profile_catalog
from scripts import web_console as web


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    import os
    for key in tuple(os.environ):
        if key.startswith("LOADTEST_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/workflow-ui-no-private.yaml")
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")


def test_exact_workflow_availability_does_not_enable_native_or_pressure(monkeypatch):
    config = load_config()
    provider, model = "anthropic_official", "claude-fable-5-1"
    config["providers"] = {provider: copy.deepcopy(config["providers"][provider])}
    config["providers"][provider]["models"]["candidates"] = [model]
    config["providers"][provider]["models"]["default"] = model
    config["providers"][provider].pop("image", None)
    import requests
    preview = Mock(wraps=web._preview_workflow)
    monkeypatch.setattr(web, "_preview_workflow", preview)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("availability sent HTTP")))
    registry = web._capability_registry_payload(config)
    capability = registry["text"][provider][model]
    assert capability["workflow_available"] is True
    assert capability.get("parameter_test_enabled") is not True
    assert capability.get("pressure_test_runnable") is not True
    leaf = capability["routes"]["vendor_direct"]["api_forms"]["anthropic_messages"]
    assert leaf["workflow_available"] is True and leaf["parameter_test_enabled"] is False
    assert leaf["pressure_test_runnable"] is False
    workflow = leaf["test_workflows"][0]
    assert workflow["source_id"] == "anthropic"
    assert workflow["reference_contract_id"] == "claude_fable_5_1_native_messages"
    assert workflow["case_count"] > 0 and workflow["default_runs"] == 1
    declared = {row["test_binding_id"] for row in get_model_profile_catalog().list_workflows(provider_id=provider, request_model_id=model)}
    inspected = {call.args[1]["workflow_binding_id"] for call in preview.call_args_list}
    assert workflow["workflow_binding_id"] in inspected and inspected <= declared
    assert workflow["optional"] is False
    assert all(row["optional"] for row in leaf["test_workflows"] if row["factory_id"] == "media_input")
    requests.Session.request.assert_not_called()
    before = get_model_profile_catalog().get_interface(workflow["interface_id"])
    assert before["enabled"] is False


def test_uncompilable_or_disabled_workflow_does_not_get_availability(monkeypatch):
    config = load_config()
    provider = "anthropic_official"
    config["providers"] = {provider: config["providers"][provider]}
    registry = {"text": {}, "image": {}}
    monkeypatch.setattr(web, "_preview_workflow", Mock(side_effect=ValueError("source digest drift")))
    result = web._add_workflow_capabilities(config, registry)
    assert result["workflows"] == [] and result["text"] == {}


def test_config_publishes_workflow_descriptors_and_full_image_default(monkeypatch):
    config = load_config()
    monkeypatch.setattr(web, "load_config", lambda: config)
    registry = {"text": {}, "image": {}, "workflows": [{"workflow_binding_id": "exact", "source_id": "anthropic"}]}
    monkeypatch.setattr(web, "_capability_registry_payload", lambda value: registry)
    response = web.app.test_client().get("/api/config")
    assert response.status_code == 200
    assert response.json["test_workflows"] == registry["workflows"]
    assert response.json["defaults"]["param_test_runs"] == 1
    assert response.json["image_defaults"]["suite"] == "full"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for browser-independent UI verification")
def test_workflow_js_binding_preview_selection_and_stale_digest():
    source = (Path(__file__).resolve().parents[1] / "scripts/static/web_console.js").read_text()
    source = source.rsplit("\nbindEvents();", 1)[0]
    probe = r'''
const assert = require("assert");
const nodes = {};
globalThis.document = {
  getElementById(id) {
    if (!nodes[id]) nodes[id] = {value: "", disabled: false, hidden: false, innerHTML: "", textContent: "", style: {}, className: "", classList: {toggle(){}}};
    return nodes[id];
  },
  querySelectorAll() { return []; },
};
const workflow = {workflow_binding_id: "workflow/exact-awsb", workflow_id: "exact-awsb", source_id: "aws_bedrock",
  profile_id: "aws-profile", interface_id: "aws-interface", reference_contract_id: "aws-contract", case_count: 2,
  cases: [{id: "seed", name: "Seed"}, {id: "dependent", name: "Dependent"}]};
const leaf = {profile_status: "invalid", parameter_test_enabled: false, pressure_test_runnable: false,
  workflow_available: true, test_workflows: [workflow]};
appState.config = {defaults: {param_test_runs: 1}, reference_contracts: [], image_providers: [],
 providers: [{name: "gateway", has_key: true, models: {default: "fable", candidates: ["fable"]}}],
 model_capabilities: {gateway: {fable: {profile_status: "invalid", parameter_test_enabled: false,
   workflow_default_route_profile: "gateway", routes: {gateway: {workflow_default_api_form: "anthropic_messages",
     api_forms: {anthropic_messages: leaf}}}}}}};
Object.assign(appState.formsByTab.param, {provider: "gateway", model: "fable", routeProfile: "gateway", apiForm: "anthropic_messages",
 referenceContractId: "wrong-anthropic-contract", paramTestRuns: 1});
syncParamWorkflow();
assert.equal(appState.formsByTab.param.referenceContractId, "aws-contract");
assert.equal(leaf.parameter_test_enabled, false);
assert.equal(leaf.pressure_test_runnable, false);
assert.equal(workflowRequestPayload().suite, "full");
assert.equal(workflowRequestPayload().param_test_runs, 1);
assert.equal(workflowRequestPayload().workflow_binding_id, "workflow/exact-awsb");
appState.formsByTab.param.workflowCases = "dependent";
appState.formsByTab.param.paramTestRuns = 2;
const plan = {run_count: 2, selected_cases: ["seed", "dependent"], ordered_steps: [{id: "seed"}, {id: "dependent"}],
 definition: {cases: workflow.cases}, plan_digest: "reviewed-digest"};
const preview = {plan, plan_digest: "reviewed-digest", selected_cases: plan.selected_cases, ordered_steps: plan.ordered_steps,
 request_cap: 4, cleanup_request_cap: 2, preconditions: [{case_id: "dependent", requirements: ["<fixture-required>"]}]};
const requests = [];
globalThis.fetch = async (url, options) => {
 const body = JSON.parse(options.body);
 requests.push({url, body});
 if (url === "/api/test-plan/preview") {
   const repeated = requests.filter((item) => item.url === url).length > 1;
   const current = repeated ? {...preview, plan_digest: "new-digest", plan: {...plan, plan_digest: "new-digest"}} : preview;
   return {ok: true, json: async () => current};
 }
 assert.equal(url, "/api/jobs");
 assert.equal(body.plan_digest, "reviewed-digest");
 return {ok: false, json: async () => ({error: "The selected plan changed since preview"})};
};
pollJob = async () => {};
(async () => {
 await refreshWorkflowPreview();
 assert.deepEqual(requests[0].body.cases, ["dependent"]);
 assert.equal(requests[0].body.reference_contract_id, "aws-contract");
 assert.equal($("startParam").disabled, false);
 assert($("paramWorkflowPreview").innerHTML.includes("&lt;fixture-required&gt;"));
 assert($("paramWorkflowPreview").innerHTML.includes("请求上限 4"));
 const payload = jobPayload("param_test");
 assert.equal(payload.plan_digest, "reviewed-digest");
 assert.equal(payload.source_id, "aws_bedrock");
 await createJob("param_test");
 assert.equal(requests.filter((item) => item.url === "/api/jobs").length, 1, "A rejected snapshot must not be automatically rerun");
 assert.equal(requests.length, 3, "Refresh the preview only after the server rejects the displayed digest");
 assert.equal(appState.workflowPreview.plan_digest, "new-digest");
 assert($("actionError").textContent.includes("The selected plan changed since preview"));
 assert.equal(appState.currentJobId, null);
 appState.formsByTab.param.workflowCases = "seed";
 assert.equal(workflowPreviewCurrent(), false);
 assert(!("plan_digest" in jobPayload("param_test")));
 renderBusyState();
 assert.equal($("startParam").disabled, true);

 const progressJob = {job_spec: {execution_plan: {...plan, plan_digest: appState.workflowPreview.plan_digest}}, progress: {
   total_cases: 4, completed_cases: 2, total_steps: 4, completed_steps: 2, attempt_count: 1,
   blocked_steps: 1, cleanup_status: "incomplete", resource_count: 1, deleted_resource_count: 0,
   runs: [{run_index: 1, cases: [{id: "seed", status: "passed"}, {id: "dependent", status: "blocked"}],
     steps: [{id: "seed", case_id: "seed", status: "passed"}, {id: "dependent", case_id: "dependent", status: "blocked", blocked_dependencies: ["seed"]}],
     attempts: [{id: "one", step_id: "seed", phase: "business", status: "exception", http_status: 500, request: {api_key: "never-render-this-secret"}}],
     cleanup_status: "incomplete", deleted_resource_count: 0, resource_count: 1, unknown_creation_count: 1}]}};
 renderWorkflowParamResult(progressJob);
 assert($("paramMetrics").innerHTML.includes("未完成"));
 assert($("paramWorkflowAttempts").innerHTML.includes("请求异常"));
 assert($("paramWorkflowAttempts").innerHTML.includes("500"));
 assert(!$("paramWorkflowAttempts").innerHTML.includes("never-render-this-secret"));
 assert($("paramFailedCaseLog").textContent.includes("依赖阻塞"));
 assert($("paramWorkflowCleanup").textContent.includes("1 个创建结果未确认"));
 assert($("paramTokenAudit").innerHTML.includes("No token audit data"));
 appState.formsByTab.param.workflowCases = "dependent";
 Object.assign(progressJob, {id: "stopped-run", type: "param_test", provider: "gateway", model: "fable",
   route_profile: "gateway", api_form: "anthropic_messages", status: "stopped", result_validation: {pass: false}});
 Object.assign(progressJob.job_spec, {test_binding_id: "workflow/exact-awsb", reference_contract_id: "aws-contract", test_workflow_snapshot: {}});
 Object.assign(progressJob.progress, {status: "incomplete", pass: false, percent: 100});
 appState.paramLiveJobId = progressJob.id;
 renderParamResults(progressJob);
 assert.equal($("paramResultSource").className, "pill bad", "Incomplete cleanup must never get a green result badge");

 const form = {provider: "gateway", model: "image", routeProfile: "route", apiForm: "image", suite: "full", resolutionPreferences: {}};
 imageApiFormCapability = () => ({suite: "grok_imagine"});
 applyImageResolutionDefaults(form);
 assert.equal(form.include2k, true); assert.equal(form.include4k, false);
 form.resolutionPreferences.grok_imagine = {include2k: false, include4k: false};
 applyImageResolutionDefaults(form); assert.equal(form.include2k, false);
 imageApiFormCapability = () => ({suite: "gpt_image_2"});
 applyImageResolutionDefaults(form); assert.equal(form.include4k, true);

 Object.assign(appState.formsByTab.image, {provider: "gateway", model: "image", routeProfile: "route", apiForm: "openai_images_generations",
   suite: "full", runCount: 2, cases: "square_1k", include2k: false, include4k: true});
 appState.config.image_providers = [{name: "gateway", has_key: true, default_model: "image", models: [{id: "image", family: "gpt-image-2"}]}];
 imageApiFormCapability = () => ({suite: "gpt_image_2", profile_status: "registered", parameter_test_runnable: true});
 const imageRequests = [];
 globalThis.fetch = async (url, options) => {
   const body = JSON.parse(options.body); imageRequests.push({url, body});
   assert.equal(body.run_count, 2); assert.deepEqual(body.image_plan.cases, ["square_1k"]);
   if (url === "/api/image-plan/preview") return {ok: true, json: async () => ({estimated_case_count: 1,
     plan_digest: "image-reviewed", plan: {run_count: 2}, request_cap: 8, cleanup_request_cap: 2})};
   assert.equal(url, "/api/jobs"); assert.equal(body.plan_digest, "image-reviewed");
   return {ok: true, json: async () => ({id: "image-created", type: "image_param_test"})};
 };
 let renderedImage;
 renderJob = (job) => {renderedImage = job;};
 await refreshImagePlanPreview();
 assert.equal(imagePreviewCurrent(), true);
 assert($("imageCaseHint").textContent.includes("请求上限 8"));
 await createJob("image_param_test");
 assert.equal(imageRequests.length, 2);
 assert.equal(renderedImage.id, "image-created");
 appState.formsByTab.image.runCount = 3;
 assert.equal(imagePreviewCurrent(), false);
 assert(!("plan_digest" in jobPayload("image_param_test")));
 process.stdout.write("ok");
})().catch((error) => {console.error(error); process.exit(1);});
'''
    completed = subprocess.run([shutil.which("node"), "-"], input=source + "\n" + probe, text=True,
                               capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ok"


def test_preview_digest_mismatch_rejected_before_credentials_or_job_creation(monkeypatch, tmp_path):
    import threading
    import requests
    config = load_config()
    provider, model = "anthropic_official", "claude-fable-5-1"
    binding = get_model_profile_catalog().list_workflows(provider_id=provider, request_model_id=model, enabled=True)[0]
    payload = {"type": "param_test", "provider": provider, "model": model,
               "route_profile": "vendor_direct", "api_form": "anthropic_messages",
               "workflow_binding_id": binding["test_binding_id"],
               "reference_contract_id": binding["contract_id"],
               "cases": [binding["workflow"]["case_ids"][0]], "param_test_runs": 1}
    monkeypatch.setattr(web, "load_config", lambda: config)
    credentials = Mock(side_effect=AssertionError("preview/mismatched plan read credentials"))
    monkeypatch.setattr(web, "provider_has_api_key", credentials)
    monkeypatch.setattr(web, "build_provider_child_env", credentials)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("preview sent HTTP")))
    monkeypatch.setattr(web.subprocess, "Popen", Mock(side_effect=AssertionError("mismatched plan launched")))
    manager = web.JobManager.__new__(web.JobManager)
    manager._lock = threading.Lock()
    manager._jobs = {}
    monkeypatch.setattr(web, "JOB_MANAGER", manager)
    monkeypatch.setattr(web, "JOBS_ROOT", tmp_path)
    client = web.app.test_client()
    preview = client.post("/api/test-plan/preview", json=payload)
    assert preview.status_code == 200, preview.json
    assert preview.json["job_spec"]["test_binding_id"] == binding["test_binding_id"]
    assert preview.json["plan"]["run_count"] == 1
    assert preview.json["request_cap"] >= 1
    rejected = client.post("/api/jobs", json={**payload, "plan_digest": "0" * 64})
    assert rejected.status_code == 400 and "changed since preview" in rejected.json["error"]
    credentials.assert_not_called()
    requests.Session.request.assert_not_called()
    web.subprocess.Popen.assert_not_called()
    assert not list(tmp_path.iterdir()) and manager._jobs == {}


def test_gateway_workflow_keeps_aws_reference_separate_from_wire_api(monkeypatch):
    import requests
    config = load_config()
    model = "claude-fable-5-1"
    gateway = copy.deepcopy(config["providers"]["anthropic_official"])
    gateway["base_url"] = "https://model.service-inference.ai/v1"
    gateway["api_interfaces"]["claude_messages"]["base_url"] = gateway["base_url"]
    gateway["models"]["candidates"] = [model]
    gateway["models"]["default"] = model
    gateway.pop("image", None)
    config["providers"] = {"inferenceai_awsb": gateway}
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("availability sent HTTP")))
    registry = web._add_workflow_capabilities(config, {"text": {}, "image": {}})
    leaf = registry["text"]["inferenceai_awsb"][model]["routes"]["vendor_direct"]["api_forms"]["anthropic_messages"]
    workflow = leaf["test_workflows"][0]
    assert workflow["source_id"] == "aws_bedrock" and workflow["api_form"] == "anthropic_messages"
    assert workflow["reference_contract_id"] == "claude_fable_5_1_aws_bedrock_runtime_messages"
    assert workflow["interface_id"].endswith("#bedrock-runtime-messages-default")
    assert leaf["parameter_test_enabled"] is False and leaf["pressure_test_runnable"] is False
    native = get_model_profile_catalog().get_interface(workflow["interface_id"])
    assert native["enabled"] is False
    requests.Session.request.assert_not_called()
