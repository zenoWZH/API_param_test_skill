from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lib.config import load_config
from lib.job_spec import resolve_image_plan
from scripts import web_console


class GptImage25PlanIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory, patch("lib.config.LOCAL_PROVIDERS_PATH", Path(directory) / "absent.yaml"), patch.dict(os.environ, {"LOADTEST_SKIP_DOTENV": "1"}):
            cls.config = load_config()

    def test_all_six_jobs_keep_exact_binding_and_full_matrix(self):
        forms = {"openai_images_generations": ("images-generations", 48, "/images/generations"),
                 "openai_images_edits": ("images-edits", 59, "/images/edits"),
                 "openai_responses": ("openai-responses-image", 48, "/responses")}
        for variant in ("sunburst", "flare"):
            model = "gpt-image-2.5-" + variant
            for form, (transport, count, suffix) in forms.items():
                with (
                    self.subTest(model=model, form=form),
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(web_console, "JOBS_ROOT", Path(directory)),
                    patch.object(web_console, "load_config", return_value=self.config),
                    patch.object(web_console, "image_provider_has_api_key", return_value=True),
                    patch.object(web_console.JobManager, "_load_finished_jobs"),
                    patch.object(web_console.JobManager, "_discover_external_jobs"),
                    patch.object(web_console.JobManager, "_start_locked") as start_job,
                ):
                    job = web_console.JobManager().create({
                        "type": "image_param_test", "provider": "openai_official", "model": model,
                        "timeout_sec": 120,
                        "image_plan": {"suite": "full", "include_4k": True,
                                       "api_form": form, "route_profile": "vendor_direct"},
                    })
                    start_job.assert_called_once_with(job)
                    plan = job.image_plan
                    self.assertEqual(plan["transport"], transport)
                    self.assertEqual(plan["estimated_case_count"], count)
                    self.assertEqual(plan["endpoint"], "https://api.openai.com/v1" + suffix)
                    self.assertEqual(len(set(plan["test_profiles"])), count)
                    binding = plan["model_profile_database"]
                    self.assertEqual(binding["source_id"], "openai")
                    self.assertEqual(binding["profile_id"], "image/openai/gpt-image-2/" + model)
                    self.assertFalse(plan["model_capability_profile"]["pressure_test_enabled"])
                    self.assertEqual(job.job_spec["api_form"], form)
                    self.assertEqual(job.job_spec["model_profile_database"], binding)
                    self.assertEqual(plan["model_capability_profile"]["model_profile_database"], binding)
                    persisted = json.loads((job.report_dir / "job_spec.json").read_text())
                    self.assertEqual(persisted, job.job_spec)
                    self.assertEqual(persisted["image_plan"], plan)

    def test_hidden_global_controls_cannot_change_pinned_matrix(self):
        plan = resolve_image_plan(self.config, {"image_plan": {"suite": "smoke", "api_form": "openai_responses",
            "quality": "max", "output_format": "jpeg"}}, "openai_official", "gpt-image-2.5-flare", 120)
        self.assertEqual((plan["quality"], plan["output_format"]), ("low", "png"))
        self.assertEqual(plan["cases"], ["gpt_image_25_responses_baseline_generate"])

    def test_mismatched_api_and_transport_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_image_plan(self.config, {"image_plan": {"api_form": "openai_images_generations", "transport": "images-edits"}},
                               "openai_official", "gpt-image-2.5-flare", 120)


if __name__ == "__main__":
    unittest.main()
