from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.config import list_image_providers, load_config
import scripts.web_console as web_console


class ImagePlanPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory, patch(
            "lib.config.LOCAL_PROVIDERS_PATH", Path(directory) / "absent.yaml"
        ), patch.dict(os.environ, {"LOADTEST_SKIP_DOTENV": "1"}):
            cls.config = load_config()
        # Use the same public model and interface choices that populate the UI.
        public_config = {
            **cls.config,
            "providers": {"openai_official": cls.config["providers"]["openai_official"]},
        }
        cls.providers = list_image_providers(public_config)
        cls.image_capabilities = web_console._capability_registry_payload(public_config)["image"]

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LLM_API_TEST_DISABLE_AUTH": "1"}).start()
        patch.object(web_console, "load_config", return_value=self.config).start()
        self.create_job = patch.object(
            web_console.JOB_MANAGER, "create",
            side_effect=AssertionError("preview must not create a job"),
        ).start()
        self.client = web_console.app.test_client()

    def preview_fixtures(self):
        counts = {
            "openai_images_generations": (45, 30, 47, 48, 33),
            "openai_images_edits": (56, 39, 58, 59, 42),
            "openai_responses": (47, 30, 48, 48, 31),
        }
        scenarios = [
            ("resolution", False, False),
            ("resolution", True, False),
            ("resolution", False, True),
            ("full", False, True),
            ("full", True, True),
        ]
        fixtures = []
        selections = set()
        for provider in self.providers:
            for model in provider["models"]:
                if model["id"] not in (
                    "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"
                ):
                    continue
                for route, route_row in model["routes"].items():
                    for api_form in route_row["api_forms"]:
                        selections.add((model["id"], api_form))
                        for (suite, no_negative, include_4k), expected in [
                            (("smoke", False, False), 1),
                            *zip(scenarios, counts[api_form]),
                        ]:
                            payload = {
                                "type": "image_param_test",
                                "provider": provider["name"],
                                "model": model["id"],
                                "timeout_sec": 120,
                                "run_count": 1,
                                "image_plan": {
                                    "route_profile": route,
                                    "api_form": api_form,
                                    "suite": suite,
                                    "include_2k": False,
                                    "include_4k": include_4k,
                                    "quality": "low",
                                    "output_format": "png",
                                    "no_negative": no_negative,
                                    "no_cross_control": False,
                                    "visual_forensics": True,
                                },
                            }
                            with self.subTest(model=model["id"], **payload["image_plan"]):
                                response = self.client.post(
                                    "/api/image-plan/preview", json=payload
                                )
                                self.assertEqual(response.status_code, 200, response.json)
                                self.assertEqual(response.json["estimated_case_count"], expected)
                                self.assertEqual(response.json["job_spec"]["schema_version"], 6)
                            preview = response.json
                            # Exercise the actual UI fields without copying large
                            # frozen image fixtures into Node dozens of times.
                            fixtures.append({"payload": payload, "preview": {
                                **{key: preview[key] for key in ("estimated_case_count", "plan_digest", "request_cap", "cleanup_request_cap")},
                                "plan": {"run_count": preview["plan"]["run_count"]},
                            }})
        self.assertEqual(len(selections), 6)
        self.create_job.assert_not_called()
        return fixtures

    @unittest.skipUnless(shutil.which("node"), "node is required for console JS tests")
    def test_actual_public_plans_reach_ui_and_stale_responses_cannot_replace_counts(self):
        fixtures = self.preview_fixtures()
        providers = copy.deepcopy(self.providers)
        for provider in providers:
            provider["has_key"] = True
        source = (
            Path(__file__).resolve().parents[1] / "scripts/static/web_console.js"
        ).read_text(encoding="utf-8").rsplit("\nbindEvents();", 1)[0]
        probe = r"""
const assert = require("node:assert/strict");
const nodes = {};
globalThis.document = {
  getElementById(id) {
    return nodes[id] ||= {innerHTML: "", textContent: "", value: "", className: ""};
  },
};
appState.config = {image_providers: fixtureProviders, image_model_capabilities: fixtureCapabilities};
appState.timeoutSec = 120;
const pending = [];
globalThis.fetch = (url, options) => {
  assert.equal(url, "/api/image-plan/preview");
  assert.equal(options.method, "POST");
  return new Promise(resolve => pending.push({payload: JSON.parse(options.body), resolve}));
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const expectedHint = preview => `${preview.estimated_case_count} 个用例 × ${preview.plan.run_count} 轮 · 请求上限 ${preview.request_cap} · 清理上限 ${preview.cleanup_request_cap}`;
function select(payload) {
  const plan = payload.image_plan;
  Object.assign(appState.formsByTab.image, {
    provider: payload.provider, model: payload.model,
    routeProfile: plan.route_profile, apiForm: plan.api_form,
    suite: plan.suite, include2k: plan.include_2k, include4k: plan.include_4k,
    noNegative: plan.no_negative, noCrossControl: plan.no_cross_control,
    visualForensics: plan.visual_forensics, quality: plan.quality,
    outputFormat: plan.output_format,
  });
  renderImageControls();
  assert.equal(nodes.imageCaseHint.textContent, "counting cases…");
  assert.equal(nodes.startImage.disabled, true);
  const request = pending.shift();
  assert.deepEqual(request.payload, payload);
  return request;
}
(async () => {
  for (const fixture of fixtures) {
    const request = select(fixture.payload);
    request.resolve({ok: true, json: async () => fixture.preview});
    await tick();
    const count = fixture.preview.estimated_case_count;
    assert.equal(nodes.imageCaseHint.textContent, expectedHint(fixture.preview));
    assert.equal(nodes.startImage.disabled, false);
  }
  // A slow generation preview must not overwrite a later edits selection.
  const oldFixture = fixtures.find(x => x.payload.image_plan.suite === "resolution");
  const newFixture = fixtures.find(x => x.payload.image_plan.api_form === "openai_images_edits"
    && x.payload.image_plan.suite === "resolution");
  const oldRequest = select(oldFixture.payload);
  const newRequest = select(newFixture.payload);
  newRequest.resolve({ok: true, json: async () => newFixture.preview});
  await tick();
  oldRequest.resolve({ok: true, json: async () => oldFixture.preview});
  await tick();
  assert.equal(nodes.imageCaseHint.textContent, expectedHint(newFixture.preview));
  // Invalid/full-without-4K or failed previews clear any previously trusted count.
  const invalid = JSON.parse(JSON.stringify(newFixture.payload));
  invalid.image_plan.no_negative = !invalid.image_plan.no_negative;
  const failed = select(invalid);
  failed.resolve({ok: false, json: async () => ({error: "full requires include_4k"})});
  await tick();
  assert.equal(nodes.imageCaseHint.textContent, "case count unavailable");
  assert.equal(nodes.imageCaseHint.title, "full requires include_4k");
  assert.equal(nodes.startImage.disabled, true);
  // A transient failure can be retried on the next explicit controls refresh.
  const transient = select(newFixture.payload);
  transient.resolve({ok: false, json: async () => ({error: "temporarily unavailable"})});
  await tick();
  const retry = select(newFixture.payload);
  retry.resolve({ok: true, json: async () => newFixture.preview});
  await tick();
  assert.equal(nodes.imageCaseHint.textContent, expectedHint(newFixture.preview));
  assert.equal(nodes.startImage.disabled, false);
  process.stdout.write("ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            [shutil.which("node") or "node", "-"],
            input=source + "\nconst fixtures = " + json.dumps(fixtures)
            + ";\nconst fixtureProviders = " + json.dumps(providers)
            + ";\nconst fixtureCapabilities = " + json.dumps(self.image_capabilities) + ";\n" + probe,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "ok")

    def test_invalid_plan_is_rejected_without_creating_job(self):
        for payload in [
            [],
            {
                "provider": "openai_official",
                "model": "gpt-image-2.5-flare",
                "image_plan": {"suite": "full", "include_4k": False},
            },
            {
                "provider": "openai_official",
                "model": "gpt-image-2.5-flare",
                "image_plan": {"route_profile": "missing-route"},
            },
        ]:
            with self.subTest(payload=payload):
                response = self.client.post("/api/image-plan/preview", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
                self.assertNotIn("estimated_case_count", response.json)
        self.create_job.assert_not_called()
