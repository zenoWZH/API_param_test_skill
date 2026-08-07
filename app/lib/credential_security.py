from __future__ import annotations

import os
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterable
from urllib.parse import urlsplit


SELECTED_API_KEY_ENV = "LOADTEST_SELECTED_API_KEY"
SELECTED_API_KEY_PROVIDER_ENV = "LOADTEST_SELECTED_API_KEY_PROVIDER"
SKIP_DOTENV_ENV = "LOADTEST_SKIP_DOTENV"
REDACTED = "***REDACTED***"

ALLOWED_PROFILE_REQUEST_HEADERS = {
    "x-vertex-ai-llm-request-type",
    "x-vertex-ai-llm-shared-request-type",
}

_SAFE_CHILD_ENV_NAMES = {
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "DYLD_LIBRARY_PATH",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LD_LIBRARY_PATH",
    "NO_PROXY",
    "PATH",
    "PYTHONPATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "VIRTUAL_ENV",
}
_SENSITIVE_FIELD_NAMES = {
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-goog-api-key",
}
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|TOKEN|SECRET(?:_ACCESS_KEY)?|PASSWORD|CREDENTIALS?)(?:$|_)",
    re.IGNORECASE,
)
_REGISTERED_SECRETS: set[str] = set()
_REGISTERED_SECRETS_LOCK = Lock()


@dataclass(frozen=True, repr=False)
class ProviderCredential:
    provider: str
    _secret: str
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        secret: str,
        base_urls: Iterable[str],
    ) -> "ProviderCredential":
        value = str(secret)
        if not value:
            raise ValueError("API key must not be empty.")
        origins = frozenset(_normalized_origin(url) for url in base_urls if str(url).strip())
        if not origins:
            raise ValueError(f"Provider {provider!r} has no credential-bound origin.")
        register_secret(value)
        return cls(provider=provider, _secret=value, allowed_origins=origins)

    def auth_headers(self, *, url: str, auth_mode: str) -> dict[str, str]:
        origin = _normalized_origin(url)
        if origin not in self.allowed_origins:
            raise ValueError(
                f"Refusing to send credentials for provider {self.provider!r} to unbound origin {origin!r}."
            )
        if auth_mode == "anthropic":
            return {
                "x-api-key": self._secret,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        if auth_mode == "google_api_key":
            return {"x-goog-api-key": self._secret, "content-type": "application/json"}
        if auth_mode == "bearer":
            return {"Authorization": f"Bearer {self._secret}", "content-type": "application/json"}
        raise ValueError(f"Unsupported auth mode {auth_mode!r}.")

    def redact(self, value: Any) -> Any:
        return redact_secrets(value, extra_secrets=(self._secret,))

    def _for_child_process(self) -> str:
        return self._secret


def credential_from_config(config: dict[str, Any], provider: str | None = None) -> ProviderCredential:
    # Local import avoids making config loading depend on this module.
    from .config import get_api_key, get_provider_config

    provider_cfg = get_provider_config(config, provider)
    base_urls = [str(provider_cfg.get("base_url") or "")]
    for interface in (provider_cfg.get("api_interfaces") or {}).values():
        if isinstance(interface, dict):
            base_urls.append(str(interface.get("base_url") or provider_cfg.get("base_url") or ""))
    return ProviderCredential.create(
        provider=str(provider_cfg["name"]),
        secret=get_api_key(config, str(provider_cfg["name"])),
        base_urls=base_urls,
    )


def build_provider_child_env(
    config: dict[str, Any],
    provider: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    credential = credential_from_config(config, provider)
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in _SAFE_CHILD_ENV_NAMES or name.startswith("LLM_API_TEST_") or (
            name.startswith("LOADTEST_") and not _looks_sensitive_name(name)
        ):
            env[name] = value
    if extra:
        for key, value in extra.items():
            name = str(key)
            if _looks_sensitive_name(name):
                raise ValueError(f"Child environment override {name!r} is sensitive and is not allowed.")
            env[name] = str(value)
    env[SELECTED_API_KEY_ENV] = credential._for_child_process()
    env[SELECTED_API_KEY_PROVIDER_ENV] = credential.provider
    env[SKIP_DOTENV_ENV] = "1"
    return env


def validate_profile_request_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("request_headers keys must be non-empty strings.")
        lowered = key.strip().lower()
        if lowered not in ALLOWED_PROFILE_REQUEST_HEADERS:
            raise ValueError(
                f"request_headers.{key} is not allowed; profile headers cannot set authentication, "
                "routing, cookie, host, or content headers."
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError("request_headers values must be non-empty strings.")
        if "\r" in value or "\n" in value:
            raise ValueError("request_headers values must not contain line breaks.")
        normalized[key.strip()] = value
    return normalized


def register_secret(secret: str) -> None:
    value = str(secret)
    if len(value) < 8:
        return
    with _REGISTERED_SECRETS_LOCK:
        _REGISTERED_SECRETS.add(value)


def redact_secrets(value: Any, *, extra_secrets: Iterable[str] = ()) -> Any:
    secrets = _known_secrets(extra_secrets)
    return _redact(value, secrets)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key).strip().lower() in _SENSITIVE_FIELD_NAMES:
                result[key] = REDACTED
            else:
                result[key] = _redact(item, secrets)
        return result
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, secrets) for item in value)
    if isinstance(value, str):
        redacted = value
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, REDACTED)
        return redacted
    return value


def _known_secrets(extra_secrets: Iterable[str]) -> tuple[str, ...]:
    values = {str(value) for value in extra_secrets if str(value)}
    with _REGISTERED_SECRETS_LOCK:
        values.update(_REGISTERED_SECRETS)
    for name, value in os.environ.items():
        if (
            name != SELECTED_API_KEY_PROVIDER_ENV
            and value
            and len(value) >= 8
            and _looks_sensitive_name(name)
        ):
            values.add(value)
    return tuple(values)


def _looks_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_ENV_NAME.search(name))


def _normalized_origin(url: str) -> str:
    parsed = urlsplit(str(url))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Credential-bound URL must be an absolute HTTP(S) URL: {url!r}.")
    if parsed.username or parsed.password:
        raise ValueError("Credential-bound URLs must not contain user information.")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"
