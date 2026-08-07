from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.config import get_api_key, load_config
from lib.credential_security import (
    REDACTED,
    SELECTED_API_KEY_ENV,
    SELECTED_API_KEY_PROVIDER_ENV,
    SKIP_DOTENV_ENV,
    ProviderCredential,
    build_provider_child_env,
    redact_secrets,
    validate_profile_request_headers,
)
from lib.metrics import write_json


def _provider_config(api_key_env: str = "ALPHA_API_KEY") -> dict:
    return {
        "active_provider": "alpha",
        "api": {"timeout_sec": 30},
        "providers": {
            "alpha": {
                "label": "Alpha",
                "base_url": "https://alpha.example/v1",
                "backend": "openai_compatible",
                "default_transport": "chat_completions",
                "api_interfaces": {
                    "chat_completions": {
                        "path": "/chat/completions",
                        "auth": "bearer",
                    }
                },
                "api_key_env": api_key_env,
                "models": {
                    "default": "model-a",
                    "candidates": ["model-a"],
                    "families": {"model-a": "gpt"},
                    "transports": {"model-a": "chat_completions"},
                },
            }
        },
    }


class CredentialSecurityTest(unittest.TestCase):
    def test_inline_local_key_is_resolved_but_never_merged_into_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            local_path = root / "providers.local.yaml"
            config_path.write_text(
                """active_provider: alpha
api:
  timeout_sec: 30
providers:
  alpha:
    label: Alpha
    base_url: https://alpha.example/v1
    backend: openai_compatible
    default_transport: chat_completions
    api_interfaces:
      chat_completions:
        path: /chat/completions
        auth: bearer
    api_key_env: ALPHA_API_KEY
    models:
      default: model-a
      candidates: [model-a]
      families: {model-a: gpt}
      transports: {model-a: chat_completions}
""",
                encoding="utf-8",
            )
            local_path.write_text(
                "providers:\n  alpha:\n    api_key: local-inline-secret-123\n",
                encoding="utf-8",
            )
            with patch("lib.config.LOCAL_PROVIDERS_PATH", local_path), patch.dict(
                os.environ, {}, clear=True
            ):
                config = load_config(config_path)
                self.assertNotIn("local-inline-secret-123", json.dumps(config))
                self.assertNotIn("api_key", config["providers"]["alpha"])
                self.assertEqual(get_api_key(config, "alpha"), "local-inline-secret-123")

    def test_selected_child_key_is_bound_to_provider(self) -> None:
        config = _provider_config()
        with patch.dict(
            os.environ,
            {
                "ALPHA_API_KEY": "alpha-secret-value-123",
                "BETA_API_KEY": "beta-secret-value-456",
                "HF_TOKEN": "hf-secret-value-789",
                "AWS_SECRET_ACCESS_KEY": "aws-secret-value-012",
                "LOADTEST_WORKLOAD": "throughput",
            },
            clear=True,
        ):
            child = build_provider_child_env(config, "alpha")
            self.assertEqual(child[SELECTED_API_KEY_ENV], "alpha-secret-value-123")
            self.assertEqual(child[SELECTED_API_KEY_PROVIDER_ENV], "alpha")
            self.assertEqual(child[SKIP_DOTENV_ENV], "1")
            self.assertEqual(child["LOADTEST_WORKLOAD"], "throughput")
            self.assertNotIn("ALPHA_API_KEY", child)
            self.assertNotIn("BETA_API_KEY", child)
            self.assertNotIn("HF_TOKEN", child)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
            with patch.dict(os.environ, child, clear=True):
                self.assertEqual(get_api_key(config, "alpha"), "alpha-secret-value-123")
                with self.assertRaisesRegex(RuntimeError, "Missing API key"):
                    get_api_key({**config, "active_provider": "beta", "providers": {"beta": config["providers"]["alpha"]}}, "beta")

    def test_provider_does_not_fall_back_to_another_provider_legacy_key(self) -> None:
        config = _provider_config()
        with patch.dict(os.environ, {"YIBU_API_KEY": "wrong-provider-secret-123"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing API key"):
                get_api_key(config, "alpha")

    def test_child_extra_cannot_override_selected_credential(self) -> None:
        config = _provider_config()
        with patch.dict(os.environ, {"ALPHA_API_KEY": "alpha-secret-value-123"}, clear=True):
            with self.assertRaisesRegex(ValueError, "sensitive"):
                build_provider_child_env(
                    config,
                    "alpha",
                    {SELECTED_API_KEY_ENV: "attacker-value"},
                )

    def test_credential_only_builds_auth_for_bound_origin(self) -> None:
        credential = ProviderCredential.create(
            provider="alpha",
            secret="alpha-secret-value-123",
            base_urls=["https://alpha.example/v1"],
        )
        self.assertEqual(
            credential.auth_headers(url="https://alpha.example/v1/models", auth_mode="bearer")[
                "Authorization"
            ],
            "Bearer alpha-secret-value-123",
        )
        with self.assertRaisesRegex(ValueError, "unbound origin"):
            credential.auth_headers(url="https://attacker.example/v1", auth_mode="bearer")

    def test_profile_headers_are_case_insensitive_allowlist(self) -> None:
        self.assertEqual(
            validate_profile_request_headers({"X-Vertex-AI-LLM-Request-Type": "shared"}),
            {"X-Vertex-AI-LLM-Request-Type": "shared"},
        )
        for name in ("Authorization", "x-GOOG-api-key", "Host", "Content-Type", "Cookie"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not allowed"):
                validate_profile_request_headers({name: "override"})

    def test_recursive_redaction_and_json_writer_remove_canary(self) -> None:
        secret = "canary-secret-value-123"
        payload = {
            "api_key": secret,
            "nested": [f"upstream echoed {secret}", {"Authorization": f"Bearer {secret}"}],
        }
        with patch.dict(os.environ, {"CANARY_API_KEY": secret}, clear=False):
            redacted = redact_secrets(payload)
            self.assertEqual(redacted["api_key"], REDACTED)
            self.assertNotIn(secret, json.dumps(redacted))
            with tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir) / "result.json"
                write_json(target, payload)
                self.assertNotIn(secret, target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
