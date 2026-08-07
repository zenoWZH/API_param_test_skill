from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_staircase import run_locust


class StaircaseTest(unittest.TestCase):
    def test_child_run_uses_staircase_target_only_for_adaptive_sizing(self) -> None:
        config = {
            "active_provider": "yibu",
            "providers": {
                "yibu": {
                    "base_url": "https://example.test/v1",
                    "api_key_env": "YIBU_API_KEY",
                    "api_interfaces": {
                        "chat_completions": {
                            "base_url": "https://example.test/v1",
                            "path": "/chat/completions",
                            "auth": "bearer",
                        }
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"LOADTEST_TARGET_RPM": "1000", "LOADTEST_TARGET_TPM": "100000", "YIBU_API_KEY": "test-secret-value-123"}):
                with patch("scripts.run_staircase.subprocess.run") as mocked_run:
                    mocked_run.return_value.returncode = 0
                    run_locust(
                        config=config,
                        report_dir=Path(temp_dir),
                        users=30,
                        spawn_rate=5,
                        duration="1m",
                        workload="throughput",
                        phase="measure",
                        staircase_step=2,
                        target_rpm=1000,
                        target_tpm=100000,
                    )

        child_env = mocked_run.call_args.kwargs["env"]
        self.assertNotIn("LOADTEST_TARGET_RPM", child_env)
        self.assertNotIn("LOADTEST_TARGET_TPM", child_env)
        self.assertEqual(child_env["LOADTEST_TARGET_TOKENS_PER_REQUEST"], "100.0")
        self.assertEqual(child_env["LOADTEST_USERS"], "30")
        self.assertEqual(child_env["LOADTEST_STAIRCASE_STEP"], "2")
        child_command = mocked_run.call_args.args[0]
        exit_code_index = child_command.index("--exit-code-on-error")
        self.assertEqual(child_command[exit_code_index + 1], "0")


if __name__ == "__main__":
    unittest.main()
