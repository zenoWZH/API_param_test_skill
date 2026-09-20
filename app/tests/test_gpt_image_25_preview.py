from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import web_console


class GptImage25PreviewTests(unittest.TestCase):
    def test_preview_counts_exact_selected_matrix_without_a_job_or_network(self):
        with tempfile.TemporaryDirectory() as directory, patch("lib.config.LOCAL_PROVIDERS_PATH", Path(directory) / "absent.yaml"), patch.dict(os.environ, {"LOADTEST_SKIP_DOTENV": "1"}):
            config = web_console.load_config()
        with patch.object(web_console, "load_config", return_value=config), \
                patch.object(web_console.JOB_MANAGER, "create") as create, \
                patch("requests.sessions.Session.request", side_effect=AssertionError("preview sent a request")):
            client = web_console.app.test_client()
            for api_form, count in (("openai_images_generations", 48), ("openai_images_edits", 59), ("openai_responses", 48)):
                with self.subTest(api_form=api_form):
                    response = client.post("/api/image-plan/preview", json={
                        "provider": "openai_official", "model": "gpt-image-2.5-sunburst",
                        "image_plan": {"suite": "full", "include_4k": True, "api_form": api_form, "route_profile": "vendor_direct"},
                    })
                    self.assertEqual(response.status_code, 200, response.get_json())
                    preview = response.get_json()
                    self.assertEqual(preview["estimated_case_count"], count)
                    self.assertEqual(preview["job_spec"]["schema_version"], 6)
                    self.assertEqual(preview["plan_digest"], preview["job_spec"]["execution_plan"]["plan_digest"])
            response = client.post("/api/image-plan/preview", json={
                "provider": "openai_official", "model": "gpt-image-2.5-flare",
                "image_plan": {"suite": "full", "include_4k": True, "no_negative": True,
                               "api_form": "openai_responses", "route_profile": "vendor_direct"},
            })
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["estimated_case_count"], 31)
            create.assert_not_called()

    def test_invalid_preview_payload_is_rejected(self):
        response = web_console.app.test_client().post("/api/image-plan/preview", json=[])
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
