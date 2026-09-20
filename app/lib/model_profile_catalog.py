from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

from .config import (
    PROJECT_ROOT,
    get_image_model_config,
    get_model_api_forms,
    get_model_family,
    get_model_route_profiles,
    get_provider_config,
)


PACKAGE_DISTRIBUTION = "yibu-model-profile-db"
CHECKOUT_PACKAGE_ROOT = PROJECT_ROOT.parent / "packages" / "model-profile-db"
# This is an explicit consumer pin, not a second Profile authoring source.  The
# values must move together after an approved catalog/package upgrade.  The
# package's own manifest still supplies and verifies the underlying artifacts.
EXPECTED_MODEL_PROFILE_DATABASE = {
    "package_version": "0.3.0",
    "manifest_schema_version": 1,
    "mpdb_schema_version": 3,
    "catalog_version": "0.4.0",
    "catalog_digest": (
        "f7217d7595a6545a66a33e4d8599dda08706d290c885740d12d7561b5f9fc39a"
    ),
    "test_extension_schema_version": 2,
    "test_extension_digest": (
        "629ca0a5dfa6eceeeac73a6f5ee73546c43efb8dfe861032bba5a7a6b6658b51"
    ),
}

# A repository checkout must consume its sibling package, even if the Python
# environment happens to contain an older wheel with the same import name.
# Packaged deployments have no sibling path and use the exact requirement pin.
if CHECKOUT_PACKAGE_ROOT.is_dir():
    checkout_path = str(CHECKOUT_PACKAGE_ROOT.resolve())
    sys.path[:] = [
        entry
        for entry in sys.path
        if str(Path(entry or ".").resolve()) != checkout_path
    ]
    sys.path.insert(0, checkout_path)

try:
    import model_profile_db as _model_profile_db
except ModuleNotFoundError as exc:
    if exc.name != "model_profile_db":
        raise
    raise RuntimeError(
        "yibu-model-profile-db is required. Install the exact version declared "
        "in app/requirements.txt."
    ) from exc

from model_profile_db import Catalog, load_catalog  # noqa: E402


_PACKAGE_ORIGIN = Path(_model_profile_db.__file__).resolve()
if CHECKOUT_PACKAGE_ROOT.is_dir():
    checkout_root = CHECKOUT_PACKAGE_ROOT.resolve()
    if not _PACKAGE_ORIGIN.is_relative_to(checkout_root):
        raise RuntimeError(
            "A source checkout must use its sibling yibu-model-profile-db; "
            f"Python already loaded {_PACKAGE_ORIGIN}. Restart with the checkout "
            "package first on PYTHONPATH."
        )
    PACKAGE_LOAD_MODE = "checkout"
else:
    PACKAGE_LOAD_MODE = "installed"

DEFAULT_API_FORM_BY_FAMILY = {
    "deepseek": "openai_chat_completions",
    "glm": "openai_chat_completions",
    "qwen": "openai_chat_completions",
    "gemini": "openai_chat_completions",
    "claude": "anthropic_messages",
    "claude_fable": "anthropic_messages",
    "gpt": "openai_chat_completions",
    "kimi": "openai_chat_completions",
    "minimax": "openai_chat_completions",
    "grok": "openai_responses",
    "gpt-image-2": "openai_images_generations",
    # Keep the existing app runtime default.  Changing the parameter-test
    # default is governed by the separate non-migration decision.
    "banana": "gemini_interactions",
    "grok-imagine": "openai_images_generations",
}


def stable_slug(value: str) -> str:
    text = str(value).strip().casefold().replace("/", "--")
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
    return text or "unknown"


def interface_slug(api_form: str, routing_mode: str) -> str:
    form = {
        "openai_chat_completions": "openai-chat",
        "openai_responses": "openai-responses",
        "anthropic_messages": "anthropic-messages",
        "gemini_generate_content": "gemini-generate-content",
        "gemini_interactions": "gemini-interactions",
        "openai_images_generations": "openai-images",
        "openai_images_edits": "openai-image-edits",
        "aws_bedrock_runtime_messages": "bedrock-runtime-messages",
        "aws_bedrock_invoke": "bedrock-invoke",
        "aws_bedrock_converse": "bedrock-converse",
    }.get(str(api_form), stable_slug(api_form))
    if routing_mode in {
        "vendor_direct",
        "vendor_compat",
        "aliyun_maas",
        "azure_openai",
        "azure_foundry",
        "google_ai_studio",
        "google_vertex",
        "aws_bedrock",
        "openrouter",
    }:
        return f"{form}-default"
    return f"{form}-{stable_slug(routing_mode)}"


