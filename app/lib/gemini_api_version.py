"""Version policy for new requests to Google's AI Studio origin.

This policy concerns the API endpoint, not a model name or a provider alias.
Vertex and third-party Gemini-compatible gateways retain their own versions.
Historical catalog/report data is deliberately not rewritten here.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


AI_STUDIO_API_VERSION = "v1beta"
AI_STUDIO_HOST = "generativelanguage.googleapis.com"
_VERSION_SEGMENT = re.compile(r"^v[0-9]+(?:beta[0-9]*|alpha[0-9]*)?$")


def is_ai_studio_origin(base_url: str) -> bool:
    parsed = urlsplit(str(base_url))
    return parsed.hostname == AI_STUDIO_HOST


def resolve_gemini_api_version(
    base_url: str, requested_version: str | None = None
) -> str | None:
    """Choose beta for AI Studio, rejecting explicit conflicting selections."""
    if not is_ai_studio_origin(base_url):
        return requested_version
    if requested_version not in (None, "", AI_STUDIO_API_VERSION):
        raise ValueError("Google AI Studio requests require api_version=v1beta.")
    return AI_STUDIO_API_VERSION


def require_ai_studio_v1beta_url(url: str) -> str:
    """Validate the final URL before attaching credentials or sending a call."""
    if not is_ai_studio_origin(url):
        return url
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path.split("/")[1:2] != [AI_STUDIO_API_VERSION]
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("Google AI Studio outbound URL must use HTTPS /v1beta/.")
    return url


def build_gemini_api_url(
    base_url: str, path: str, *, api_version: str | None = None
) -> str:
    """Join an endpoint, adding the AI Studio default only when unversioned.

An explicit v1 URL is rejected instead of silently changing what was selected.
Unversioned compatibility paths, including /openai, gain the beta prefix.
"""
    url = f"{str(base_url).rstrip('/')}/{str(path).lstrip('/')}"
    if not is_ai_studio_origin(base_url):
        return url
    resolve_gemini_api_version(base_url, api_version)
    parsed = urlsplit(url)
    segments = parsed.path.lstrip("/").split("/")
    versions = [part for part in segments if _VERSION_SEGMENT.fullmatch(part)]
    if versions and (versions != [AI_STUDIO_API_VERSION] or segments[0] != versions[0]):
        raise ValueError("Google AI Studio endpoint conflicts with api_version=v1beta.")
    if not versions:
        parsed = parsed._replace(path=f"/{AI_STUDIO_API_VERSION}/{parsed.path.lstrip('/')}")
        url = urlunsplit(parsed)
    return require_ai_studio_v1beta_url(url)
