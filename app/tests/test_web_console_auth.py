from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.web_console as web_console


class WebConsoleAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.auth_path = data / "console_auth.json"
        self.password_path = data / "console_password"
        self.secret_path = data / "console_secret_key"
        self.path_patches = [
            patch.object(web_console, "CONSOLE_AUTH_PATH", self.auth_path),
            patch.object(web_console, "CONSOLE_PASSWORD_PATH", self.password_path),
            patch.object(web_console, "CONSOLE_SECRET_PATH", self.secret_path),
        ]
        for item in self.path_patches:
            item.start()
        self.env_patch = patch.dict(
            os.environ,
            {
                "LLM_API_TEST_DISABLE_AUTH": "0",
                "WEB_CONSOLE_USER": "",
                "WEB_CONSOLE_PASSWORD": "",
            },
        )
        self.env_patch.start()
        web_console.app.config.update(
            TESTING=True,
            SECRET_KEY="test-only-session-secret",
            SESSION_COOKIE_SECURE=False,
        )
        web_console._LOGIN_FAILURES.clear()

    def tearDown(self) -> None:
        self.env_patch.stop()
        for item in reversed(self.path_patches):
            item.stop()
        self.temp.cleanup()

    def test_missing_auth_file_fails_closed(self) -> None:
        with self.assertRaises(web_console.AuthConfigurationError):
            web_console._load_auth_credentials()
        response = web_console.app.test_client().get("/api/config")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json(),
            {"error": "authentication configuration unavailable"},
        )

    def test_corrupt_auth_file_fails_closed_and_is_not_overwritten(self) -> None:
        self.auth_path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(web_console.AuthConfigurationError):
            web_console._ensure_auth_configured()
        self.assertEqual(self.auth_path.read_text(encoding="utf-8"), "not-json")
        response = web_console.app.test_client().get("/login")
        self.assertEqual(response.status_code, 503)

    def test_missing_auth_is_initialized_with_private_files(self) -> None:
        generated = web_console._ensure_auth_configured()
        self.assertIsNotNone(generated)
        self.assertTrue(self.auth_path.is_file())
        self.assertTrue(self.password_path.is_file())
        self.assertEqual(stat.S_IMODE(self.auth_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.password_path.stat().st_mode), 0o600)
        self.assertIsNotNone(web_console._load_auth_credentials())

    def test_auth_replace_failure_preserves_existing_file(self) -> None:
        original = "keep-existing-auth"
        self.auth_path.write_text(original, encoding="utf-8")
        with patch.object(
            web_console.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                web_console._write_auth_file("admin", "replacement-password")
        self.assertEqual(self.auth_path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.auth_path.parent.glob(".*.tmp")), [])

    def test_existing_session_secret_is_private_and_must_not_be_empty(self) -> None:
        self.secret_path.write_text("existing-session-secret", encoding="utf-8")
        os.chmod(self.secret_path.parent, 0o755)
        os.chmod(self.secret_path, 0o644)
        web_console._ensure_secret_key()
        self.assertEqual(web_console.app.secret_key, "existing-session-secret")
        self.assertEqual(stat.S_IMODE(self.secret_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.secret_path.stat().st_mode), 0o600)

        self.secret_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(web_console.AuthConfigurationError, "empty"):
            web_console._ensure_secret_key()
        self.assertEqual(self.secret_path.read_text(encoding="utf-8"), "")

    def test_explicit_disable_is_the_only_fail_open_state(self) -> None:
        with patch.dict(os.environ, {"LLM_API_TEST_DISABLE_AUTH": "1"}):
            self.assertIsNone(web_console._load_auth_credentials())
            response = web_console.app.test_client().get("/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_partial_environment_credentials_fail_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"WEB_CONSOLE_USER": "admin", "WEB_CONSOLE_PASSWORD": ""},
        ):
            with self.assertRaises(web_console.AuthConfigurationError):
                web_console._load_auth_credentials()

    def test_login_cookie_is_httponly_and_same_site_lax(self) -> None:
        web_console._write_auth_file("admin", "test-password")
        response = web_console.app.test_client().post(
            "/login",
            data={"username": "admin", "password": "test-password"},
        )
        self.assertEqual(response.status_code, 302)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)


if __name__ == "__main__":
    unittest.main()