def _package_version() -> str:
    package_file = Path(_model_profile_db.__file__).resolve()
    for parent in package_file.parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        text = pyproject.read_text(encoding="utf-8")
        if tomllib is not None:
            payload = tomllib.loads(text)
            project = payload.get("project") or {}
            if str(project.get("name") or "") == PACKAGE_DISTRIBUTION:
                value = project.get("version")
                if value:
                    return str(value)
        else:  # Python 3.10 checkout fallback; no extra TOML dependency.
            project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
            if project:
                name = re.search(
                    r'(?m)^name\s*=\s*["\']([^"\']+)["\']', project.group(1)
                )
                version = re.search(
                    r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
                    project.group(1),
                )
                if (
                    name
                    and name.group(1) == PACKAGE_DISTRIBUTION
                    and version
                ):
                    return version.group(1)
    try:
        return importlib.metadata.version(PACKAGE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_and_verify_runtime_manifest() -> dict[str, Any]:
    """Verify the package artifacts actually consumed by the app."""

    data_dir = _PACKAGE_ORIGIN.parent / "data"
    manifest_path = data_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot read yibu-model-profile-db manifest: {manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("yibu-model-profile-db manifest must be an object.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("yibu-model-profile-db manifest has no artifact registry.")
    for artifact_name in ("json", "test_extensions_yaml"):
        row = artifacts.get(artifact_name)
        if not isinstance(row, dict):
            raise RuntimeError(
                f"yibu-model-profile-db manifest is missing {artifact_name}."
            )
        relative_path = Path(str(row.get("path") or ""))
        artifact_path = (data_dir / relative_path).resolve()
        if data_dir.resolve() not in artifact_path.parents:
            raise RuntimeError(
                f"yibu-model-profile-db {artifact_name} leaves its data directory."
            )
        try:
            size = artifact_path.stat().st_size
            digest = _sha256_file(artifact_path)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot read yibu-model-profile-db artifact: {artifact_path}"
            ) from exc
        if size != row.get("size") or digest != row.get("sha256"):
            raise RuntimeError(
                f"yibu-model-profile-db {artifact_name} does not match its manifest."
            )
    return manifest


def _validate_pinned_model_profile_database(
    *,
    package_version: str,
    catalog_info: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    actual = {
        "package_version": package_version,
        "manifest_schema_version": manifest.get("manifest_schema_version"),
        "mpdb_schema_version": catalog_info.get("mpdb_schema_version"),
        "catalog_version": catalog_info.get("catalog_version"),
        "catalog_digest": catalog_info.get("catalog_digest"),
        "test_extension_schema_version": catalog_info.get(
            "test_extension_schema_version"
        ),
        "test_extension_digest": catalog_info.get("test_extension_digest"),
    }
    mismatches = {
        field: {
            "expected": expected,
            "actual": actual.get(field),
        }
        for field, expected in EXPECTED_MODEL_PROFILE_DATABASE.items()
        if actual.get(field) != expected
    }
    for field in (
        "mpdb_schema_version",
        "catalog_version",
        "catalog_digest",
        "test_extension_digest",
    ):
        if manifest.get(field) != catalog_info.get(field):
            mismatches[f"manifest.{field}"] = {
                "expected": catalog_info.get(field),
                "actual": manifest.get(field),
            }
    if catalog_info.get("test_extensions_loaded") is not True:
        mismatches["test_extensions_loaded"] = {
            "expected": True,
            "actual": catalog_info.get("test_extensions_loaded"),
        }
    if mismatches:
        details = ", ".join(
            f"{field}: expected={values['expected']!r}, actual={values['actual']!r}"
            for field, values in sorted(mismatches.items())
        )
        raise RuntimeError(
            "Installed yibu-model-profile-db does not match the app consumer pin; "
            + details
        )


@lru_cache(maxsize=1)
def get_model_profile_catalog() -> Catalog:
    """Load and fail closed on an unpinned or tampered MPDB package."""

    manifest = _load_and_verify_runtime_manifest()
    catalog = load_catalog()
    _validate_pinned_model_profile_database(
        package_version=_package_version(),
        catalog_info=catalog.database_info(),
        manifest=manifest,
    )
    return catalog


def catalog_metadata() -> dict[str, Any]:
    catalog = get_model_profile_catalog()
    info = catalog.database_info()
    return {
        "package_name": PACKAGE_DISTRIBUTION,
        "package_version": _package_version(),
        "package_load_mode": PACKAGE_LOAD_MODE,
        "package_origin": str(Path(_model_profile_db.__file__).resolve()),
        "mpdb_schema_version": info["mpdb_schema_version"],
        "catalog_version": info["catalog_version"],
        "catalog_digest": info["catalog_digest"],
        "test_extension_schema_version": info.get(
            "test_extension_schema_version"
        ),
        "test_extension_digest": info.get("test_extension_digest"),
        "identity_contract": copy.deepcopy(info.get("identity_contract")),
    }


def _contract_source_id(contract: dict[str, Any]) -> str:
    """Return the schema-v4 scalar source and reject a drifting legacy alias."""

    source_id = str(contract.get("source_id") or "").strip()
    aliases = [str(value).strip() for value in contract.get("source_ids") or []]
    if not source_id:
        # Read compatibility is intentionally limited to pre-v4 snapshots.  A
        # multi-source Contract can never be treated as a valid runtime source.
        if len(aliases) == 1 and aliases[0]:
            return aliases[0]
        raise ValueError(
            f"Contract {contract.get('contract_id')!r} has no scalar source_id."
        )
    if aliases and aliases != [source_id]:
        raise ValueError(
            f"Contract {contract.get('contract_id')!r} source_ids alias conflicts "
            "with scalar source_id."
        )
    return source_id


def _eligible_contract_interfaces(
    catalog: Catalog,
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return enabled, executable Interfaces on executable Profiles."""

    contract_id = str(contract.get("contract_id") or "")
    source_id = _contract_source_id(contract)
    result: list[dict[str, Any]] = []
    for row in catalog.list_interfaces(
        source=source_id,
        family=str(contract.get("family_id") or ""),
        api_form=str(contract.get("api_form") or ""),
    ):
        interface_id = str(row.get("interface_id") or "")
        if not interface_id or contract_id not in {
            str(value) for value in row.get("contract_ids") or []
        }:
            continue
        try:
            interface = catalog.get_interface(interface_id)
            profile = catalog.get_profile(str(interface.get("profile_id") or ""))
        except KeyError:
            continue
        if (
            interface.get("enabled", True) is not True
            or interface.get("executable", True) is not True
            or str(interface.get("test_binding_status") or "required")
            != "required"
            or str(profile.get("profile_state") or "executable")
            != "executable"
        ):
            continue
        result.append(interface)
    return result


def _configured_source_id(
    provider_cfg: dict[str, Any], model: str, route_profile: str
) -> tuple[str, bool]:
    models_cfg = provider_cfg.get("models") or {}
    per_model = models_cfg.get("reference_source_ids") or {}
    configured = str(
        (per_model.get(model) if isinstance(per_model, dict) else "")
        or provider_cfg.get("reference_source_id")
        or ""
    ).strip()
    if configured:
        return configured, False

    # Compatibility-only provider declarations may still name an MPDB
    # Contract.  Convert it to the Contract's single source_id immediately;
    # the legacy identifier is never persisted as source identity.
    legacy_sources = models_cfg.get("reference_sources") or {}
    legacy = legacy_sources.get(model) if isinstance(legacy_sources, dict) else None
    if isinstance(legacy, dict):
        route_value = legacy.get(route_profile)
        if isinstance(route_value, str):
            legacy = route_value
    if isinstance(legacy, str) and legacy.strip():
        try:
            contract = get_model_profile_catalog().get_contract(legacy.strip())
        except KeyError:
            pass
        else:
            return _contract_source_id(contract), True

    return "", True


def _reference_model_id(
    provider_cfg: dict[str, Any],
    model: str,
    *,
    modality: str,
) -> tuple[str, bool]:
    configured: Any = None
    if modality == "text":
        mapping = (provider_cfg.get("models") or {}).get("reference_model_ids") or {}
        if isinstance(mapping, dict):
            configured = mapping.get(model)
    else:
        for model_cfg in ((provider_cfg.get("image") or {}).get("models") or []):
            if isinstance(model_cfg, dict) and str(model_cfg.get("id") or "") == model:
                configured = model_cfg.get("reference_model_id")
                break
    return str(configured or model).strip(), not bool(configured)


def _configured_binding(
    config: dict[str, Any],
    provider: str,
    model: str,
    *,
    modality: str,
    route_profile: str,
    api_form: str,
) -> dict[str, str] | None:
    provider_cfg = get_provider_config(config, provider)
    if modality == "text":
        raw = ((provider_cfg.get("models") or {}).get("reference_bindings") or {}).get(
            model
        )
    else:
        raw = None
        for model_cfg in ((provider_cfg.get("image") or {}).get("models") or []):
            if isinstance(model_cfg, dict) and str(model_cfg.get("id") or "") == model:
                raw = model_cfg.get("reference_binding")
                break
    if not isinstance(raw, dict):
        return None
    profile_id = str(raw.get("profile_id") or "").strip()
    interface_id = str(raw.get("interface_id") or "").strip()
    interfaces = raw.get("interfaces") or {}
    if not interface_id and isinstance(interfaces, dict):
        direct = interfaces.get(api_form)
        if isinstance(direct, str):
            interface_id = direct.strip()
        if not interface_id:
            route_interfaces = interfaces.get(route_profile) or {}
            if isinstance(route_interfaces, dict):
                interface_id = str(route_interfaces.get(api_form) or "").strip()
    if not profile_id or not interface_id:
        raise ValueError(
            "Configured MPDB reference_binding requires profile_id and interface_id."
        )
    return {"profile_id": profile_id, "interface_id": interface_id}


def _profile_supports_suite(
    catalog: Catalog, profile: dict[str, Any], suite_family_id: str
) -> bool:
    if str(profile.get("family_id") or "") == suite_family_id:
        return True
    for interface_id in profile.get("interface_ids") or []:
        interface = catalog.get_interface(str(interface_id))
        for binding in interface.get("test_bindings") or []:
            if (
                isinstance(binding, dict)
                and binding.get("extension_type") == "model_test_policy"
                and str(binding.get("suite_family_id") or "") == suite_family_id
            ):
                return True
    return False


def _canonical_family_from_catalog(
    catalog: Catalog,
    *,
    modality: str,
    suite_family_id: str,
    model: str,
    source_id: str | None = None,
) -> str:
    candidates = catalog.list_profiles(
        modality=modality,
        source=source_id,
        model=model,
    )
    families = {
        str(profile.get("family_id") or "")
        for profile in candidates
        if _profile_supports_suite(catalog, profile, suite_family_id)
    }
    if len(families) == 1:
        return next(iter(families))
    return suite_family_id


def _source_for_family_route(
    family: str,
    route_profile: str,
    *,
    model: str | None = None,
    modality: str | None = None,
    api_form: str | None = None,
) -> str:
    """Derive a source from package records without a duplicated source map."""

    catalog = get_model_profile_catalog()
    route = str(route_profile or "")
    if route:
        try:
            catalog.get_source(route)
        except KeyError:
            pass
        else:
            return route

    candidates: set[str] = set()
    origin_candidates: set[str] = set()
    for profile in catalog.list_profiles(
        modality=modality,
        model=model,
    ):
        if not _profile_supports_suite(catalog, profile, str(family)):
            continue
        if api_form and not any(
            str(catalog.get_interface(str(interface_id)).get("api_form") or "")
            == str(api_form)
            for interface_id in profile.get("interface_ids") or []
        ):
            continue
        source_id = str(profile.get("source_id") or "")
        if not source_id:
            continue
        candidates.add(source_id)
        source = catalog.get_source(source_id)
        if str(source.get("authority") or "") == "origin_vendor":
            origin_candidates.add(source_id)
    if len(origin_candidates) == 1:
        return next(iter(origin_candidates))
    if len(candidates) == 1:
        return next(iter(candidates))
    return ""


def _validate_catalog_binding(
    profile: dict[str, Any],
    interface: dict[str, Any],
    *,
    source_id: str,
    reference_model_id: str,
    canonical_family_id: str,
    api_form: str,
    modality: str,
    runtime_model_id: str | None = None,
) -> None:
    expected_profile = {
        "source_id": source_id,
        "family_id": canonical_family_id,
        "modality": modality,
    }
    mismatches = [
        field
        for field, expected in expected_profile.items()
        if str(profile.get(field) or "") != expected
    ]
    accepted_model_ids = {
        str(value).casefold()
        for value in (
            profile.get("model_slug"),
            profile.get("canonical_model_id"),
            *(profile.get("request_model_ids") or []),
        )
        if value
    }
    if str(reference_model_id).casefold() not in accepted_model_ids:
        mismatches.append("request_model_ids")
    interface_request_model_ids = {
        str(value).casefold()
        for value in interface.get("request_model_ids") or []
    }
    canonical_names = {
        str(profile.get("model_slug") or "").casefold(),
        str(profile.get("canonical_model_id") or "").casefold(),
    }
    # Metadata-only queries may use canonical names without a runtime target.
    # Actual runtime requests still need an endpoint-native ID or explicit map.
    if (
        interface_request_model_ids
        and str(reference_model_id).casefold() not in interface_request_model_ids
        and not (
            str(reference_model_id).casefold() in canonical_names
            and (runtime_model_id is None or str(runtime_model_id).casefold() in interface_request_model_ids)
        )
    ):
        mismatches.append("interface.request_model_ids")
    expected_interface = {
        "profile_id": str(profile.get("profile_id") or ""),
        "source_id": source_id,
        "family_id": canonical_family_id,
        "modality": modality,
        "api_form": api_form,
    }
    for field, expected in expected_interface.items():
        if str(interface.get(field) or "") != expected:
            mismatches.append(f"interface.{field}")
    if str(profile.get("profile_state") or "executable") != "executable":
        mismatches.append("profile.profile_state")
    if interface.get("enabled", True) is not True:
        mismatches.append("interface.enabled")
    if interface.get("executable", True) is not True:
        mismatches.append("interface.executable")
    if str(interface.get("test_binding_status") or "required") != "required":
        mismatches.append("interface.test_binding_status")
    if mismatches:
        raise ValueError(
            "Official MPDB binding does not match the selected source/family/API: "
            + ", ".join(sorted(set(mismatches)))
        )


def _resolve_exact_profile_interface(
    *,
    source_id: str,
    modality: str,
    family: str,
    model: str,
    api_form: str,
    runtime_model_id: str | None = None,
    contract_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    catalog = get_model_profile_catalog()
    profiles = [
        profile
        for profile in catalog.list_profiles(
            modality=modality,
            source=source_id,
            model=model,
        )
        if _profile_supports_suite(catalog, profile, family)
    ]
    if len(profiles) != 1:
        raise ValueError(
            "Expected one exact MPDB Profile for "
            f"source={source_id!r}, modality={modality!r}, family={family!r}, "
            f"model={model!r}; found={len(profiles)}."
        )
    profile = catalog.get_profile(str(profiles[0]["profile_id"]))
    interfaces = [
        catalog.get_interface(str(interface_id))
        for interface_id in profile.get("interface_ids") or []
        if str(catalog.get_interface(str(interface_id)).get("api_form") or "")
        == api_form
    ]
    if contract_id:
        interfaces = [
            item for item in interfaces
            if contract_id in (item.get("contract_ids") or []) or any(
                policy.get("extension_type") == "model_test_policy"
                and str(policy.get("suite_family_id") or "") == family
                and contract_id in (policy.get("reference_contract_ids") or [])
                for policy in item.get("test_bindings") or [] if isinstance(policy, dict)
            )
        ]
    elif len(interfaces) > 1:
        # A recorded-only sibling must not shadow the existing runnable
        # default. Multiple runnable siblings still require exact selection.
        interfaces = [item for item in interfaces
                      if item.get("enabled", True) is True and item.get("executable", True) is True]
    if len(interfaces) != 1:
        raise ValueError(
            f"Expected one MPDB Interface for {profile['profile_id']!r} and "
            f"api_form={api_form!r}; found={len(interfaces)}."
        )
    canonical_family = str(profile.get("family_id") or "")
    _validate_catalog_binding(
        profile,
        interfaces[0],
        source_id=source_id,
        reference_model_id=model,
        canonical_family_id=canonical_family,
        api_form=api_form,
        modality=modality,
        runtime_model_id=runtime_model_id,
    )
    return profile, interfaces[0], canonical_family


def resolve_runtime_profile_binding(
    config: dict[str, Any],
    provider: str,
    model: str,
    family: str,
    route_profile: str,
    api_form: str,
    *,
    modality: str = "text",
) -> dict[str, Any]:
    catalog = get_model_profile_catalog()
    provider_cfg = get_provider_config(config, provider)
    if modality == "image":
        configured_target = get_image_model_config(
            config,
            provider,
            model,
            route_profile=route_profile,
            api_form=api_form,
        )
        configured_family = str(configured_target.get("family") or "")
        if str(family) != configured_family:
            raise ValueError(
                f"Runtime family {family!r} conflicts with image binding "
                f"{configured_family!r}."
            )
        if str(configured_target.get("route_profile") or "") != str(route_profile):
            raise ValueError("Runtime image route conflicts with provider binding.")
        if str(configured_target.get("api_form") or "") != str(api_form):
            raise ValueError("Runtime image API form conflicts with provider binding.")
    else:
        configured_family = get_model_family(config, model, provider)
        if str(family) != str(configured_family):
            raise ValueError(
                f"Runtime family {family!r} conflicts with provider binding "
                f"{configured_family!r}."
            )
        configured_routes = get_model_route_profiles(config, model, provider)
        if str(route_profile) not in configured_routes:
            raise ValueError(
                f"Runtime route {route_profile!r} is not configured for "
                f"{provider}/{model}."
            )
        configured_forms = get_model_api_forms(
            config, model, provider, route_profile=route_profile
        )
        if str(api_form) not in configured_forms:
            raise ValueError(
                f"Runtime API form {api_form!r} is not configured for "
                f"{provider}/{model}/{route_profile}."
            )
    source_id, source_inferred = _configured_source_id(
        provider_cfg, model, route_profile
    )
    reference_model_id, reference_model_inferred = _reference_model_id(
        provider_cfg, model, modality=modality
    )
    if not source_id:
        source_id = _source_for_family_route(
            family,
            route_profile,
            model=reference_model_id,
            modality=modality,
            api_form=api_form,
        )
    configured = _configured_binding(
        config,
        provider,
        model,
        modality=modality,
        route_profile=route_profile,
        api_form=api_form,
    )
    if configured:
        profile = catalog.get_profile(configured["profile_id"])
        interface = catalog.get_interface(configured["interface_id"])
        canonical_family = str(profile.get("family_id") or "")
        _validate_catalog_binding(
            profile,
            interface,
            source_id=source_id,
            reference_model_id=reference_model_id,
            canonical_family_id=canonical_family,
            api_form=api_form,
            modality=modality,
            runtime_model_id=model,
        )
        binding_source = "configured_reference"
    else:
        try:
            profile, interface, canonical_family = _resolve_exact_profile_interface(
                source_id=source_id,
                modality=modality,
                family=family,
                model=reference_model_id,
                api_form=api_form,
                runtime_model_id=model,
            )
        except (KeyError, ValueError):
            return {
                **catalog_metadata(),
                "source_id": source_id or None,
                "suite_family_id": family,
                "canonical_family_id": family,
                "reference_model_id": reference_model_id,
                "profile_id": None,
                "interface_id": None,
                "binding_source": "unresolved_official_reference",
                "catalog_resolved": False,
                "reference_unresolved": True,
                "execution_target": {
                    "provider_id": str(provider_cfg.get("name") or provider),
                    "request_model_id": model,
                    "route_profile": route_profile,
                    "api_form": api_form,
                },
            }
        binding_source = "catalog_official_reference"
    return {
        **catalog_metadata(),
        "source_id": source_id,
        "source_id_inferred": source_inferred,
        "reference_model_id": reference_model_id,
        "reference_model_id_inferred": reference_model_inferred,
        "suite_family_id": family,
        "canonical_family_id": canonical_family,
        "compatibility_family_alias": family != canonical_family,
        "profile_id": profile["profile_id"],
        "interface_id": interface["interface_id"],
        "binding_source": binding_source,
        "catalog_resolved": True,
        "reference_unresolved": False,
        "profile": profile,
        "interface": interface,
        "execution_target": {
            "provider_id": str(provider_cfg.get("name") or provider),
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": api_form,
        },
    }


def require_official_reference_binding(binding: dict[str, Any]) -> dict[str, Any]:
    if (
        binding.get("catalog_resolved") is True
        and binding.get("source_id")
        and binding.get("profile_id")
        and binding.get("interface_id")
    ):
        return binding
    target = binding.get("execution_target") or {}
    raise ValueError(
        "No approved official MPDB binding for runtime target "
        f"{target.get('provider_id') or 'unknown'}/"
        f"{target.get('request_model_id') or 'unknown'}; "
        f"source={binding.get('source_id') or 'unresolved'!r}."
    )


def resolve_test_binding_id(
    binding: dict[str, Any], extension_type: str = "model_test_policy"
) -> str | None:
    interface = binding.get("interface")
    if not isinstance(interface, dict):
        return None
    suite_family = str(binding.get("suite_family_id") or "")
    canonical_family = str(binding.get("canonical_family_id") or suite_family)
    candidates = [
        row
        for row in interface.get("test_bindings") or []
        if isinstance(row, dict) and row.get("extension_type") == extension_type
    ]
    explicit = str(binding.get("test_binding_id") or "")
    if explicit:
        return explicit if any(
            str(row.get("test_binding_id") or "") == explicit for row in candidates
        ) else None
    matches = [
        row
        for row in candidates
        if str(row.get("suite_family_id") or canonical_family) == suite_family
    ]
    if len(matches) != 1:
        return None
    return str(matches[0].get("test_binding_id") or "") or None


def resolve_runtime_test_policy(
    binding: dict[str, Any], *, test_binding_id: str | None = None
) -> dict[str, Any]:
    require_official_reference_binding(binding)
    interface = binding.get("interface")
    if not isinstance(interface, dict):
        raise ValueError("MPDB binding has no Interface snapshot.")
    selected_id = str(test_binding_id or resolve_test_binding_id(binding) or "")
    candidates = [
        row
        for row in interface.get("test_bindings") or []
        if isinstance(row, dict)
        and row.get("extension_type") == "model_test_policy"
        and (not selected_id or str(row.get("test_binding_id") or "") == selected_id)
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected one model_test_policy for Interface "
            f"{binding.get('interface_id')!r}; found={len(candidates)}."
        )
    policy = copy.deepcopy(candidates[0])
    if str(policy.get("interface_id") or "") != str(binding.get("interface_id") or ""):
        raise ValueError("MPDB Test Binding belongs to another Interface.")
    expected_suite = str(binding.get("suite_family_id") or "")
    if str(policy.get("suite_family_id") or "") != expected_suite:
        raise ValueError("MPDB Test Binding belongs to another suite family.")
    return policy


def _reference_contract_for_policy(
    binding: dict[str, Any],
    policy: dict[str, Any],
    requested_contract_id: str | None = None,
) -> dict[str, Any]:
    contract_ids = [str(value) for value in policy.get("reference_contract_ids") or []]
    contract_id = str(
        requested_contract_id
        or policy.get("default_reference_contract_id")
        or policy.get("contract_id")
        or ""
    )
    if not contract_id or contract_id not in contract_ids:
        raise ValueError("MPDB Test Binding has no valid source-local Contract.")
    contract = get_model_profile_catalog().get_contract(contract_id)
    expected_source = str(binding.get("source_id") or "")
    expected_api_form = str((binding.get("interface") or {}).get("api_form") or "")
    expected_family = str(policy.get("suite_family_id") or "")
    mismatches: list[str] = []
    try:
        contract_source = _contract_source_id(contract)
    except ValueError:
        contract_source = ""
    if contract_source != expected_source:
        mismatches.append("source_id")
    if str(contract.get("api_form") or "") != expected_api_form:
        mismatches.append("api_form")
    if str(contract.get("family_id") or "") != expected_family:
        mismatches.append("family_id")
    if str(contract.get("test_binding_status") or "required") != "required":
        mismatches.append("test_binding_status")
    if mismatches:
        raise ValueError(
            f"MPDB Contract {contract_id!r} crosses binding identity: "
            + ", ".join(mismatches)
        )
    return contract


def resolve_runtime_parameter_config(
    config: dict[str, Any],
    provider: str,
    model: str,
    family: str,
    route_profile: str,
    api_form: str,
    *,
    modality: str = "text",
    test_binding_id: str | None = None,
    contract_id: str | None = None,
) -> dict[str, Any]:
    binding = resolve_runtime_profile_binding(
        config,
        provider,
        model,
        family,
        route_profile,
        api_form,
        modality=modality,
    )
    require_official_reference_binding(binding)
    policy = resolve_runtime_test_policy(binding, test_binding_id=test_binding_id)
    profile = binding["profile"]
    interface = binding["interface"]
    if modality == "text":
        selected = get_model_profile_catalog().resolve_parameter_config(
            source_id=str(binding["source_id"]),
            modality=str(profile["modality"]),
            family_id=str(profile["family_id"]),
            model_slug=str(profile["model_slug"]),
            interface_id=str(interface["interface_id"]),
            api_form=str(interface["api_form"]),
            test_binding_id=str(policy["test_binding_id"]),
            contract_id=contract_id,
            execution_target=copy.deepcopy(binding["execution_target"]),
        )
    else:
        # Image suites use the same Profile/Interface/model-policy extension,
        # but intentionally do not opt into the text parameter-test boolean.
        contract = _reference_contract_for_policy(binding, policy, contract_id)
        candidates = get_model_profile_catalog().list_test_bindings(
            contract_id=str(contract["contract_id"]), extension_type="parameter"
        )
        if len(candidates) > 1:
            raise ValueError(
                f"Expected at most one parameter Test Binding for image Contract "
                f"{contract['contract_id']!r}; found={len(candidates)}."
            )
        selected = {
            "source_id": str(binding["source_id"]),
            "profile_id": str(profile["profile_id"]),
            "interface_id": str(interface["interface_id"]),
            "test_binding_id": str(policy["test_binding_id"]),
            "contract_id": str(contract["contract_id"]),
            "profile": copy.deepcopy(profile),
            "interface": copy.deepcopy(interface),
            "test_binding": copy.deepcopy(policy),
            "contract": copy.deepcopy(contract),
            "parameter_test_binding": (
                copy.deepcopy(candidates[0]) if candidates else None
            ),
            "execution_target": copy.deepcopy(binding["execution_target"]),
        }
    selected["model_profile_database"] = database_snapshot(
        binding, parameter_config=selected
    )
    return selected


def _binding_for_catalog_selection(
    modality: str,
    family: str,
    model: str,
    *,
    api_form: str | None,
    route_profile: str | None,
    reference_contract_id: str | None,
    execution_target: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = get_model_profile_catalog()
    contract: dict[str, Any] | None = None
    source_id = ""
    if reference_contract_id:
        try:
            source = catalog.get_source(reference_contract_id)
        except KeyError:
            contract = catalog.get_contract(reference_contract_id)
            source_id = _contract_source_id(contract)
        else:
            source_id = str(source["source_id"])
    if not source_id:
        source_id = _source_for_family_route(
            family,
            str(route_profile or ""),
            model=model,
            modality=modality,
            api_form=api_form,
        )
    if not source_id:
        raise ValueError(
            f"Cannot resolve an official source for {modality}/{family}/{model}."
        )
    selected_api_form = str(
        api_form
        or (contract or {}).get("api_form")
        or DEFAULT_API_FORM_BY_FAMILY.get(family)
        or ""
    )
    if not selected_api_form:
        raise ValueError("api_form is required for MPDB selection.")
    profile, interface, canonical_family = _resolve_exact_profile_interface(
        source_id=source_id,
        modality=modality,
        family=family,
        model=model,
        api_form=selected_api_form,
        contract_id=str((contract or {}).get("contract_id") or "") or None,
    )
    binding = {
        **catalog_metadata(),
        "source_id": source_id,
        "reference_model_id": model,
        "suite_family_id": family,
        "canonical_family_id": canonical_family,
        "compatibility_family_alias": family != canonical_family,
        "profile_id": profile["profile_id"],
        "interface_id": interface["interface_id"],
        "binding_source": "catalog_source_first",
        "catalog_resolved": True,
        "profile": profile,
        "interface": interface,
        "execution_target": copy.deepcopy(execution_target) if execution_target else {
            "provider_id": None,
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": selected_api_form,
        },
    }
    policy = resolve_runtime_test_policy(binding)
    resolved_contract = _reference_contract_for_policy(
        binding, policy, reference_contract_id if contract is not None else None
    )
    return binding, resolved_contract


def catalog_capability_profile(
    modality: str,
    family: str,
    model: str,
    *,
    api_form: str | None = None,
    route_profile: str | None = None,
    reference_contract_id: str | None = None,
    provider_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if str(modality).strip().casefold() in {"image", "video"}:
        override = provider_override or {}
        for field in (
            "pressure_test_enabled",
            "test_policy_pressure_test_enabled",
        ):
            if field in override and override[field] is not False:
                raise ValueError(
                    f"{modality}/{family}/{model} cannot enable pressure testing; "
                    f"{field} must be false for image/video modalities."
                )
        populated = [
            field
            for field in (
                "pressure_profiles",
                "pressure_omit_params",
                "pressure_parameter_aliases",
                "pressure_overrides",
                "pressure_transport_overrides",
            )
            if override.get(field)
        ]
        if populated:
            raise ValueError(
                f"{modality}/{family}/{model} cannot define pressure policy "
                f"fields: {sorted(populated)}."
            )
    binding, contract = _binding_for_catalog_selection(
        modality,
        family,
        model,
        api_form=api_form,
        route_profile=route_profile,
        reference_contract_id=reference_contract_id,
    )
    policy = resolve_runtime_test_policy(binding)
    interface = binding["interface"]
    contract_id = str(contract["contract_id"])
    allowed_contracts = [
        str(value) for value in policy.get("reference_contract_ids") or []
    ]
    parameter_bindings = get_model_profile_catalog().list_test_bindings(
        contract_id=contract_id,
        extension_type="parameter",
    )
    parameter_binding = parameter_bindings[0] if len(parameter_bindings) == 1 else None
    parameter_enabled = bool(
        policy.get("parameter_test_enabled") is True
        and parameter_binding is not None
        and parameter_binding.get("test_cases")
    )
    pressure_enabled = bool(
        modality == "text" and policy.get("pressure_test_enabled") is True
    )
    contract_status = str(contract.get("test_binding_status") or "required")
    reference_executable = contract_status == "required"
    transport = str(interface.get("transport_adapter_id") or "")
    reference_routing_mode = str(interface.get("routing_mode") or "")
    runtime_route_profile = str(route_profile or reference_routing_mode)
    runtime_is_adapter = bool(
        runtime_route_profile and runtime_route_profile != reference_routing_mode
    )
    profile = binding["profile"]
    execution_target = copy.deepcopy(binding["execution_target"])
    execution_target.update(
        {
            "request_model_id": model,
            "route_profile": runtime_route_profile,
            "api_form": str(interface.get("api_form") or ""),
            "transport": transport,
        }
    )
    snapshot = database_snapshot(binding)
    return {
        "storage_source": "model_profile_database",
        "legacy_yaml_read": False,
        "modality": modality,
        "family": family,
        "suite_family_id": family,
        "canonical_family_id": binding["canonical_family_id"],
        "model": model,
        "canonical_model_slug": str(profile.get("model_slug") or ""),
        "profile_id": str(binding["profile_id"]),
        "model_api_profile_id": binding["interface_id"],
        "interface_id": binding["interface_id"],
        "test_binding_id": str(policy.get("test_binding_id") or ""),
        "parameter_test_binding_id": (
            str(parameter_binding.get("test_binding_id") or "")
            if parameter_binding
            else None
        ),
        "api_form": interface["api_form"],
        "transport": transport,
        "route_profile": runtime_route_profile,
        "runtime_route_profile": runtime_route_profile,
        "reference_routing_mode": reference_routing_mode,
        "route_profile_known": True,
        "source_id": str(binding["source_id"]),
        "reference_contract_id": contract_id,
        "reference_identity": {
            "source_id": str(binding["source_id"]),
            "profile_id": str(binding["profile_id"]),
            "interface_id": str(binding["interface_id"]),
            "test_binding_id": str(policy.get("test_binding_id") or ""),
            "reference_contract_id": contract_id,
            "parameter_test_binding_id": (
                str(parameter_binding.get("test_binding_id") or "")
                if parameter_binding
                else None
            ),
        },
        "reference_source": contract_id,
        "default_reference_contract_id": contract_id,
        "default_reference_source": contract_id,
        "comparison_reference_source": contract_id,
        "reference_sources": {transport: contract_id} if transport else {},
        "allowed_reference_contract_ids": allowed_contracts,
        "allowed_reference_sources": allowed_contracts,
        "alternate_sources": [
            value
            for value in allowed_contracts
            if str(value) != contract_id
        ],
        "reference_source_enabled": reference_executable,
        "reference_source_executable": reference_executable,
        "reference_source_profile_eligible": reference_executable,
        "reference_source_test_binding_status": contract_status,
        "suite": policy.get("suite"),
        "default_expectation": "supported",
        "default_expectations": copy.deepcopy(
            policy.get("default_expectations") or {}
        ),
        "model_expectations": copy.deepcopy(policy.get("model_expectations") or {}),
        "expectations": copy.deepcopy(policy.get("expectations") or {}),
        "default_parameter_expectations": copy.deepcopy(
            policy.get("default_parameter_expectations") or {}
        ),
        "model_parameter_expectations": copy.deepcopy(
            policy.get("model_parameter_expectations") or {}
        ),
        "parameter_expectations": copy.deepcopy(
            policy.get("parameter_expectations") or {}
        ),
        "pressure_profiles": copy.deepcopy(policy.get("pressure_profiles") or {}),
        "pressure_omit_params": list(policy.get("pressure_omit_params") or []),
        "pressure_parameter_aliases": copy.deepcopy(
            policy.get("pressure_parameter_aliases") or {}
        ),
        "pressure_overrides": copy.deepcopy(policy.get("pressure_overrides") or {}),
        "pressure_transport_overrides": copy.deepcopy(
            policy.get("pressure_transport_overrides") or {}
        ),
        "parameter_test_enabled": parameter_enabled,
        "pressure_test_enabled": pressure_enabled,
        "test_policy_parameter_test_enabled": policy.get(
            "parameter_test_enabled"
        ),
        "test_policy_pressure_test_enabled": policy.get("pressure_test_enabled"),
        "enabled": True,
        "executable": True,
        "disabled_reason": policy.get("disabled_reason"),
        "known_model": True,
        "known_api_form": True,
        "known_api_profile": True,
        "profile_status": "registered",
        "evidence": interface.get("evidence") or profile.get("evidence"),
        "certification_scope": (
            "adapter_only"
            if runtime_is_adapter
            else interface.get("certification_scope")
        ),
        "reference_certification_scope": interface.get("certification_scope"),
        "route_stability_required": bool(
            runtime_is_adapter or interface.get("route_stability_required", False)
        ),
        "identity": copy.deepcopy(interface.get("identity") or {}),
        "response_validators": list(interface.get("response_validators") or []),
        "usage_schema": copy.deepcopy(interface.get("usage_schema") or {}),
        "cache_policy": copy.deepcopy(interface.get("cache_policy") or {}),
        "parameter_constraints": copy.deepcopy(
            interface.get("parameter_constraints") or {}
        ),
        "source_conflicts": copy.deepcopy(interface.get("source_conflicts") or {}),
        "request_model_ids": list(profile.get("request_model_ids") or []),
        "default_api_version": interface.get("default_api_version"),
        "api_versions": copy.deepcopy(interface.get("api_versions") or {}),
        "validation_api_version": policy.get("api_version"),
        "image_case_expectations": copy.deepcopy(policy.get("image_case_expectations") or {}),
        "validation_date": policy.get("validation_date"),
        "execution_target": execution_target,
        "execution_target_boundary": {
            "included": True,
            "used_for_reference_resolution": False,
        },
        "test_binding_status": interface.get("test_binding_status", "required"),
        "interface_test_binding_status": interface.get(
            "test_binding_status", "required"
        ),
        "test_scope": policy.get("context") or policy.get("test_scope"),
        "model_profile_database": snapshot,
    }


def reference_contract_payload(contract_id: str) -> dict[str, Any]:
    catalog = get_model_profile_catalog()
    contract = catalog.get_contract(contract_id)
    source_id = _contract_source_id(contract)
    parameter_bindings = catalog.list_test_bindings(
        contract_id=contract_id, extension_type="parameter"
    )
    parameter_binding = parameter_bindings[0] if len(parameter_bindings) == 1 else {}
    test_profiles = list(parameter_binding.get("test_cases") or [])
    coverage = parameter_binding.get("parameter_coverage") or {}
    params: dict[str, Any] = {}
    for name, raw in (contract.get("parameter_capabilities") or {}).items():
        row = raw if isinstance(raw, dict) else {}
        state = str(row.get("state") or "supported")
        params[str(name)] = {
            "required": bool(row.get("required", False)),
            "supported": state != "unsupported",
            "coverage": str(coverage.get(name) or "not tested"),
        }
    eligible_interfaces = _eligible_contract_interfaces(catalog, contract)
    policies = [
        binding
        for interface in eligible_interfaces
        for binding in interface.get("test_bindings") or []
        if isinstance(binding, dict)
        and binding.get("extension_type") == "model_test_policy"
        and contract_id in (binding.get("reference_contract_ids") or [])
    ]
    parameter_enabled = any(
        binding.get("parameter_test_enabled") is True for binding in policies
    )
    pressure_enabled = any(
        binding.get("pressure_test_enabled") is True for binding in policies
    )
    required = str(contract.get("test_binding_status") or "required") == "required"
    executable = required and bool(eligible_interfaces)
    parameter_binding_runnable = bool(parameter_binding and test_profiles)
    parameter_enabled = (
        executable and parameter_binding_runnable and parameter_enabled
    )
    pressure_enabled = executable and pressure_enabled
    return {
        "id": contract_id,
        "label": str(contract.get("label") or contract_id),
        "official_sources": list(contract.get("official_sources") or []),
        "families": [str(contract.get("family_id") or "")],
        "default_for_families": [str(contract.get("family_id") or "")],
        "model_family": str(contract.get("family_id") or ""),
        "api_form": str(contract.get("api_form") or ""),
        "route_profile": str(contract.get("routing_mode") or ""),
        "certification_scope": str(
            next(
                (
                    binding.get("certification_scope")
                    for binding in policies
                    if binding.get("certification_scope")
                ),
                "raw_route_contract",
            )
        ),
        "route_stability_required": False,
        "profile_eligible": executable,
        "catalog_eligible": True,
        "enabled": executable,
        "executable": executable,
        "parameter_test_enabled": parameter_enabled,
        "pressure_test_enabled": pressure_enabled,
        "test_binding_status": contract.get("test_binding_status", "required"),
        "disabled_reason": (
            ""
            if executable
            else "Contract is not an exact executable MPDB reference binding"
        ),
        "test_profiles": test_profiles,
        "params": params,
        "evidence": contract.get("evidence") or contract.get("verification_status"),
        "source_id": source_id,
        "contract_id": contract_id,
    }


def default_reference_contract_for_family(
    family: str,
    *,
    route_profile: str | None = None,
    api_form: str | None = None,
) -> str:
    """Select the package's first origin Contract for a family/API view."""

    family_id = str(family).strip()
    route = str(route_profile or "").strip()
    selected_form = str(
        api_form or DEFAULT_API_FORM_BY_FAMILY.get(family_id) or ""
    )
    source_id = _source_for_family_route(
        family_id,
        route,
        modality="text",
        api_form=selected_form,
    )
    if not source_id or not selected_form:
        raise RuntimeError(
            f"No reference source configured in MPDB for family={family_id!r}."
        )
    candidates: list[str] = []
    for contract in get_model_profile_catalog().list_contracts():
        if str(contract.get("family_id") or "") != family_id:
            continue
        if _contract_source_id(contract) != source_id:
            continue
        if str(contract.get("api_form") or "") != selected_form:
            continue
        contract_route = str(contract.get("routing_mode") or "")
        if route and route != "dynamic_aggregator" and contract_route != route:
            continue
        if str(contract.get("test_binding_status") or "required") != "required":
            continue
        payload = reference_contract_payload(str(contract["contract_id"]))
        if payload["params"] and payload["test_profiles"]:
            candidates.append(str(contract["contract_id"]))
    if not candidates:
        raise RuntimeError(
            f"No reference source configured in MPDB for family={family_id!r}, "
            f"route_profile={route or None!r}, api_form={selected_form!r}."
        )
    return candidates[0]


@lru_cache(maxsize=1)
def _reference_specs_projection_cached() -> dict[str, Any]:
    catalog = get_model_profile_catalog()
    sources: dict[str, Any] = {}
    for contract in catalog.list_contracts():
        contract_id = str(contract["contract_id"])
        payload = reference_contract_payload(contract_id)
        # The compatibility facade represents executable test matrices, not
        # every descriptive Contract.  A Contract without both a parameter
        # binding and cases remains available through the package API but must
        # not masquerade as a runnable legacy reference source.
        if payload["test_profiles"] and payload["params"]:
            sources[contract_id] = {
                key: copy.deepcopy(value)
                for key, value in payload.items()
                if key != "id"
            }
    return {
        "schema_version": 1,
        "projection_source": "yibu-model-profile-db",
        **catalog_metadata(),
        "reference_sources": sources,
    }


def reference_specs_projection() -> dict[str, Any]:
    return copy.deepcopy(_reference_specs_projection_cached())


@lru_cache(maxsize=1)
def _capability_profiles_projection_cached() -> dict[str, Any]:
    """Project only executable MPDB identities into the temporary read facade."""

    catalog = get_model_profile_catalog()
    modalities: dict[str, Any] = {
        str(row["modality"]): {
            "families": {},
            **(
                {"pressure_test_enabled": False}
                if row.get("pressure_test_enabled") is False
                else {}
            ),
        }
        for row in catalog.list_modalities()
    }
    for raw_profile in catalog.list_profiles():
        profile = catalog.get_profile(str(raw_profile["profile_id"]))
        if str(profile.get("profile_state") or "executable") != "executable":
            continue
        modality = str(profile.get("modality") or "")
        model = str(profile.get("model_slug") or "")
        canonical_family = str(profile.get("family_id") or "")
        for interface_id in profile.get("interface_ids") or []:
            interface = catalog.get_interface(str(interface_id))
            if (
                interface.get("enabled", True) is not True
                or interface.get("executable", True) is not True
                or str(interface.get("test_binding_status") or "required")
                != "required"
            ):
                continue
            valid_contract_ids = {
                str(contract_id)
                for contract_id, contract in (interface.get("contracts") or {}).items()
                if str(contract.get("test_binding_status") or "required")
                == "required"
                and _contract_source_id(contract)
                == str(profile.get("source_id") or "")
            }
            policies = [
                row
                for row in interface.get("test_bindings") or []
                if isinstance(row, dict)
                and row.get("extension_type") == "model_test_policy"
                and str(row.get("default_reference_contract_id") or "")
                in valid_contract_ids
            ]
            for policy in policies:
                suite_family = str(
                    policy.get("suite_family_id") or canonical_family
                )
                family_node = (
                    modalities.setdefault(modality, {"families": {}})["families"]
                    .setdefault(
                        suite_family,
                        {"canonical_models": {}, "route_profiles": {}},
                    )
                )
                model_node = family_node["canonical_models"].setdefault(
                    model,
                    {
                        "canonical_family_id": canonical_family,
                        "aliases": [],
                        "profile_ids": [],
                        "request_model_ids": [],
                    },
                )
                model_node["profile_ids"].append(str(profile["profile_id"]))
                model_node["request_model_ids"] = list(
                    dict.fromkeys(
                        [
                            *model_node["request_model_ids"],
                            *[
                                str(value)
                                for value in profile.get("request_model_ids") or []
                            ],
                        ]
                    )
                )
                route = str(interface.get("routing_mode") or "")
                api_form = str(interface.get("api_form") or "")
                route_node = family_node["route_profiles"].setdefault(
                    route,
                    {"api_forms": {}},
                )
                form_node = route_node["api_forms"].setdefault(
                    api_form,
                    {
                        "transport": str(
                            interface.get("transport_adapter_id") or ""
                        ),
                        "reference_sources": list(
                            policy.get("reference_contract_ids") or []
                        ),
                        "default_reference_source": policy.get(
                            "default_reference_contract_id"
                        ),
                        "model_profiles": {},
                    },
                )
                form_node["model_profiles"][model] = {
                    "profile_id": str(profile["profile_id"]),
                    "source_id": str(profile.get("source_id") or ""),
                    "interface_id": str(interface["interface_id"]),
                    "test_binding_id": str(policy["test_binding_id"]),
                    "reference_sources": list(
                        policy.get("reference_contract_ids") or []
                    ),
                    "suite": policy.get("suite"),
                    "parameter_test_enabled": policy.get(
                        "parameter_test_enabled", False
                    ),
                    "pressure_test_enabled": policy.get(
                        "pressure_test_enabled", False
                    ),
                    "expectations": copy.deepcopy(policy.get("expectations") or {}),
                }
    return {
        "schema_version": 4,
        "projection_source": "yibu-model-profile-db",
        **catalog_metadata(),
        "modalities": modalities,
    }


def capability_profiles_projection() -> dict[str, Any]:
    return copy.deepcopy(_capability_profiles_projection_cached())


def database_snapshot(
    binding: dict[str, Any],
    *,
    parameter_config: dict[str, Any] | None = None,
    include_parameter_binding: bool = True,
) -> dict[str, Any]:
    require_official_reference_binding(binding)
    profile = copy.deepcopy(binding.get("profile"))
    interface = copy.deepcopy(binding.get("interface"))
    if not isinstance(profile, dict) or not isinstance(interface, dict):
        raise ValueError("MPDB binding is missing Profile or Interface payload.")
    policy = (
        copy.deepcopy(parameter_config.get("test_binding"))
        if isinstance(parameter_config, dict)
        and isinstance(parameter_config.get("test_binding"), dict)
        else resolve_runtime_test_policy(binding)
    )
    contract = (
        copy.deepcopy(parameter_config.get("contract"))
        if isinstance(parameter_config, dict)
        and isinstance(parameter_config.get("contract"), dict)
        else _reference_contract_for_policy(binding, policy)
    )
    parameter_binding = (
        copy.deepcopy(parameter_config.get("parameter_test_binding"))
        if isinstance(parameter_config, dict)
        and isinstance(parameter_config.get("parameter_test_binding"), dict)
        else None
    )
    if not include_parameter_binding:
        parameter_binding = None
    elif parameter_binding is None:
        candidates = get_model_profile_catalog().list_test_bindings(
            contract_id=str(contract["contract_id"]), extension_type="parameter"
        )
        if len(candidates) > 1 or (
            policy.get("parameter_test_enabled") is True
            and str(profile.get("modality") or "") == "text"
            and len(candidates) != 1
        ):
            raise ValueError(
                f"Expected one unambiguous parameter Test Binding for Contract "
                f"{contract['contract_id']!r}; found={len(candidates)}."
            )
        parameter_binding = candidates[0] if candidates else None
    metadata = catalog_metadata()
    snapshot = {
        "snapshot_schema_version": 1,
        **{
            key: copy.deepcopy(metadata[key])
            for key in (
                "package_name",
                "package_version",
                "mpdb_schema_version",
                "catalog_version",
                "catalog_digest",
                "test_extension_schema_version",
                "test_extension_digest",
                "identity_contract",
            )
        },
        "source_id": str(binding["source_id"]),
        "modality": str(profile["modality"]),
        "family_id": str(profile["family_id"]),
        "suite_family_id": str(binding.get("suite_family_id") or profile["family_id"]),
        "model_slug": str(profile["model_slug"]),
        "profile_id": str(profile["profile_id"]),
        "interface_id": str(interface["interface_id"]),
        "api_form": str(interface["api_form"]),
        "test_binding_id": str(policy["test_binding_id"]),
        "reference_contract_id": str(contract["contract_id"]),
        "parameter_test_binding_id": (
            str(parameter_binding["test_binding_id"])
            if isinstance(parameter_binding, dict)
            else None
        ),
        "profile": profile,
        "interface": interface,
        "test_binding": policy,
        "reference_contract": contract,
        "parameter_test_binding": parameter_binding,
        "execution_target": copy.deepcopy(binding.get("execution_target") or {}),
        "binding_source": binding.get("binding_source"),
        "catalog_resolved": True,
    }
    snapshot["snapshot_digest"] = hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return snapshot


def _require_snapshot_value(snapshot: dict[str, Any], field: str) -> str:
    value = str(snapshot.get(field) or "")
    if not value:
        raise ValueError(f"MPDB snapshot is missing {field}.")
    return value


def binding_from_database_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate and reuse only the immutable snapshot; never query today's catalog."""

    if not isinstance(snapshot, dict):
        raise ValueError("MPDB snapshot must be an object.")
    source_id = _require_snapshot_value(snapshot, "source_id")
    profile_id = _require_snapshot_value(snapshot, "profile_id")
    interface_id = _require_snapshot_value(snapshot, "interface_id")
    profile = snapshot.get("profile")
    interface = snapshot.get("interface")
    if not isinstance(profile, dict) or not isinstance(interface, dict):
        raise ValueError("MPDB snapshot requires immutable Profile and Interface payloads.")
    expected_profile = {
        "profile_id": profile_id,
        "source_id": source_id,
        "modality": _require_snapshot_value(snapshot, "modality"),
        "family_id": _require_snapshot_value(snapshot, "family_id"),
        "model_slug": _require_snapshot_value(snapshot, "model_slug"),
    }
    mismatches = [
        f"profile.{field}"
        for field, expected in expected_profile.items()
        if str(profile.get(field) or "") != expected
    ]
    expected_interface = {
        "interface_id": interface_id,
        "profile_id": profile_id,
        "source_id": source_id,
        "modality": expected_profile["modality"],
        "family_id": expected_profile["family_id"],
        "model_slug": expected_profile["model_slug"],
        "api_form": _require_snapshot_value(snapshot, "api_form"),
    }
    mismatches.extend(
        f"interface.{field}"
        for field, expected in expected_interface.items()
        if str(interface.get(field) or "") != expected
    )
    if interface_id not in (profile.get("interface_ids") or []):
        mismatches.append("profile.interface_ids")
    if str(profile.get("profile_state") or "executable") != "executable":
        mismatches.append("profile.profile_state")
    if interface.get("enabled", True) is not True:
        mismatches.append("interface.enabled")
    if interface.get("executable", True) is not True:
        mismatches.append("interface.executable")
    if mismatches:
        raise ValueError(
            "MPDB snapshot Profile/Interface identity conflict: "
            + ", ".join(sorted(set(mismatches)))
        )

    snapshot_version = int(snapshot.get("snapshot_schema_version") or 0)
    policy = snapshot.get("test_binding")
    contract = snapshot.get("reference_contract")
    parameter_binding = snapshot.get("parameter_test_binding")
    if snapshot_version >= 1:
        for digest_field in (
            "package_version",
            "catalog_version",
            "catalog_digest",
            "test_extension_digest",
        ):
            _require_snapshot_value(snapshot, digest_field)
        snapshot_digest = _require_snapshot_value(snapshot, "snapshot_digest")
        digest_payload = copy.deepcopy(snapshot)
        digest_payload.pop("snapshot_digest", None)
        actual_snapshot_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if actual_snapshot_digest != snapshot_digest:
            raise ValueError("MPDB snapshot digest does not match its payload.")
        if not isinstance(policy, dict) or not isinstance(contract, dict):
            raise ValueError(
                "MPDB v1 snapshot requires Test Binding and Contract payloads."
            )
        test_binding_id = _require_snapshot_value(snapshot, "test_binding_id")
        contract_id = _require_snapshot_value(snapshot, "reference_contract_id")
        parameter_binding_id = str(snapshot.get("parameter_test_binding_id") or "")
        suite_family = _require_snapshot_value(snapshot, "suite_family_id")
        policy_mismatches = []
        if str(policy.get("test_binding_id") or "") != test_binding_id:
            policy_mismatches.append("test_binding_id")
        if str(policy.get("interface_id") or "") != interface_id:
            policy_mismatches.append("interface_id")
        if str(policy.get("extension_type") or "") != "model_test_policy":
            policy_mismatches.append("extension_type")
        if str(policy.get("suite_family_id") or "") != suite_family:
            policy_mismatches.append("suite_family_id")
        if contract_id not in (policy.get("reference_contract_ids") or []):
            policy_mismatches.append("reference_contract_ids")
        if str(contract.get("contract_id") or "") != contract_id:
            policy_mismatches.append("contract.contract_id")
        try:
            contract_source = _contract_source_id(contract)
        except ValueError:
            contract_source = ""
        if contract_source != source_id:
            policy_mismatches.append("contract.source_id")
        if str(contract.get("api_form") or "") != expected_interface["api_form"]:
            policy_mismatches.append("contract.api_form")
        if str(contract.get("family_id") or "") != suite_family:
            policy_mismatches.append("contract.family_id")
        if parameter_binding_id:
            if not isinstance(parameter_binding, dict):
                policy_mismatches.append("parameter_binding")
            elif str(parameter_binding.get("test_binding_id") or "") != parameter_binding_id:
                policy_mismatches.append("parameter_binding.test_binding_id")
            elif str(parameter_binding.get("extension_type") or "") != "parameter":
                policy_mismatches.append("parameter_binding.extension_type")
            elif str(parameter_binding.get("contract_id") or "") != contract_id:
                policy_mismatches.append("parameter_binding.contract_id")
        elif isinstance(parameter_binding, dict):
            policy_mismatches.append("parameter_test_binding_id")
        if policy_mismatches:
            raise ValueError(
                "MPDB snapshot binding identity conflict: "
                + ", ".join(sorted(set(policy_mismatches)))
            )
    return {
        "package_version": snapshot.get("package_version"),
        "mpdb_schema_version": snapshot.get("mpdb_schema_version"),
        "catalog_version": snapshot.get("catalog_version"),
        "catalog_digest": snapshot.get("catalog_digest"),
        "test_extension_digest": snapshot.get("test_extension_digest"),
        "source_id": source_id,
        "suite_family_id": snapshot.get("suite_family_id")
        or expected_profile["family_id"],
        "canonical_family_id": expected_profile["family_id"],
        "profile_id": profile_id,
        "interface_id": interface_id,
        "test_binding_id": snapshot.get("test_binding_id"),
        "reference_contract_id": snapshot.get("reference_contract_id"),
        "parameter_test_binding_id": snapshot.get("parameter_test_binding_id"),
        "profile": copy.deepcopy(profile),
        "interface": copy.deepcopy(interface),
        "test_binding": copy.deepcopy(policy),
        "reference_contract": copy.deepcopy(contract),
        "parameter_test_binding": copy.deepcopy(parameter_binding),
        "execution_target": copy.deepcopy(snapshot.get("execution_target") or {}),
        "binding_source": snapshot.get("binding_source") or "job_snapshot",
        "catalog_resolved": True,
        "snapshot_reused": True,
    }


def capability_profile_from_database_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Project one runtime capability from its validated immutable snapshot."""

    binding = binding_from_database_snapshot(snapshot)
    profile = binding["profile"]
    interface = binding["interface"]
    policy = binding["test_binding"]
    contract = binding["reference_contract"]
    parameter_binding = binding.get("parameter_test_binding")
    if not isinstance(policy, dict) or not isinstance(contract, dict):
        raise ValueError("MPDB snapshot is missing its Test Binding or Contract.")

    modality = str(profile.get("modality") or "")
    family = str(binding.get("suite_family_id") or profile.get("family_id") or "")
    canonical_family = str(profile.get("family_id") or "")
    contract_id = str(binding.get("reference_contract_id") or "")
    interface_id = str(binding.get("interface_id") or "")
    profile_id = str(binding.get("profile_id") or "")
    target = copy.deepcopy(binding.get("execution_target") or {})
    model = str(target.get("request_model_id") or profile.get("model_slug") or "")
    api_form = str(interface.get("api_form") or "")
    route_profile = str(
        target.get("route_profile") or interface.get("routing_mode") or ""
    )
    transport = str(interface.get("transport_adapter_id") or "")
    target_conflicts = [
        field
        for field, expected in (
            ("api_form", api_form),
            ("transport", transport),
        )
        if target.get(field) not in (None, "", expected)
    ]
    if target_conflicts:
        raise ValueError(
            "MPDB snapshot execution target conflicts with its Interface: "
            + ", ".join(target_conflicts)
        )
    target.update(
        {
            "request_model_id": model,
            "route_profile": route_profile,
            "api_form": api_form,
            "transport": transport,
        }
    )

    allowed_contracts = [
        str(value) for value in policy.get("reference_contract_ids") or []
    ]
    contract_status = str(contract.get("test_binding_status") or "required")
    reference_executable = contract_status == "required"
    parameter_enabled = bool(
        policy.get("parameter_test_enabled") is True
        and isinstance(parameter_binding, dict)
        and parameter_binding.get("test_cases")
    )
    pressure_enabled = bool(
        modality == "text" and policy.get("pressure_test_enabled") is True
    )
    reference_routing_mode = str(interface.get("routing_mode") or "")
    runtime_is_adapter = bool(
        route_profile and route_profile != reference_routing_mode
    )
    parameter_binding_id = (
        str(parameter_binding.get("test_binding_id") or "")
        if isinstance(parameter_binding, dict)
        else None
    )
    source_id = str(binding.get("source_id") or "")

    return {
        "storage_source": "model_profile_database",
        "legacy_yaml_read": False,
        "modality": modality,
        "family": family,
        "suite_family_id": family,
        "canonical_family_id": canonical_family,
        "model": model,
        "canonical_model_slug": str(profile.get("model_slug") or ""),
        "profile_id": profile_id,
        "model_api_profile_id": interface_id,
        "interface_id": interface_id,
        "test_binding_id": str(policy.get("test_binding_id") or ""),
        "parameter_test_binding_id": parameter_binding_id,
        "api_form": api_form,
        "transport": transport,
        "route_profile": route_profile,
        "runtime_route_profile": route_profile,
        "reference_routing_mode": reference_routing_mode,
        "route_profile_known": True,
        "source_id": source_id,
        "reference_contract_id": contract_id,
        "reference_identity": {
            "source_id": source_id,
            "profile_id": profile_id,
            "interface_id": interface_id,
            "test_binding_id": str(policy.get("test_binding_id") or ""),
            "reference_contract_id": contract_id,
            "parameter_test_binding_id": parameter_binding_id,
        },
        "reference_source": contract_id,
        "default_reference_contract_id": contract_id,
        "default_reference_source": contract_id,
        "comparison_reference_source": contract_id,
        "reference_sources": {transport: contract_id} if transport else {},
        "allowed_reference_contract_ids": allowed_contracts,
        "allowed_reference_sources": allowed_contracts,
        "alternate_sources": [
            value for value in allowed_contracts if value != contract_id
        ],
        "reference_source_enabled": reference_executable,
        "reference_source_executable": reference_executable,
        "reference_source_profile_eligible": reference_executable,
        "reference_source_test_binding_status": contract_status,
        "suite": policy.get("suite"),
        "default_expectation": "supported",
        "default_expectations": copy.deepcopy(
            policy.get("default_expectations") or {}
        ),
        "model_expectations": copy.deepcopy(policy.get("model_expectations") or {}),
        "expectations": copy.deepcopy(policy.get("expectations") or {}),
        "default_parameter_expectations": copy.deepcopy(
            policy.get("default_parameter_expectations") or {}
        ),
        "model_parameter_expectations": copy.deepcopy(
            policy.get("model_parameter_expectations") or {}
        ),
        "parameter_expectations": copy.deepcopy(
            policy.get("parameter_expectations") or {}
        ),
        "pressure_profiles": copy.deepcopy(policy.get("pressure_profiles") or {}),
        "pressure_omit_params": list(policy.get("pressure_omit_params") or []),
        "pressure_parameter_aliases": copy.deepcopy(
            policy.get("pressure_parameter_aliases") or {}
        ),
        "pressure_overrides": copy.deepcopy(policy.get("pressure_overrides") or {}),
        "pressure_transport_overrides": copy.deepcopy(
            policy.get("pressure_transport_overrides") or {}
        ),
        "parameter_test_enabled": parameter_enabled,
        "pressure_test_enabled": pressure_enabled,
        "test_policy_parameter_test_enabled": policy.get(
            "parameter_test_enabled"
        ),
        "test_policy_pressure_test_enabled": policy.get("pressure_test_enabled"),
        "enabled": True,
        "executable": True,
        "disabled_reason": policy.get("disabled_reason"),
        "known_model": True,
        "known_api_form": True,
        "known_api_profile": True,
        "profile_status": "registered",
        "evidence": interface.get("evidence") or profile.get("evidence"),
        "certification_scope": (
            "adapter_only"
            if runtime_is_adapter
            else interface.get("certification_scope")
        ),
        "reference_certification_scope": interface.get("certification_scope"),
        "route_stability_required": bool(
            runtime_is_adapter
            or interface.get("route_stability_required", False)
        ),
        "identity": copy.deepcopy(interface.get("identity") or {}),
        "response_validators": list(interface.get("response_validators") or []),
        "usage_schema": copy.deepcopy(interface.get("usage_schema") or {}),
        "cache_policy": copy.deepcopy(interface.get("cache_policy") or {}),
        "parameter_constraints": copy.deepcopy(
            interface.get("parameter_constraints") or {}
        ),
        "source_conflicts": copy.deepcopy(interface.get("source_conflicts") or {}),
        "request_model_ids": list(profile.get("request_model_ids") or []),
        "default_api_version": interface.get("default_api_version"),
        "api_versions": copy.deepcopy(interface.get("api_versions") or {}),
        "validation_api_version": policy.get("api_version"),
        "image_case_expectations": copy.deepcopy(policy.get("image_case_expectations") or {}),
        "validation_date": policy.get("validation_date"),
        "execution_target": target,
        "execution_target_boundary": {
            "included": True,
            "used_for_reference_resolution": False,
        },
        "test_binding_status": interface.get("test_binding_status", "required"),
        "interface_test_binding_status": interface.get(
            "test_binding_status", "required"
        ),
        "test_scope": policy.get("context") or policy.get("test_scope"),
        "profile": copy.deepcopy(profile),
        "interface": copy.deepcopy(interface),
        "test_binding": copy.deepcopy(policy),
        "reference_contract": copy.deepcopy(contract),
        "parameter_test_binding": copy.deepcopy(parameter_binding),
        "model_profile_database": copy.deepcopy(snapshot),
    }


def resolve_legacy_interface_id(legacy_id: str | None) -> str | None:
    if not legacy_id:
        return None
    try:
        return get_model_profile_catalog().resolve_legacy_id(str(legacy_id))
    except KeyError:
        return None


__all__ = [
    "EXPECTED_MODEL_PROFILE_DATABASE",
    "PACKAGE_LOAD_MODE",
    "binding_from_database_snapshot",
    "capability_profile_from_database_snapshot",
    "capability_profiles_projection",
    "catalog_capability_profile",
    "catalog_metadata",
    "database_snapshot",
    "default_reference_contract_for_family",
    "get_model_profile_catalog",
    "interface_slug",
    "reference_contract_payload",
    "reference_specs_projection",
    "require_official_reference_binding",
    "resolve_legacy_interface_id",
    "resolve_runtime_parameter_config",
    "resolve_runtime_profile_binding",
    "resolve_runtime_test_policy",
    "resolve_test_binding_id",
    "stable_slug",
]
