from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

try:
    from datetime import UTC
except ImportError:  # Python < 3.11
    UTC = timezone.utc

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.image_validation import (  # noqa: E402
    ImageInfo,
    ImageTestCase,
    apply_capability_expectations,
    banana_variant_cases,
    evaluate_case,
    gpt_image_2_cases,
    grok_imagine_cases,
    infer_postprocess_suspicion,
    infer_resolution_correspondence,
    inspect_image_bytes,
)
from lib.credential_security import ProviderCredential, redact_secrets  # noqa: E402
from lib.config import (  # noqa: E402
    api_form_for_transport,
    get_image_model_config,
    get_provider_config,
    load_config,
)
from lib.reference_specs import (  # noqa: E402
    capability_profile_snapshot,
    load_model_capability_profile,
)
from lib.model_identity import (  # noqa: E402
    audit_model_identity,
    combine_model_identity_audits,
    summarize_model_identity_audits,
)
from lib.token_audit import audit_image_usage, summarize_token_audits  # noqa: E402


DEFAULT_PROMPT = (
    "A clean technical resolution test chart on a neutral gray background. "
    "Include a black and white checkerboard, thin diagonal lines, concentric circles, "
    "fine fabric texture, and the text RESOLUTION TEST in a simple sans-serif font."
)
IMAGE_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
IMAGE_TRANSPORTS = (
    "images-generations",
    "chat-completions",
    "gemini-interactions",
)
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "openai-processing-ms",
    "x-request-id",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    config = load_config()
    configured_provider = False
    try:
        provider_cfg = (
            get_provider_config(config, args.provider)
            if args.provider
            else {"name": "image-cli", "backend": "unknown", "models": {}}
        )
        configured_provider = bool(args.provider)
    except KeyError:
        # A caller may supply a fully resolved endpoint/model and an ephemeral
        # provider label (for example an isolated console integration test).
        # Auditing remains available but cannot apply provider-local aliases.
        provider_cfg = {
            "name": args.provider or "image-cli",
            "backend": "unknown",
            "models": {},
        }
    try:
        endpoint = normalize_image_endpoint(
            args.base_url or os.getenv("IMAGE_TEST_BASE_URL") or "",
            args.transport,
        )
    except ValueError as exc:
        parser.error(str(exc))

    model = args.model or {
        "gpt-image-2": "gpt-image-2",
        "banana": (
            "gemini-3.1-flash-image"
            if args.transport == "gemini-interactions"
            else "nano-banana-pro-{resolution_lower}"
        ),
        "grok-imagine": "grok-imagine-image",
    }[args.family]
    if (
        args.transport in {"chat-completions", "gemini-interactions"}
        and args.family != "banana"
    ):
        parser.error(
            f"The {args.transport} image transport supports only --family banana."
        )
    args.auth_mode = args.auth_mode or (
        "google_api_key"
        if args.transport == "gemini-interactions"
        else "bearer"
    )
    args.output_format = args.output_format or (
        "jpeg" if args.transport == "gemini-interactions" else "png"
    )
    if args.transport == "gemini-interactions" and args.output_format != "jpeg":
        parser.error(
            "Gemini Interactions image output currently supports only --output-format jpeg."
        )
    if args.family == "grok-imagine" and args.include_4k:
        parser.error("Grok Imagine supports 1K/2K tiers; use --include-2k instead of --include-4k.")
    if args.family != "grok-imagine" and args.include_2k:
        parser.error("--include-2k currently applies only to --family grok-imagine.")
    try:
        if args.family == "banana":
            cases = banana_variant_cases(
                args.suite,
                model_template=model,
                include_4k=args.include_4k,
                include_cross_control=not args.no_cross_control,
                include_negative=not args.no_negative,
                transport=args.transport,
            )
        elif args.family == "gpt-image-2":
            cases = gpt_image_2_cases(
                args.suite,
                include_4k=args.include_4k,
                include_negative=not args.no_negative,
            )
        else:
            cases = grok_imagine_cases(
                args.suite,
                include_2k=args.include_2k,
                include_negative=not args.no_negative,
            )
    except ValueError as exc:
        parser.error(str(exc))
    cases = _select_cases(cases, args.case)
    transport_api_form = api_form_for_transport(args.transport, modality="image")
    api_form = str(args.api_form or transport_api_form)
    if api_form != transport_api_form:
        parser.error(
            f"--api-form {api_form!r} conflicts with --transport "
            f"{args.transport!r} ({transport_api_form!r})."
        )
    route_profile = str(args.route_profile or "").strip()
    if configured_provider:
        try:
            selected_model_cfg = get_image_model_config(
                config,
                str(args.provider),
                str(model),
                route_profile=route_profile or None,
                api_form=api_form,
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        route_profile = str(selected_model_cfg.get("route_profile") or "")
        if str(selected_model_cfg.get("transport") or "") != args.transport:
            parser.error(
                f"API form {api_form!r} on route {route_profile!r} maps to "
                f"transport {selected_model_cfg.get('transport')!r}, not "
                f"{args.transport!r}."
            )
    else:
        try:
            capability = load_model_capability_profile(
                "image",
                args.family,
                str(model),
                route_profile=route_profile or None,
                api_form=api_form,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        if capability.get("profile_status") != "registered":
            parser.error(
                "Missing registered image model/API/route profile for "
                f"{args.family}/{api_form}/{model}/{route_profile or '<default>'}."
            )
        route_profile = str(capability.get("route_profile") or "")
        if str(capability.get("transport") or "") != args.transport:
            parser.error(
                f"API form {api_form!r} on route {route_profile!r} maps to "
                f"transport {capability.get('transport')!r}, not {args.transport!r}."
            )
    try:
        cases = apply_capability_expectations(
            cases,
            family=args.family,
            model=str(model),
            api_form=api_form,
            route_profile=route_profile,
        )
    except KeyError as exc:
        parser.error(str(exc))
    if args.family != "grok-imagine":
        cases = [
            _with_output_options(
                case,
                args.quality,
                args.output_format,
                transport=args.transport,
            )
            for case in cases
        ]

    public_plan = {
        "endpoint": endpoint,
        "provider": args.provider,
        "transport": args.transport,
        "api_form": api_form,
        "route_profile": route_profile,
        "auth_mode": args.auth_mode,
        "family": args.family,
        "model": model,
        "requested_models": sorted({case.model_override or model for case in cases}),
        "suite": args.suite,
        "include_2k": args.include_2k,
        "include_4k": args.include_4k,
        "visual_forensics": not args.no_visual_forensics,
        "prompt_sha256": hashlib.sha256(args.prompt.encode("utf-8")).hexdigest(),
        "prompt": args.prompt if args.store_prompt else None,
        "cases": [case.public() for case in cases],
    }
    if args.dry_run:
        print(json.dumps(public_plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    api_key = (
        getpass.getpass("Image provider API key: ")
        if args.api_key_stdin
        else os.getenv(args.api_key_env)
    )
    if not api_key:
        parser.error(
            f"Missing API key. Set {args.api_key_env}; raw API keys are intentionally not accepted as CLI arguments."
        )

    report_dir = _report_dir(args.output_dir, model)
    images_dir = report_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_dir / "plan.json", public_plan)

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    credential = ProviderCredential.create(
        provider=f"image:{model}",
        secret=api_key,
        base_urls=[endpoint],
    )
    model_check = redact_secrets(
        _list_models(
            session,
            models_endpoint(endpoint, args.transport, args.family),
            args.timeout,
            credential,
            args.auth_mode,
        )
    )
    requested_models = sorted({case.model_override or model for case in cases})
    model_check["requested_models"] = requested_models
    model_check["missing_requested_models"] = [
        item for item in requested_models if item not in model_check.get("model_ids", [])
    ]
    _write_json(report_dir / "model_check.json", model_check)

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        print(
            f"[image-param] {index}/{len(cases)} {case.name} "
            f"size={_case_size_label(case)} expected={case.expected_outcome}",
            flush=True,
        )
        result = redact_secrets(
            run_case(
                session,
                endpoint,
                model,
                args.prompt,
                case,
                timeout=args.timeout,
                images_dir=images_dir,
                visual_forensics=not args.no_visual_forensics,
                credential=credential,
                transport=args.transport,
                auth_mode=args.auth_mode,
                config=config,
                provider=args.provider,
                provider_cfg=provider_cfg,
            )
        )
        results.append(result)
        _write_json(report_dir / "case_results.json", results)
        print(
            f"[image-param] {case.name} status={result.get('status')} "
            f"http={result.get('status_code')} latency_ms={result.get('latency_ms')}",
            flush=True,
        )

    postprocess = infer_postprocess_suspicion(results)
    resolution_correspondence = infer_resolution_correspondence(results)
    failures = [item for item in results if not item.get("pass")]
    token_audit_summary = summarize_token_audits(results)
    model_identity_summary = summarize_model_identity_audits(results)
    token_accuracy_pass = bool(token_audit_summary.get("pass", True))
    model_identity_pass = bool(model_identity_summary.get("pass", True))
    capability_snapshot = capability_profile_snapshot(
        "image",
        args.family,
        str(model),
        [str(item.get("case") or "") for item in results],
        api_form=api_form,
        route_profile=route_profile,
    )
    summary = {
        "pass": not failures and token_accuracy_pass and model_identity_pass,
        "compatibility_pass": not failures,
        "token_accuracy_pass": token_accuracy_pass,
        "model_identity_pass": model_identity_pass,
        "family": args.family,
        "model": model,
        "api_form": api_form,
        "route_profile": capability_snapshot.get("route_profile"),
        "endpoint": endpoint,
        "transport": args.transport,
        "suite": args.suite,
        "case_count": len(results),
        "pass_count": len(results) - len(failures),
        "failure_count": len(failures),
        "failed_cases": [item.get("case") for item in failures],
        "model_capability_profile": capability_snapshot,
        "model_check": model_check,
        "token_audit_summary": token_audit_summary,
        "model_identity_summary": model_identity_summary,
        "postprocess_inference": postprocess,
        "resolution_correspondence": resolution_correspondence,
        "report_dir": str(report_dir),
    }
    _write_json(report_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["pass"] else 1


def run_case(
    session: requests.Session,
    endpoint: str,
    model: str,
    prompt: str,
    case: ImageTestCase,
    *,
    timeout: int,
    images_dir: Path,
    visual_forensics: bool,
    credential: ProviderCredential | None = None,
    transport: str = "images-generations",
    auth_mode: str = "bearer",
    config: dict[str, Any] | None = None,
    provider: str | None = None,
    provider_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = _request_body(case, model, prompt, transport)
    effective_model = case.model_override or model
    status_code: int | None = None
    latency_ms: float | None = None
    usage: dict[str, Any] = {}
    error: dict[str, Any] | str | None = None
    images: list[ImageInfo] = []
    artifacts: list[str] = []
    response_keys: list[str] = []
    response_headers: dict[str, str] = {}
    revised_prompt_present = False
    payload: dict[str, Any] = {}

    try:
        started = time.perf_counter()
        headers = (
            credential.auth_headers(url=endpoint, auth_mode=auth_mode)
            if credential
            else {}
        )
        response = session.post(
            endpoint,
            json=body,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        status_code = response.status_code
        response_headers = _safe_headers(response.headers)
        payload = _safe_json(response)
        response_keys = sorted(payload) if isinstance(payload, dict) else []
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

        if 200 <= response.status_code <= 299:
            data = _response_image_items(payload, transport)
            if not data:
                error = {
                    "chat-completions": "chat response contains no supported image data",
                    "gemini-interactions": (
                        "Gemini interaction contains no image content in model-output steps"
                    ),
                }.get(transport, "response.data is not a non-empty list")
            else:
                for image_index, item in enumerate(data):
                    if not isinstance(item, dict):
                        error = f"response.data[{image_index}] is not an object"
                        continue
                    revised_prompt_present = revised_prompt_present or bool(item.get("revised_prompt"))
                    try:
                        raw, delivery = _image_bytes(item, timeout)
                        info = inspect_image_bytes(raw, visual_forensics=visual_forensics)
                        extension = IMAGE_EXTENSIONS.get(info.format, ".bin")
                        artifact = images_dir / f"{case.name}_{image_index + 1}{extension}"
                        artifact.write_bytes(raw)
                        images.append(info)
                        artifacts.append(str(artifact.relative_to(images_dir.parent)))
                    except Exception as exc:
                        error = (
                            f"image_decode_failed:index={image_index}:"
                            f"{exc.__class__.__name__}:{exc}"
                        )
                    else:
                        if delivery == "url":
                            response_headers["image_delivery"] = "url_without_forwarded_authorization"
        else:
            error = _error_payload(payload, response.text)
    except requests.RequestException as exc:
        error = f"{exc.__class__.__name__}: {exc}"

    result = evaluate_case(
        case,
        status_code=status_code,
        images=images,
        usage=usage,
        latency_ms=latency_ms,
        error=error,
    )
    result.update(
        {
            "model": effective_model,
            "api_form": api_form_for_transport(transport, modality="image"),
            "transport": transport,
            "response_keys": response_keys,
            "response_headers": response_headers,
            "revised_prompt_present": revised_prompt_present,
            "artifacts": artifacts,
            "token_audit": audit_image_usage(
                body,
                payload,
                usage,
                config or {},
                provider=provider,
                model=effective_model,
                transport=(
                    "gemini_interactions"
                    if transport == "gemini-interactions"
                    else "chat_completions"
                    if transport == "chat-completions"
                    else "image_generation"
                ),
            ),
            "model_identity_audit": combine_model_identity_audits(
                [
                    audit_model_identity(
                        requested_model=effective_model,
                        result=SimpleNamespace(
                            response_json=payload,
                            headers=response_headers,
                        ),
                        transport=(
                            "gemini_interactions"
                            if transport == "gemini-interactions"
                            else "chat_completions"
                            if transport == "chat-completions"
                            else "image_generation"
                        ),
                        provider_cfg=provider_cfg
                        or {"backend": "unknown", "models": {}},
                        exchange="initial",
                        request_endpoint=urlparse(endpoint).path,
                    )
                ]
            ),
        }
    )
    if error and case.expected_outcome == "success" and not result["failures"]:
        result["pass"] = False
        result["status"] = "fail"
        result["verification_level"] = "none"
        result["failures"] = ["response_or_image_decode_error"]
    return redact_secrets(result)


def normalize_image_endpoint(value: str, transport: str) -> str:
    if transport not in IMAGE_TRANSPORTS:
        raise ValueError(f"Unsupported image transport: {transport!r}")
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise ValueError("Set --base-url or IMAGE_TEST_BASE_URL.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid image provider URL: {value!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Image provider URL must not contain user information, a query, or a fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote image provider URLs must use HTTPS.")
    suffixes = {
        "images-generations": "/images/generations",
        "chat-completions": "/chat/completions",
        "gemini-interactions": "/v1beta/interactions",
    }
    suffix = suffixes[transport]
    mismatched = [
        candidate
        for candidate in suffixes.values()
        if candidate != suffix and value.endswith(candidate)
    ]
    if mismatched:
        raise ValueError(
            f"The supplied endpoint does not match --transport {transport}."
        )
    if value.endswith(suffix):
        return value
    if transport == "gemini-interactions":
        if value.endswith("/v1beta"):
            return value + "/interactions"
        return value + suffix
    if value.endswith("/v1"):
        return value + suffix
    return value + "/v1" + suffix


def normalize_image_generation_endpoint(value: str) -> str:
    return normalize_image_endpoint(value, "images-generations")


def models_endpoint(
    image_endpoint: str,
    transport: str = "images-generations",
    family: str | None = None,
) -> str:
    suffix = {
        "images-generations": "/images/generations",
        "chat-completions": "/chat/completions",
        "gemini-interactions": "/v1beta/interactions",
    }.get(transport)
    if suffix is None or not image_endpoint.endswith(suffix):
        raise ValueError(f"Unexpected {transport} image endpoint: {image_endpoint!r}")
    if transport == "gemini-interactions":
        return image_endpoint[: -len("/interactions")] + "/models"
    route = "/image-generation-models" if family == "grok-imagine" else "/models"
    return image_endpoint[: -len(suffix)] + route


def _request_body(
    case: ImageTestCase,
    model: str,
    prompt: str,
    transport: str,
) -> dict[str, Any]:
    if transport == "images-generations":
        return case.request_body(model, prompt)
    if transport == "gemini-interactions":
        response_format = case.parameters.get("response_format")
        if not isinstance(response_format, dict):
            raise ValueError(
                "Gemini Interactions image cases require response_format."
            )
        return {
            "model": case.model_override or model,
            "input": [{"type": "text", "text": prompt}],
            "response_format": response_format,
        }
    if transport != "chat-completions":
        raise ValueError(f"Unsupported image transport: {transport!r}")
    extra_body = case.parameters.get("extra_body")
    google = extra_body.get("google") if isinstance(extra_body, dict) else None
    image_config = google.get("image_config") if isinstance(google, dict) else None
    if not isinstance(image_config, dict):
        raise ValueError(
            "Chat image cases require extra_body.google.image_config."
        )
    return {
        "model": case.model_override or model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "extra_body": {"google": {"image_config": image_config}},
    }


def _response_image_items(
    payload: dict[str, Any],
    transport: str,
) -> list[dict[str, Any]]:
    if transport == "images-generations":
        data = payload.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    if transport == "gemini-interactions":
        return _gemini_interaction_image_items(payload)

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []

    image_items: list[dict[str, Any]] = []
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                image_items.append({"url": image_url})

    content = message.get("content")
    if isinstance(content, str):
        for match in re.finditer(
            r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
            content,
        ):
            image_items.append({"url": match.group(0)})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                image_items.append({"url": image_url})

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in image_items:
        url = str(item["url"])
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if digest not in seen:
            seen.add(digest)
            unique.append(item)
    return unique


def _gemini_interaction_image_items(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract inline or URI images from native Interactions model-output steps."""
    candidates: list[dict[str, Any]] = []
    convenience = payload.get("output_image")
    if isinstance(convenience, dict):
        candidates.append(convenience)
    for step in payload.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        candidates.extend(
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "image"
        )

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        data = candidate.get("data")
        uri = candidate.get("uri")
        item: dict[str, Any] | None = None
        identity: str | None = None
        if isinstance(data, str) and data:
            item = {"b64_json": data}
            identity = hashlib.sha256(data.encode("ascii", errors="ignore")).hexdigest()
        elif isinstance(uri, str) and uri:
            item = {"url": uri}
            identity = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        if item is not None and identity not in seen:
            seen.add(str(identity))
            items.append(item)
    return items


def _image_bytes(item: dict[str, Any], timeout: int) -> tuple[bytes, str]:
    b64_json = item.get("b64_json")
    if isinstance(b64_json, str) and b64_json:
        try:
            return base64.b64decode(b64_json, validate=True), "b64_json"
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid b64_json: {exc}") from exc
    url = item.get("url")
    if isinstance(url, str) and url:
        data_uri = re.fullmatch(
            r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=]+)",
            url,
        )
        if data_uri:
            try:
                return base64.b64decode(data_uri.group(1), validate=True), "data_url"
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"invalid image data URL: {exc}") from exc
        # Deliberately use a new unauthenticated request. A signed image URL may point
        # to a different host and must never receive the provider Authorization header.
        response = requests.get(
            url,
            headers={
                "Accept": "image/*",
                "User-Agent": "Mozilla/5.0 (compatible; yibuapi-image-param-test/1.0)",
            },
            timeout=min(timeout, 60),
        )
        response.raise_for_status()
        return response.content, "url"
    raise ValueError("image item has neither b64_json nor url")


def _list_models(
    session: requests.Session,
    endpoint: str,
    timeout: int,
    credential: ProviderCredential,
    auth_mode: str = "bearer",
) -> dict[str, Any]:
    try:
        started = time.perf_counter()
        response = session.get(
            endpoint,
            headers=credential.auth_headers(url=endpoint, auth_mode=auth_mode),
            timeout=min(timeout, 60),
            allow_redirects=False,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = _safe_json(response)
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raw_models = payload.get("models")
        model_ids = []
        if isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                value = item.get("id") or item.get("name")
                if not value:
                    continue
                model_id = str(value)
                if model_id.startswith("models/"):
                    model_id = model_id[len("models/") :]
                model_ids.append(model_id)
        return {
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "model_count": len(model_ids),
            "model_ids": model_ids,
            "response_headers": _safe_headers(response.headers),
        }
    except requests.RequestException as exc:
        return {
            "status_code": None,
            "error": f"{exc.__class__.__name__}: {exc}",
            "model_count": 0,
            "model_ids": [],
        }


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() in SAFE_RESPONSE_HEADERS
    }


def _error_payload(payload: dict[str, Any], raw_text: str) -> dict[str, Any] | str:
    error = payload.get("error")
    if isinstance(error, dict):
        return {
            "type": error.get("type"),
            "code": error.get("code"),
            "message": str(error.get("message") or "")[:1000],
        }
    return " ".join(str(raw_text or "").split())[:1000]


def _with_output_options(
    case: ImageTestCase,
    quality: str,
    output_format: str,
    *,
    transport: str = "images-generations",
) -> ImageTestCase:
    if transport in {"chat-completions", "gemini-interactions"}:
        requested_resolution = case.metadata.get("requested_resolution")
        if not isinstance(requested_resolution, str):
            raise ValueError(
                f"{transport} image cases require requested_resolution metadata."
            )
        aspect_ratio = str(case.metadata.get("aspect_ratio") or "1:1")
        if transport == "gemini-interactions":
            if output_format != "jpeg":
                raise ValueError(
                    "Gemini Interactions image output currently supports only jpeg."
                )
            return replace(
                case,
                parameters={
                    "response_format": {
                        "type": "image",
                        "mime_type": "image/jpeg",
                        "aspect_ratio": aspect_ratio,
                        "image_size": requested_resolution,
                    },
                },
                expected_format=(
                    "JPEG"
                    if case.expected_outcome in {"success", "observation"}
                    else None
                ),
            )
        return replace(
            case,
            parameters={
                "extra_body": {
                    "google": {
                        "image_config": {
                            "aspect_ratio": aspect_ratio,
                            "image_size": requested_resolution,
                        }
                    }
                },
            },
            expected_format=None,
        )
    forced_output_format = case.metadata.get("forced_output_format")
    effective_output_format = (
        str(forced_output_format)
        if forced_output_format
        else output_format
    )
    parameters = {
        **case.parameters,
        "quality": quality,
        "output_format": effective_output_format,
    }
    expected_format = {
        "png": "PNG",
        "jpeg": "JPEG",
        "webp": "WEBP",
    }[effective_output_format]
    return replace(
        case,
        parameters=parameters,
        expected_format=(
            expected_format
            if case.expected_outcome in {"success", "observation"}
            else None
        ),
    )


def _select_cases(cases: list[ImageTestCase], selected: list[str]) -> list[ImageTestCase]:
    if not selected:
        return cases
    by_name = {case.name: case for case in cases}
    missing = [name for name in selected if name not in by_name]
    if missing:
        raise SystemExit(f"Unknown image test case(s): {', '.join(missing)}")
    return [by_name[name] for name in selected]


def _case_size_label(case: ImageTestCase) -> str | None:
    aspect_ratio = case.parameters.get("aspect_ratio")
    resolution = case.parameters.get("resolution")
    if aspect_ratio is not None or resolution is not None:
        return f"{resolution or '?'} / {aspect_ratio or '?'}"
    direct = case.parameters.get("size")
    if direct is not None:
        return str(direct)
    extra_body = case.parameters.get("extra_body")
    google = extra_body.get("google") if isinstance(extra_body, dict) else None
    image_config = google.get("image_config") if isinstance(google, dict) else None
    value = image_config.get("image_size") if isinstance(image_config, dict) else None
    if value is not None:
        return str(value)
    response_format = case.parameters.get("response_format")
    if isinstance(response_format, dict):
        size = response_format.get("image_size")
        ratio = response_format.get("aspect_ratio")
        if size is not None or ratio is not None:
            return f"{size or '?'} / {ratio or '?'}"
    return None


def _report_dir(value: str | None, model: str) -> Path:
    if value:
        target = Path(value)
        return target if target.is_absolute() else PROJECT_ROOT / target
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model).strip("_") or "image-model"
    return default_reports_root() / "image_param" / f"{timestamp}-{slug}"


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_secrets(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run reusable GPT Image 2, Banana, or Grok Imagine parameter tests. "
            "Postprocess results are heuristic and never reported as confirmed."
        )
    )
    parser.add_argument(
        "--base-url",
        help="Provider root, /v1 base, or full endpoint selected by --transport.",
    )
    parser.add_argument(
        "--provider",
        help="Configured provider name used for token-counter and model-identity aliases.",
    )
    parser.add_argument("--api-key-env", default="IMAGE_TEST_API_KEY")
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from an interactive hidden prompt instead of the environment.",
    )
    parser.add_argument(
        "--family",
        choices=("gpt-image-2", "banana", "grok-imagine"),
        default="gpt-image-2",
        help="Select the parameter contract and case matrix.",
    )
    parser.add_argument(
        "--transport",
        choices=IMAGE_TRANSPORTS,
        default="images-generations",
        help=(
            "Image API shape. chat-completions is for Banana providers that return "
            "images from choices[].message and accept extra_body.google.image_config; "
            "gemini-interactions is Google's native /v1beta/interactions API."
        ),
    )
    parser.add_argument(
        "--api-form",
        choices=(
            "openai_images_generations",
            "openai_chat_completions",
            "gemini_interactions",
        ),
        help=(
            "Public image API form selected within --route-profile. The internal "
            "--transport must map to the same form."
        ),
    )
    parser.add_argument(
        "--route-profile",
        default=os.getenv("LOADTEST_ROUTE_PROFILE"),
        help="Route contract selected before the image API form.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("bearer", "google_api_key"),
        help=(
            "Credential header mode. Defaults to google_api_key for "
            "gemini-interactions and bearer for compatible image transports."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("IMAGE_TEST_MODEL"),
        help=(
            "GPT/Grok model name, fixed Banana model ID, or Banana alias template containing "
            "{resolution} or {resolution_lower}. Native Gemini Interactions defaults to "
            "gemini-3.1-flash-image; compatible Banana defaults to "
            "nano-banana-pro-{resolution_lower}."
        ),
    )
    parser.add_argument("--suite", choices=("smoke", "resolution", "full"), default="smoke")
    parser.add_argument(
        "--include-2k",
        action="store_true",
        help="Acknowledge Grok Imagine 2K charges and include its 2K cases.",
    )
    parser.add_argument("--include-4k", action="store_true", help="Acknowledge the billable 4K case.")
    parser.add_argument("--no-negative", action="store_true", help="Skip family-specific invalid-parameter rejection cases.")
    parser.add_argument(
        "--no-cross-control",
        action="store_true",
        help="Skip Banana cases that intentionally conflict alias suffix and size.",
    )
    parser.add_argument("--case", action="append", default=[], help="Run only the named case; repeatable.")
    parser.add_argument(
        "--quality",
        choices=("low", "medium", "high", "auto"),
        default="low",
        help="GPT/Banana Images option; not sent for Grok Imagine.",
    )
    parser.add_argument(
        "--output-format",
        choices=("png", "jpeg", "webp"),
        help=(
            "GPT/Banana Images option (default png). Gemini Interactions defaults "
            "to and currently accepts only jpeg; Grok records the actual format."
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--store-prompt", action="store_true", help="Store the prompt in plan.json; default stores only SHA-256.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output-dir")
    parser.add_argument("--no-visual-forensics", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the request plan without reading a key or sending requests.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
