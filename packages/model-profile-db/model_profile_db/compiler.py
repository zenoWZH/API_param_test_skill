from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import yaml

from .paths import (
    ARTIFACT_MANIFEST_SCHEMA_PATH,
    COMPILED_CATALOG_PATH,
    COMPILED_CORE_SCHEMA_PATH,
    MANIFEST_PATH,
    SOURCE_CATALOG_SCHEMA_PATH,
    SOURCE_CATALOG_PATH,
    SQLITE_CATALOG_PATH,
    TEST_EXTENSIONS_PATH,
    TEST_EXTENSIONS_SCHEMA_PATH,
)


SUPPORTED_SCHEMA_VERSIONS = {2, 3}
# Physical artifact formats evolve independently of the logical MPDB schema.
# This prevents a layout-breaking JSON/SQLite change from being published
# under an unchanged ``mpdb_schema_version`` without an explicit signal.
SOURCE_CATALOG_FORMAT_VERSION = 1
COMPILED_CORE_FORMAT_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
SQLITE_SCHEMA_VERSION = 3
TEST_EXTENSION_SCHEMA_VERSION = 2
WORKFLOW_SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_REQUIRED_SCHEMA_VERSION = 3
PROVENANCE_REQUIRED_CATALOG_VERSION = (0, 4, 0)
# User-approved on 2026-09-20 for the media-input validator only. These exact
# tuples are packaged with MPDB; compilation never reads a repository-local
# approval file and this permission does not change generic policies.
MEDIA_INPUT_VALIDATION_APPROVAL_ID = "official-media-input-validation-20260920"
MEDIA_INPUT_VALIDATION_TARGET_FIELDS = (
    "source_id", "profile_id", "interface_id", "contract_id", "provider_id",
    "request_model_id", "api_form", "transport_adapter_id",
)
MEDIA_INPUT_VALIDATION_TARGETS = frozenset(
    (
        "google_ai_studio", f"text/google_ai_studio/gemini/{slug}",
        f"text/google_ai_studio/gemini/{slug}#gemini-generate-content-default",
        "gemini_native_generate_content", "gemini", request_model,
        "gemini_generate_content", "gemini_generate_content",
    )
    for request_model, slug in (
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
        ("gemini-2.5-pro", "gemini-2.5-pro"),
        ("gemini-3-flash-preview", "gemini-3-flash"),
        ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite"),
        ("gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),
        ("gemini-3.1-pro-preview-customtools", "gemini-3.1-pro-preview"),
        ("gemini-3.5-flash", "gemini-3.5-flash"),
        ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
        ("gemini-3.6-flash", "gemini-3.6-flash"),
    )
) | frozenset(
    (
        "deepseek", "text/deepseek/deepseek/deepseek-v4.1-flash",
        "text/deepseek/deepseek/deepseek-v4.1-flash#" + interface_slug,
        contract_id, "deepseek_official", "deepseek-flash", api_form, transport,
    )
    for interface_slug, contract_id, api_form, transport in (
        ("openai-chat-default", "deepseek_v4_1_flash_chat", "openai_chat_completions", "chat_completions"),
        ("openai-responses-default", "deepseek_v4_1_flash_responses", "openai_responses", "openai_responses"),
        ("anthropic-messages-default", "deepseek_v4_1_flash_anthropic", "anthropic_messages", "claude_messages"),
    )
) | frozenset({(
    "zhipu", "text/zhipu/glm/glm-5.3-flash",
    "text/zhipu/glm/glm-5.3-flash#zai-coding-chat", "zai_coding_glm_5_3_flash_chat",
    "zhipu_official", "glm-5.3-flash", "openai_chat_completions", "chat_completions",
)})


def is_approved_media_input_workflow(binding: dict[str, Any]) -> bool:
    """Recognize one explicit media approval; no family/provider wildcards."""
    target = binding.get("execution_target") or {}
    workflow = binding.get("workflow") or {}
    identity = tuple(binding.get(key) if key in {"source_id", "profile_id", "interface_id", "contract_id"}
                     else target.get(key) for key in MEDIA_INPUT_VALIDATION_TARGET_FIELDS)
    expected_id = "media-input/{}/{}/{}".format(
        target.get("provider_id", ""), str(target.get("request_model_id", "")).casefold(),
        target.get("api_form", ""))
    return (
        binding.get("execution_permission") == {
            "scope": "exact_workflow", "mode": "scoped_research",
            "approval_id": MEDIA_INPUT_VALIDATION_APPROVAL_ID,
        }
        and workflow.get("factory_id") == "media_input"
        and workflow.get("version") == "1"
        and binding.get("workflow_id") == expected_id
        and not binding.get("gateway_mapping")
        and identity in MEDIA_INPUT_VALIDATION_TARGETS
    )


SOURCE_CATALOG_SCHEMA_ID = (
    "https://yibu.example/schemas/model-profile-db/source-catalog-v3.json"
)
COMPILED_CORE_SCHEMA_ID = (
    "https://yibu.example/schemas/model-profile-db/compiled-core-format-v1.json"
)
TEST_EXTENSIONS_SCHEMA_ID = (
    "https://yibu.example/schemas/model-profile-db/test-extensions-v2.json"
)
ARTIFACT_MANIFEST_SCHEMA_ID = (
    "https://yibu.example/schemas/model-profile-db/artifact-manifest-v1.json"
)
SQLITE_SCHEMA_ID = (
    f"urn:yibu:model-profile-db:sqlite-core-v{SQLITE_SCHEMA_VERSION}"
)
EXPECTED_ARTIFACT_FORMAT_VERSIONS = {
    "source_catalog": SOURCE_CATALOG_FORMAT_VERSION,
    "compiled_core": COMPILED_CORE_FORMAT_VERSION,
    "test_extensions": TEST_EXTENSION_SCHEMA_VERSION,
    "sqlite": SQLITE_SCHEMA_VERSION,
    "manifest": MANIFEST_SCHEMA_VERSION,
}
EXPECTED_SCHEMA_IDENTITIES = {
    "source_catalog": SOURCE_CATALOG_SCHEMA_ID,
    "compiled_core": COMPILED_CORE_SCHEMA_ID,
    "test_extensions": TEST_EXTENSIONS_SCHEMA_ID,
    "artifact_manifest": ARTIFACT_MANIFEST_SCHEMA_ID,
    "sqlite": SQLITE_SCHEMA_ID,
}
REQUIRED_MANIFEST_ARTIFACTS = {
    "source_yaml",
    "json",
    "sqlite",
    "source_catalog_schema",
    "compiled_core_schema",
    "test_extensions_schema",
    "artifact_manifest_schema",
}
OPTIONAL_MANIFEST_ARTIFACTS = {"test_extensions_yaml"}
ARTIFACT_SCHEMA_KEYS = {
    "source_yaml": "source_catalog",
    "json": "compiled_core",
    "sqlite": "sqlite",
    "source_catalog_schema": "source_catalog",
    "compiled_core_schema": "compiled_core",
    "test_extensions_schema": "test_extensions",
    "artifact_manifest_schema": "artifact_manifest",
    "test_extensions_yaml": "test_extensions",
}
SUPPORTED_MODALITIES = {"text", "image", "video"}
NON_PRESSURE_MODALITIES = {"image", "video"}
PRESSURE_POLICY_FIELDS = (
    "pressure_profiles",
    "pressure_omit_params",
    "pressure_parameter_aliases",
    "pressure_overrides",
    "pressure_transport_overrides",
)
SUPPORTED_SOURCE_TYPES = {
    "official_direct",
    "cloud_managed",
    "platform_marketplace",
}
SOURCE_TYPE_AUTHORITIES = {
    "official_direct": "origin_vendor",
    "cloud_managed": "managed_cloud",
    "platform_marketplace": "managed_platform_marketplace",
}
SUPPORTED_CAPABILITY_STATES = {
    "supported",
    "unsupported",
    "conditional",
    "unknown",
}
CANONICAL_MODEL_VERIFICATION_STATUS = "exact_official_model_id"
PROFILE_VERIFICATION_STATUS = "exact_official_source_model_id"
CONTRACT_VERIFICATION_STATUS = "exact_official_api_contract"
SUPPORTED_REQUEST_MODEL_ID_SEMANTICS = {
    "exact_source_model_id",
    "azure_deployment_model_identity",
}
SUPPORTED_PROFILE_STATES = {"executable", "identity_only"}
SUPPORTED_TEST_BINDING_STATUSES = {
    "required",
    "access_restricted",
    "retired",
    "not_certified",
}
NON_CERTIFIED_PARAMETER_EXECUTION_STATE = (
    "metadata_only_non_certified_parameter_cases"
)
SUPPORTED_API_VERSION_STABILITIES = {"stable", "beta"}
EXPECTED_API_VERSION_STABILITIES = {
    "v1": "stable",
    "v1beta": "beta",
}
PROFILE_HIERARCHY = ["modality", "source_id", "family_id", "model_slug"]
IDENTITY_CONTRACT = {
    "hierarchy": PROFILE_HIERARCHY,
    "profile_id_template": "{modality}/{source_id}/{family_id}/{model_slug}",
    "interface_id_template": "{profile_id}#{interface_slug}",
    "api_form_scope": "interface",
    "source_semantics": "official_reference_origin",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
INTERFACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*#[a-z0-9][a-z0-9._-]*$")
OFFICIAL_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
SECRET_KEYS = {
    "api_key",
    "api-key",
    "apikey",
    "api_key_env",
    "authorization",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "private_endpoint",
    "base_url",
    "endpoint",
    "api_endpoint",
    "credentials",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
)
REMOVE_SENTINEL = {"$remove": True}
PROVENANCE_RETRIEVAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROVENANCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_TARGET_TYPES = {
    "source",
    "canonical_model",
    "profile",
    "interface",
    "contract",
    "test_binding",
}
SEMANTIC_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class CatalogValidationError(ValueError):
    """Raised when source or compiled catalog data violates the public contract."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        # Both loaders use SafeConstructor; use libyaml when installed so the
        # multi-megabyte reference matrix does not dominate every CLI startup.
        payload = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
        ) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise CatalogValidationError(
            f"Cannot read YAML artifact {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CatalogValidationError(f"YAML artifact must be an object: {path}")
    return payload


def load_source_catalog(
    path: str | Path | None = None,
    *,
    test_extensions_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the author catalog.

    The default is deliberately core-only.  ``test_extensions_path`` remains
    an explicit compatibility hook for callers that need the old combined
    source shape; new code should use :func:`load_test_extensions` and
    :func:`compose_catalog`.
    """
    source_path = Path(path) if path else SOURCE_CATALOG_PATH
    payload = _read_yaml(source_path)
    if test_extensions_path is not None:
        extensions = load_test_extensions(test_extensions_path)
        payload["test_bindings"] = copy.deepcopy(
            extensions.get("test_bindings") or {}
        )
        payload["test_extension_schema_version"] = int(
            extensions.get("test_extension_schema_version") or 1
        )
        if "provenance_records" in extensions:
            payload["test_binding_provenance_records"] = copy.deepcopy(
                extensions.get("provenance_records") or {}
            )
    return payload


def load_test_extensions(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the optional project test-extension artifact without core data."""

    extension_path = Path(path) if path else TEST_EXTENSIONS_PATH
    payload = _read_yaml(extension_path)
    version = int(payload.get("test_extension_schema_version") or 0)
    expected_fields = {"test_extension_schema_version", "test_bindings"}
    if version >= TEST_EXTENSION_SCHEMA_VERSION:
        expected_fields.update({"provenance_schema_version", "provenance_records"})
    if set(payload) != expected_fields:
        raise CatalogValidationError(
            "Test extensions fields do not match their schema version; "
            f"expected={sorted(expected_fields)}, actual={sorted(payload)}"
        )
    if version not in {1, TEST_EXTENSION_SCHEMA_VERSION}:
        raise CatalogValidationError(
            "test_extension_schema_version must be 1 or "
            f"{TEST_EXTENSION_SCHEMA_VERSION}"
        )
    if version >= TEST_EXTENSION_SCHEMA_VERSION and int(
        payload.get("provenance_schema_version") or 0
    ) != PROVENANCE_SCHEMA_VERSION:
        raise CatalogValidationError(
            "provenance_schema_version must be "
            f"{PROVENANCE_SCHEMA_VERSION} for test extensions v{version}"
        )
    if not isinstance(payload.get("test_bindings"), dict):
        raise CatalogValidationError("test_bindings must be an object")
    if version >= TEST_EXTENSION_SCHEMA_VERSION and not isinstance(
        payload.get("provenance_records"), dict
    ):
        raise CatalogValidationError("provenance_records must be an object")
    return payload


def _deep_merge(base: Any, override: Any) -> Any:
    if override == REMOVE_SENTINEL:
        return REMOVE_SENTINEL
    if isinstance(base, dict) and isinstance(override, dict):
        result = copy.deepcopy(base)
        for key, value in override.items():
            if value == REMOVE_SENTINEL:
                result.pop(key, None)
                continue
            result[key] = _deep_merge(result.get(key), value)
        return result
    return copy.deepcopy(override)


def _expand_contracts(raw_contracts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expanded: dict[str, dict[str, Any]] = {}
    visiting: list[str] = []

    def resolve(contract_id: str) -> dict[str, Any]:
        if contract_id in expanded:
            return expanded[contract_id]
        if contract_id in visiting:
            chain = " -> ".join([*visiting, contract_id])
            raise CatalogValidationError(f"Contract inheritance cycle: {chain}")
        raw = raw_contracts.get(contract_id)
        if not isinstance(raw, dict):
            raise CatalogValidationError(
                f"Contract {contract_id!r} must be an object"
            )
        visiting.append(contract_id)
        current = copy.deepcopy(raw)
        parent_id = str(current.pop("extends", "") or "").strip()
        if parent_id:
            if parent_id not in raw_contracts:
                raise CatalogValidationError(
                    f"Contract {contract_id!r} extends unknown {parent_id!r}"
                )
            current = _deep_merge(resolve(parent_id), current)
            current["parent_contract_id"] = parent_id
        visiting.pop()
        current["contract_id"] = contract_id
        expanded[contract_id] = current
        return current

    for contract_id in sorted(raw_contracts):
        resolve(str(contract_id))
    return expanded


def _contract_source_id(contract: dict[str, Any]) -> str:
    """Return the scalar source owner, including legacy schema-v2/v3 rows."""
    scalar_source_id = str(contract.get("source_id") or "")
    if scalar_source_id:
        return scalar_source_id
    source_ids = contract.get("source_ids")
    if isinstance(source_ids, list) and len(source_ids) == 1:
        return str(source_ids[0] or "")
    return ""


def _requires_provenance(payload: dict[str, Any]) -> bool:
    """Gate the 0.4.0 provenance contract without renumbering logical schema 3.

    Schema-v3 snapshots published before catalog 0.4.0 remain readable.  Once
    provenance is declared, it cannot be stripped merely by using a synthetic
    or non-semantic catalog version.
    """
    if int(payload.get("mpdb_schema_version") or 0) < (
        PROVENANCE_REQUIRED_SCHEMA_VERSION
    ):
        return False
    match = SEMANTIC_VERSION_RE.fullmatch(
        str(payload.get("catalog_version") or "")
    )
    if match and tuple(int(part) for part in match.groups()) >= (
        PROVENANCE_REQUIRED_CATALOG_VERSION
    ):
        return True
    return any(
        field in payload
        for field in (
            "provenance_schema_version",
            "provenance_records",
            "test_binding_provenance_records",
        )
    )


def _validate_api_form_identity_boundary(payload: dict[str, Any]) -> None:
    """Keep API Form metadata out of model and Profile identity rows."""

    registries = (
        ("Canonical Model", payload.get("canonical_models") or {}),
        ("Profile", payload.get("profiles") or {}),
    )
    for label, registry in registries:
        if not isinstance(registry, dict):
            continue
        for row_id, row in sorted(registry.items()):
            if isinstance(row, dict) and "api_form" in row:
                raise CatalogValidationError(
                    f"{label} {row_id!r} must not declare api_form; "
                    "API Form belongs to Interface"
                )


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def catalog_digest(payload: dict[str, Any]) -> str:
    digest_payload = copy.deepcopy(payload)
    digest_payload.pop("catalog_digest", None)
    digest_payload.pop("manifest", None)
    # The catalog digest identifies the reusable core only.  Optional project
    # test extensions have their own digest and can be composed without
    # changing model/profile identity.
    digest_payload.pop("test_extension_schema_version", None)
    digest_payload.pop("test_extension_digest", None)
    digest_payload.pop("test_bindings", None)
    digest_payload.pop("test_binding_provenance_records", None)
    return hashlib.sha256(_canonical_json_bytes(digest_payload)).hexdigest()


def test_extension_digest(payload: dict[str, Any]) -> str:
    digest_payload = copy.deepcopy(payload)
    digest_payload.pop("test_extension_digest", None)
    return hashlib.sha256(_canonical_json_bytes(digest_payload)).hexdigest()


def compose_catalog(
    compiled_core: dict[str, Any],
    test_extensions: dict[str, Any],
) -> dict[str, Any]:
    """Validate and compose an optional extension onto an immutable core."""

    core = copy.deepcopy(compiled_core)
    if "test_bindings" in core or "test_extension_schema_version" in core:
        raise CatalogValidationError(
            "Compiled core must not physically contain test extensions"
        )
    validate_catalog(core, compiled=True)
    extensions = copy.deepcopy(test_extensions)
    extension_version = int(
        extensions.get("test_extension_schema_version") or 0
    )
    if extension_version not in {1, TEST_EXTENSION_SCHEMA_VERSION}:
        raise CatalogValidationError(
            "test_extension_schema_version must be 1 or "
            f"{TEST_EXTENSION_SCHEMA_VERSION}"
        )
    core_schema_version = int(core.get("mpdb_schema_version") or 0)
    if _requires_provenance(core) and extension_version != (
        TEST_EXTENSION_SCHEMA_VERSION
    ):
        raise CatalogValidationError(
            f"MPDB schema v{core_schema_version} requires test extensions v"
            f"{TEST_EXTENSION_SCHEMA_VERSION} with field provenance"
        )
    bindings = extensions.get("test_bindings")
    if not isinstance(bindings, dict):
        raise CatalogValidationError("test_bindings must be an object")
    composed = core
    composed["test_extension_schema_version"] = extension_version
    composed["test_extension_digest"] = test_extension_digest(extensions)
    composed["test_bindings"] = bindings
    if extension_version >= TEST_EXTENSION_SCHEMA_VERSION:
        if int(extensions.get("provenance_schema_version") or 0) != (
            PROVENANCE_SCHEMA_VERSION
        ):
            raise CatalogValidationError(
                "Test extensions require provenance_schema_version="
                f"{PROVENANCE_SCHEMA_VERSION}"
            )
        provenance_records = extensions.get("provenance_records")
        if not isinstance(provenance_records, dict):
            raise CatalogValidationError(
                "Test extensions provenance_records must be an object"
            )
        composed["test_binding_provenance_records"] = copy.deepcopy(
            provenance_records
        )
    validate_catalog(composed, compiled=True)
    return composed


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._/-]+", "-", str(value).casefold()).strip("-")
    return normalized or "unknown"


def _profile_id(modality: str, source: str, family: str, model_slug: str) -> str:
    return f"{modality}/{source}/{family}/{model_slug}"


def _interface_id(profile_id: str, interface_slug: str) -> str:
    return f"{profile_id}#{interface_slug}"


def _source_first_view(profiles: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {modality: {} for modality in sorted(SUPPORTED_MODALITIES)}
    for profile_id, profile in sorted(profiles.items()):
        modality = str(profile["modality"])
        source = str(profile["source_id"])
        family = str(profile["family_id"])
        model = str(profile["model_slug"])
        view.setdefault(modality, {}).setdefault(source, {}).setdefault(
            family, {}
        )[model] = profile_id
    return view


def _with_media_safety_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply fail-closed defaults without changing the v2/v3 schema number.

    Older schema-v2/v3 catalogs predate the explicit media pressure flag. A
    missing value is therefore interpreted as ``false``; an explicit ``true``
    is still rejected by validation. Newly compiled artifacts always serialize
    the false value so cross-language consumers do not need to infer it.
    """
    normalized = copy.deepcopy(payload)
    modalities = normalized.get("modalities") or {}
    if isinstance(modalities, dict):
        for modality in NON_PRESSURE_MODALITIES:
            row = modalities.get(modality)
            if isinstance(row, dict):
                row.setdefault("pressure_test_enabled", False)

    interfaces = normalized.get("interfaces") or {}
    bindings = normalized.get("test_bindings") or {}
    if isinstance(interfaces, dict) and isinstance(bindings, dict):
        for binding in bindings.values():
            if not isinstance(binding, dict):
                continue
            if binding.get("extension_type") != "model_test_policy":
                continue
            interface = interfaces.get(str(binding.get("interface_id") or "")) or {}
            if str(interface.get("modality") or "") in NON_PRESSURE_MODALITIES:
                binding.setdefault("pressure_test_enabled", False)
    return normalized


def compile_catalog(source: dict[str, Any]) -> dict[str, Any]:
    required = (
        "mpdb_schema_version",
        "catalog_version",
        "identity_contract",
        "modalities",
        "sources",
        "families",
        "canonical_models",
        "contracts",
        "profiles",
    )
    missing = [field for field in required if field not in source]
    schema_version = int(source.get("mpdb_schema_version") or 0)
    if _requires_provenance(source):
        missing.extend(
            field
            for field in ("provenance_schema_version", "provenance_records")
            if field not in source
        )
    if missing:
        raise CatalogValidationError(
            f"Catalog source is missing required fields: {', '.join(missing)}"
        )
    payload = copy.deepcopy(source)
    embedded_extensions: dict[str, Any] | None = None
    if "test_bindings" in payload or "test_extension_schema_version" in payload:
        embedded_extensions = {
            "test_extension_schema_version": int(
                payload.pop("test_extension_schema_version", 1) or 1
            ),
            "test_bindings": copy.deepcopy(payload.pop("test_bindings", {})),
        }
        if "test_binding_provenance_records" in payload:
            embedded_extensions["provenance_schema_version"] = (
                PROVENANCE_SCHEMA_VERSION
            )
            embedded_extensions["provenance_records"] = copy.deepcopy(
                payload.pop("test_binding_provenance_records", {})
            )
    for registry in (
        "modalities",
        "sources",
        "families",
        "canonical_models",
        "contracts",
        "profiles",
    ):
        if not isinstance(payload.get(registry), dict):
            raise CatalogValidationError(
                f"Catalog source field {registry!r} must be an object"
            )
    payload.setdefault("route_templates", {})
    payload.setdefault("legacy_aliases", {})
    for registry in ("route_templates", "legacy_aliases"):
        if not isinstance(payload.get(registry), dict):
            raise CatalogValidationError(
                f"Catalog source field {registry!r} must be an object"
            )
    # Reject project Test Binding metadata before provenance field-ledger
    # comparison.  Otherwise an embedded safety row only surfaces as a generic
    # provenance mismatch and obscures the stricter core/extension boundary.
    for profile_id, profile in sorted(payload["profiles"].items()):
        for interface_slug, interface in sorted(
            ((profile or {}).get("interfaces") or {}).items()
        ):
            if isinstance(interface, dict) and "safety_binding" in interface:
                raise CatalogValidationError(
                    f"Interface {profile_id}#{interface_slug} must not embed "
                    "project Test Binding metadata"
                )
    _validate_core_provenance(payload, compiled=False)
    _validate_api_form_identity_boundary(payload)
    payload["contracts"] = _expand_contracts(payload["contracts"])
    schema_version = int(payload.get("mpdb_schema_version") or 0)
    if schema_version >= 3:
        for contract in payload["contracts"].values():
            contract.setdefault("test_binding_status", "required")
    if _requires_provenance(payload):
        records = payload["provenance_records"]
        for contract_id, contract in payload["contracts"].items():
            record_id = str(contract.get("provenance_record_id") or "")
            record = records.get(record_id) or {}
            target = record.get("target") or {}
            target["fields"] = _provenance_field_names(
                contract,
                excluded={"contract_id", "extends", "parent_contract_id"},
            )
            record["target"] = target
            record["parameters"] = _provenance_parameters(contract)
            records[record_id] = record

    compiled_profiles: dict[str, dict[str, Any]] = {}
    compiled_interfaces: dict[str, dict[str, Any]] = {}
    for declared_profile_id, raw_profile in sorted(payload["profiles"].items()):
        if not isinstance(raw_profile, dict):
            raise CatalogValidationError(
                f"Profile {declared_profile_id!r} must be an object"
            )
        profile = copy.deepcopy(raw_profile)
        modality = str(profile.get("modality") or "")
        source = str(profile.get("source_id") or "")
        family = str(profile.get("family_id") or "")
        model_slug = str(profile.get("model_slug") or "")
        profile_state = str(profile.get("profile_state") or "executable")
        if profile_state not in SUPPORTED_PROFILE_STATES:
            raise CatalogValidationError(
                f"Profile {declared_profile_id!r} has invalid profile_state={profile_state!r}"
            )
        expected_profile_id = _profile_id(modality, source, family, model_slug)
        if declared_profile_id != expected_profile_id:
            raise CatalogValidationError(
                f"Profile key {declared_profile_id!r} must equal {expected_profile_id!r}"
            )
        if schema_version >= 3 or "profile_state" in profile:
            profile["profile_state"] = profile_state
        interfaces = profile.pop("interfaces", {}) or {}
        if not isinstance(interfaces, dict):
            raise CatalogValidationError(
                f"Profile {declared_profile_id!r}.interfaces must be an object"
            )
        interface_ids: list[str] = []
        for interface_slug, raw_interface in sorted(interfaces.items()):
            if not isinstance(raw_interface, dict):
                raise CatalogValidationError(
                    f"Interface {declared_profile_id}#{interface_slug} must be an object"
                )
            interface_id = _interface_id(declared_profile_id, str(interface_slug))
            interface = copy.deepcopy(raw_interface)
            if schema_version >= 3:
                interface.setdefault("test_binding_status", "required")
            interface.update(
                {
                    "interface_id": interface_id,
                    "interface_slug": str(interface_slug),
                    "profile_id": declared_profile_id,
                    "modality": modality,
                    "source_id": source,
                    "family_id": family,
                    "model_slug": model_slug,
                }
            )
            compiled_interfaces[interface_id] = interface
            interface_ids.append(interface_id)
        profile["profile_id"] = declared_profile_id
        profile["interface_ids"] = interface_ids
        source_row = payload["sources"].get(source) or {}
        source_authority = str(source_row.get("authority") or "")
        declared_source_authority = str(
            profile.get("source_authority") or ""
        )
        if (
            declared_source_authority
            and declared_source_authority != source_authority
        ):
            raise CatalogValidationError(
                f"Profile {declared_profile_id!r} source_authority does not "
                f"match Source {source!r}"
            )
        profile["source_authority"] = source_authority
        compiled_profiles[declared_profile_id] = profile

    compiled = {
        "compiled_core_format_version": COMPILED_CORE_FORMAT_VERSION,
        "mpdb_schema_version": int(payload.get("mpdb_schema_version") or 0),
        "catalog_version": str(payload.get("catalog_version") or ""),
        "released_at": str(payload.get("released_at") or ""),
        "identity_contract": payload["identity_contract"],
        **(
            {
                "provenance_schema_version": int(
                    payload.get("provenance_schema_version") or 0
                ),
                "provenance_records": payload["provenance_records"],
            }
            if _requires_provenance(payload)
            else {}
        ),
        "modalities": payload["modalities"],
        "sources": payload["sources"],
        "families": payload["families"],
        "canonical_models": payload["canonical_models"],
        "contracts": payload["contracts"],
        "profiles": compiled_profiles,
        "interfaces": compiled_interfaces,
        "route_templates": payload["route_templates"],
        "legacy_aliases": payload["legacy_aliases"],
        "views": {
            "modality_source_family_model": _source_first_view(compiled_profiles)
        },
    }
    compiled = _with_media_safety_defaults(compiled)
    compiled["catalog_digest"] = catalog_digest(compiled)
    validate_catalog(compiled, compiled=True)
    if embedded_extensions is not None:
        compose_catalog(compiled, embedded_extensions)
    return compiled


def _check_identifier(identifier: str, *, label: str, interface: bool = False) -> None:
    matcher = INTERFACE_ID_RE if interface else ID_RE
    if not matcher.fullmatch(identifier):
        raise CatalogValidationError(f"Invalid {label} {identifier!r}")


def _walk_for_secrets(value: Any, path: str = "catalog") -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in SECRET_KEYS:
                yield f"{path}.{key_text}: forbidden secret/runtime key"
            yield from _walk_for_secrets(child, f"{path}.{key_text}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_for_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                yield f"{path}: value resembles a credential"


def _official_url_matches_source(
    url: str,
    source: dict[str, Any],
) -> bool:
    parsed = urlparse(str(url))
    if not _is_safe_https_url(parsed):
        return False
    hostname = str(parsed.hostname or "").casefold()
    domains = {
        str(domain).strip().casefold()
        for domain in source.get("official_domains") or []
        if str(domain).strip()
    }
    if bool(hostname) and any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in domains
    ):
        return True
    url_segments = _safe_url_path_segments(parsed.path)
    if url_segments is None:
        return False
    for prefix in source.get("official_url_prefixes") or []:
        parsed_prefix = urlparse(str(prefix))
        if not _is_valid_official_url_prefix(parsed_prefix):
            continue
        if str(parsed_prefix.hostname or "").casefold() != hostname:
            continue
        prefix_segments = _safe_url_path_segments(parsed_prefix.path)
        if prefix_segments is not None and url_segments[: len(prefix_segments)] == (
            prefix_segments
        ):
            return True
    return False


def _is_safe_https_url(parsed: Any) -> bool:
    return (
        parsed.scheme.casefold() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _safe_url_path_segments(path: str) -> tuple[str, ...] | None:
    decoded_path = unquote(str(path))
    if "\\" in decoded_path:
        return None
    segments = tuple(segment for segment in decoded_path.split("/") if segment)
    if any(segment in {".", ".."} for segment in segments):
        return None
    return segments


def _is_valid_official_url_prefix(parsed: Any) -> bool:
    if not _is_safe_https_url(parsed):
        return False
    if parsed.query or parsed.fragment or parsed.params:
        return False
    segments = _safe_url_path_segments(parsed.path)
    return bool(segments) and str(parsed.path).endswith("/")


def _provenance_field_names(
    row: dict[str, Any],
    *,
    excluded: Iterable[str] = (),
) -> list[str]:
    """Return the deterministic logical field list covered by one record."""

    ignored = {"provenance_record_id", *excluded}
    return sorted(str(field) for field in row if str(field) not in ignored)


def _provenance_parameters(*rows: dict[str, Any]) -> list[str]:
    parameters: set[str] = set()
    parameter_fields = (
        "parameter_capabilities",
        "parameter_constraints",
        "parameter_coverage",
        "parameter_expectations",
        "model_parameter_expectations",
        "default_parameter_expectations",
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in parameter_fields:
            value = row.get(field) or {}
            if isinstance(value, dict):
                parameters.update(str(name) for name in value if str(name))
    return sorted(parameters)


def _validate_provenance_record(
    *,
    record_id: str,
    record: Any,
    sources: dict[str, Any],
    expected_source_id: str,
    expected_target_type: str,
    expected_target_id: str,
    expected_fields: list[str],
    expected_official_urls: list[str],
    expected_api_form: str | None,
    expected_parameters: list[str],
    expected_identity: dict[str, Any],
) -> None:
    _check_identifier(record_id, label="provenance_record_id")
    if not isinstance(record, dict):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} must be an object"
        )
    if expected_target_type not in PROVENANCE_TARGET_TYPES:
        raise CatalogValidationError(
            f"Unsupported provenance target type {expected_target_type!r}"
        )
    source = sources.get(expected_source_id)
    if not isinstance(source, dict):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} references unknown Source "
            f"{expected_source_id!r}"
        )
    if str(record.get("source_id") or "") != expected_source_id:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} belongs to the wrong Source; "
            f"expected={expected_source_id!r}"
        )
    expected_authority = str(source.get("authority") or "")
    if str(record.get("authority") or "") != expected_authority:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} authority must match Source "
            f"{expected_source_id!r}"
        )
    authority_domains = record.get("authority_domains")
    expected_domains = sorted(
        str(domain).strip().casefold()
        for domain in source.get("official_domains") or []
    )
    if authority_domains != expected_domains:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} authority_domains must exactly "
            f"match Source {expected_source_id!r}"
        )
    official_urls = record.get("official_urls")
    expected_urls = sorted(set(str(url) for url in expected_official_urls))
    if not expected_urls or official_urls != expected_urls:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} official_urls must exactly match "
            "the target's approved official_sources"
        )
    unmatched_urls = [
        str(url)
        for url in official_urls
        if not _official_url_matches_source(str(url), source)
    ]
    if unmatched_urls:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} has URLs outside Source "
            f"{expected_source_id!r}: {unmatched_urls}"
        )
    retrieved_at = str(record.get("retrieved_at") or "")
    if not PROVENANCE_RETRIEVAL_DATE_RE.fullmatch(retrieved_at):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} retrieved_at must be YYYY-MM-DD"
        )
    target = record.get("target")
    if not isinstance(target, dict):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} target must be an object"
        )
    if (
        str(target.get("type") or "") != expected_target_type
        or str(target.get("id") or "") != expected_target_id
        or target.get("fields") != expected_fields
    ):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} target or field ledger does not "
            f"match {expected_target_type} {expected_target_id!r}"
        )
    identity = record.get("exact_identity")
    if not isinstance(identity, dict) or not identity:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} exact_identity must be an object"
        )
    for field, expected in expected_identity.items():
        if identity.get(field) != expected:
            raise CatalogValidationError(
                f"Provenance record {record_id!r} exact_identity.{field} "
                f"must equal {expected!r}"
            )
    unexpected_identity_fields = sorted(set(identity) - set(expected_identity))
    if unexpected_identity_fields:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} exact_identity has fields not "
            f"declared by its target: {unexpected_identity_fields}"
        )
    if identity.get("source_id") != expected_source_id:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} exact identity crosses Source"
        )
    api_form = record.get("api_form")
    if api_form != expected_api_form:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} api_form must equal "
            f"{expected_api_form!r}"
        )
    if record.get("parameters") != expected_parameters:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} parameter ledger is stale"
        )
    if not str(record.get("section_summary") or "").strip():
        raise CatalogValidationError(
            f"Provenance record {record_id!r} requires section_summary"
        )
    content_version = record.get("content_version")
    if not isinstance(content_version, dict):
        raise CatalogValidationError(
            f"Provenance record {record_id!r} content_version must be an object"
        )
    version_kind = str(content_version.get("kind") or "")
    if version_kind == "official_version":
        if set(content_version) != {"kind", "value"} or not str(
            content_version.get("value") or ""
        ).strip():
            raise CatalogValidationError(
                f"Provenance record {record_id!r} has invalid official version"
            )
    elif version_kind == "content_sha256":
        if set(content_version) != {"kind", "sha256"} or not (
            PROVENANCE_HASH_RE.fullmatch(
                str(content_version.get("sha256") or "")
            )
        ):
            raise CatalogValidationError(
                f"Provenance record {record_id!r} has invalid content SHA256"
            )
    elif version_kind == "unversioned_retrieval_snapshot":
        if set(content_version) != {
            "kind",
            "snapshot_date",
            "page_content_sha256_available",
        } or content_version.get("page_content_sha256_available") is not False:
            raise CatalogValidationError(
                f"Provenance record {record_id!r} must transparently mark an "
                "unversioned retrieval snapshot without inventing a page hash"
            )
        if str(content_version.get("snapshot_date") or "") != retrieved_at:
            raise CatalogValidationError(
                f"Provenance record {record_id!r} snapshot_date must match "
                "retrieved_at"
            )
    else:
        raise CatalogValidationError(
            f"Provenance record {record_id!r} requires an official version, "
            "content SHA256, or explicit unversioned retrieval snapshot"
        )


def _validate_core_provenance(
    payload: dict[str, Any],
    *,
    compiled: bool | None,
) -> None:
    version = int(payload.get("mpdb_schema_version") or 0)
    if not _requires_provenance(payload):
        return
    if int(payload.get("provenance_schema_version") or 0) != (
        PROVENANCE_SCHEMA_VERSION
    ):
        raise CatalogValidationError(
            f"MPDB schema v{version} requires provenance_schema_version="
            f"{PROVENANCE_SCHEMA_VERSION}"
        )
    records = payload.get("provenance_records")
    if not isinstance(records, dict) or not records:
        raise CatalogValidationError(
            f"MPDB schema v{version} requires provenance_records"
        )
    sources = payload.get("sources") or {}
    models = payload.get("canonical_models") or {}
    profiles = payload.get("profiles") or {}
    contracts = payload.get("contracts") or {}
    compiled_shape = isinstance(payload.get("interfaces"), dict)
    logical_contracts = contracts if compiled_shape else _expand_contracts(contracts)
    used: set[str] = set()

    def check(
        row: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
        source_id: str,
        excluded_fields: Iterable[str] = (),
        official_urls: list[str],
        api_form: str | None,
        parameters: list[str],
        identity: dict[str, Any],
        logical_row: dict[str, Any] | None = None,
    ) -> None:
        record_id = str(row.get("provenance_record_id") or "")
        if not record_id:
            raise CatalogValidationError(
                f"{target_type} {target_id!r} is missing provenance_record_id"
            )
        if record_id in used:
            raise CatalogValidationError(
                f"Provenance record {record_id!r} is reused across targets"
            )
        used.add(record_id)
        target_row = logical_row if logical_row is not None else row
        _validate_provenance_record(
            record_id=record_id,
            record=records.get(record_id),
            sources=sources,
            expected_source_id=source_id,
            expected_target_type=target_type,
            expected_target_id=target_id,
            expected_fields=_provenance_field_names(
                target_row, excluded=excluded_fields
            ),
            expected_official_urls=official_urls,
            expected_api_form=api_form,
            expected_parameters=parameters,
            expected_identity=identity,
        )

    for source_id, source in sorted(sources.items()):
        check(
            source,
            target_type="source",
            target_id=str(source_id),
            source_id=str(source_id),
            official_urls=list(source.get("official_sources") or []),
            api_form=None,
            parameters=[],
            identity={"source_id": str(source_id)},
        )
    for model_id, model in sorted(models.items()):
        owner_source_id = str(model.get("owner_source_id") or "")
        check(
            model,
            target_type="canonical_model",
            target_id=str(model_id),
            source_id=owner_source_id,
            official_urls=list(model.get("official_sources") or []),
            api_form=None,
            parameters=[],
            identity={
                "source_id": owner_source_id,
                "canonical_model_id": str(model_id),
                "model_slug": str(model.get("model_slug") or ""),
                **(
                    {"model_version": str(model["model_version"])}
                    if model.get("model_version")
                    else {}
                ),
                **(
                    {
                        "source_request_model_id": str(
                            model["source_request_model_id"]
                        )
                    }
                    if model.get("source_request_model_id")
                    else {}
                ),
            },
        )
    for contract_id, contract in sorted(contracts.items()):
        logical_contract = logical_contracts.get(contract_id) or contract
        source_id = str(logical_contract.get("source_id") or "")
        check(
            contract,
            target_type="contract",
            target_id=str(contract_id),
            source_id=source_id,
            excluded_fields={"contract_id", "extends", "parent_contract_id"},
            official_urls=list(logical_contract.get("official_sources") or []),
            api_form=str(logical_contract.get("api_form") or ""),
            parameters=_provenance_parameters(logical_contract),
            identity={
                "source_id": source_id,
                "contract_id": str(contract_id),
            },
            logical_row=logical_contract,
        )

    if compiled_shape:
        interfaces = payload.get("interfaces") or {}
        interface_rows = sorted(interfaces.items())
    else:
        interface_rows = []
        for profile_id, profile in sorted(profiles.items()):
            for interface_slug, interface in sorted(
                (profile.get("interfaces") or {}).items()
            ):
                interface_rows.append(
                    (f"{profile_id}#{interface_slug}", interface)
                )
    for profile_id, profile in sorted(profiles.items()):
        source_id = str(profile.get("source_id") or "")
        check(
            profile,
            target_type="profile",
            target_id=str(profile_id),
            source_id=source_id,
            excluded_fields={"interfaces", "interface_ids", "profile_id"},
            official_urls=list(profile.get("official_sources") or []),
            api_form=None,
            parameters=[],
            identity={
                "source_id": source_id,
                "profile_id": str(profile_id),
                "canonical_model_id": str(
                    profile.get("canonical_model_id") or ""
                ),
                "request_model_ids": list(
                    profile.get("request_model_ids") or []
                ),
            },
        )
    for interface_id, interface in interface_rows:
        profile_id = str(interface_id).split("#", 1)[0]
        profile = profiles.get(profile_id) or {}
        source_id = str(profile.get("source_id") or interface.get("source_id") or "")
        contract_ids = list(interface.get("contract_ids") or [])
        interface_urls = list(profile.get("official_sources") or [])
        contract_rows: list[dict[str, Any]] = []
        for contract_id in contract_ids:
            contract = logical_contracts.get(str(contract_id)) or {}
            contract_rows.append(contract)
            interface_urls.extend(contract.get("official_sources") or [])
        request_model_ids = list(
            interface.get("request_model_ids")
            or profile.get("request_model_ids")
            or []
        )
        check(
            interface,
            target_type="interface",
            target_id=str(interface_id),
            source_id=source_id,
            excluded_fields={
                "interface_id",
                "interface_slug",
                "profile_id",
                "modality",
                "source_id",
                "family_id",
                "model_slug",
            },
            official_urls=interface_urls,
            api_form=str(interface.get("api_form") or ""),
            parameters=_provenance_parameters(interface, *contract_rows),
            identity={
                "source_id": source_id,
                "profile_id": profile_id,
                "interface_id": str(interface_id),
                "request_model_ids": request_model_ids,
            },
        )
    orphan_records = sorted(set(records) - used)
    if orphan_records:
        raise CatalogValidationError(
            f"Core provenance ledger contains orphan records: {orphan_records}"
        )


def _validate_test_binding_provenance(payload: dict[str, Any]) -> None:
    version = int(payload.get("mpdb_schema_version") or 0)
    if not _requires_provenance(payload) or "test_bindings" not in payload:
        return
    if int(payload.get("test_extension_schema_version") or 0) != (
        TEST_EXTENSION_SCHEMA_VERSION
    ):
        raise CatalogValidationError(
            f"MPDB schema v{version} requires test_extension_schema_version="
            f"{TEST_EXTENSION_SCHEMA_VERSION}"
        )
    records = payload.get("test_binding_provenance_records")
    if not isinstance(records, dict):
        raise CatalogValidationError(
            "Composed test extensions require test_binding_provenance_records"
        )
    sources = payload.get("sources") or {}
    contracts = payload.get("contracts") or {}
    interfaces = payload.get("interfaces") or {}
    profiles = payload.get("profiles") or {}
    used: set[str] = set()
    for binding_id, binding in sorted((payload.get("test_bindings") or {}).items()):
        record_id = str(binding.get("provenance_record_id") or "")
        if not record_id:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} is missing provenance_record_id"
            )
        if record_id in used:
            raise CatalogValidationError(
                f"Test Binding provenance record {record_id!r} is reused"
            )
        used.add(record_id)
        interface_id = str(binding.get("interface_id") or "")
        contract_ids = [
            str(value)
            for value in [
                binding.get("contract_id"),
                *(binding.get("reference_contract_ids") or []),
            ]
            if str(value or "")
        ]
        contract_ids = list(dict.fromkeys(contract_ids))
        reference_sources = {
            str((contracts.get(contract_id) or {}).get("source_id") or "")
            for contract_id in contract_ids
        }
        interface = interfaces.get(interface_id) or {}
        if interface_id:
            reference_sources.add(str(interface.get("source_id") or ""))
        reference_sources.discard("")
        if len(reference_sources) != 1:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} crosses official Sources: "
                f"{sorted(reference_sources)}"
            )
        expected_source_id = next(iter(reference_sources))
        if str(binding.get("source_id") or "") != expected_source_id:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} must define exactly one explicit "
                f"source_id={expected_source_id!r}"
            )
        official_urls: list[str] = []
        contract_rows: list[dict[str, Any]] = []
        for contract_id in contract_ids:
            contract = contracts.get(contract_id) or {}
            contract_rows.append(contract)
            official_urls.extend(contract.get("official_sources") or [])
        profile_id = str(interface.get("profile_id") or "")
        profile = profiles.get(profile_id) or {}
        if not official_urls:
            official_urls.extend(profile.get("official_sources") or [])
        api_forms = {
            str((contracts.get(contract_id) or {}).get("api_form") or "")
            for contract_id in contract_ids
        }
        if interface_id:
            api_forms.add(str(interface.get("api_form") or ""))
        api_forms.discard("")
        if len(api_forms) != 1:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} crosses API forms: "
                f"{sorted(api_forms)}"
            )
        api_form = next(iter(api_forms))
        identity = {
            "source_id": expected_source_id,
            "test_binding_id": str(binding_id),
        }
        if interface_id:
            identity.update(
                {
                    "profile_id": profile_id,
                    "interface_id": interface_id,
                    "request_model_ids": list(
                        interface.get("request_model_ids")
                        or profile.get("request_model_ids")
                        or []
                    ),
                }
            )
        _validate_provenance_record(
            record_id=record_id,
            record=records.get(record_id),
            sources=sources,
            expected_source_id=expected_source_id,
            expected_target_type="test_binding",
            expected_target_id=str(binding_id),
            expected_fields=_provenance_field_names(binding),
            expected_official_urls=official_urls,
            expected_api_form=api_form,
            expected_parameters=_provenance_parameters(
                binding, *contract_rows
            ),
            expected_identity=identity,
        )
    orphan_records = sorted(set(records) - used)
    if orphan_records:
        raise CatalogValidationError(
            "Test Binding provenance ledger contains orphan records: "
            f"{orphan_records}"
        )


@lru_cache(maxsize=1)
def _workflow_schema_validator() -> Any:
    # Workflows are an optional extension. Existing core-only readers retain
    # their lightweight validation path; workflow readers validate the complete
    # descriptor shape without importing any project runner or user handler.
    import jsonschema

    schema = json.loads(TEST_EXTENSIONS_SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator({
        "$ref": "#/$defs/testWorkflow", "$defs": schema["$defs"],
    })


def _validate_workflow_binding(
    binding_id: str, binding: dict[str, Any], payload: dict[str, Any]
) -> None:
    errors = sorted(
        _workflow_schema_validator().iter_errors(binding),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise CatalogValidationError(
            f"Workflow {binding_id!r} schema: {errors[0].message}"
        )
    profiles = payload.get("profiles") or {}
    interfaces = payload.get("interfaces") or {}
    contracts = payload.get("contracts") or {}
    profile = profiles.get(binding["profile_id"]) or {}
    interface = interfaces.get(binding["interface_id"]) or {}
    contract = contracts.get(binding["contract_id"]) or {}
    source = binding["source_id"]
    if (
        not profile or not interface or not contract
        or profile.get("source_id") != source
        or interface.get("source_id") != source
        or _contract_source_id(contract) != source
        or interface.get("profile_id") != binding["profile_id"]
        or binding["interface_id"] not in profile.get("interface_ids", [])
        or binding["contract_id"] not in interface.get("contract_ids", [])
        or contract.get("api_form") != interface.get("api_form")
    ):
        raise CatalogValidationError(
            f"Workflow {binding_id!r} requires one exact official reference identity"
        )
    target = binding["execution_target"]
    permission = binding["execution_permission"]
    mapping = binding.get("gateway_mapping")
    media_approval = is_approved_media_input_workflow(binding)
    if permission.get("approval_id") == MEDIA_INPUT_VALIDATION_APPROVAL_ID and not media_approval:
        raise CatalogValidationError(
            f"Workflow {binding_id!r} is outside the exact official media validation approval"
        )
    if (
        interface.get("api_form") == "gemini_interactions"
        or target["api_form"] == "gemini_interactions"
        or target["api_form"].startswith("aws_bedrock")
        or target["transport_adapter_id"].startswith("bedrock_")
    ):
        raise CatalogValidationError(
            f"Workflow {binding_id!r} cannot enable ordinary Gemini Interactions or native AWS"
        )
    # These entries describe reviewed gateway mappings, not native transport
    # certification. Merely setting approved=true cannot authorize a new mapping.
    model = profile.get("model_slug")
    fable_model = model in {"claude-fable-5", "claude-fable-5-1"}
    contract_prefix = "claude_fable_5_1" if model == "claude-fable-5-1" else "claude_fable"
    if mapping:
        mapping_id = mapping["mapping_id"]
        aws = mapping_id == "awsb_fable_messages_v1"
        expected_source = "aws_bedrock" if aws else "anthropic"
        expected_interface_slug = "bedrock-runtime-messages-default" if aws else "anthropic-messages-default"
        expected_contract = contract_prefix + ("_aws_bedrock_runtime_messages" if aws else "_native_messages")
        expected_target = {
            "provider_id": "inferenceai_awsb" if aws else "sanqiaoapi",
            "request_model_id": model,
            "api_form": "anthropic_messages",
            "transport_adapter_id": "claude_messages",
        }
        if (
            not fable_model
            or source != expected_source
            or binding["profile_id"] != f"text/{expected_source}/claude_fable/{model}"
            or binding["interface_id"] != binding["profile_id"] + "#" + expected_interface_slug
            or binding["contract_id"] != expected_contract
            or target != expected_target
            or permission["mode"] != "scoped_research"
        ):
            raise CatalogValidationError(
                f"Workflow {binding_id!r} does not match the approved gateway mapping"
            )
    elif (
        target["request_model_id"] not in (
            interface.get("request_model_ids") or profile.get("request_model_ids") or []
        )
        or target["api_form"] != interface.get("api_form")
        or target["transport_adapter_id"] != interface.get("transport_adapter_id")
    ):
        raise CatalogValidationError(
            f"Workflow {binding_id!r} execution identity requires an approved gateway mapping"
        )
    if permission["mode"] == "scoped_research":
        if media_approval and profile.get("lifecycle") in {"retired", "discontinued"}:
            raise CatalogValidationError(
                f"Workflow {binding_id!r} cannot activate a retired media model"
            )
        if not media_approval and not mapping and not (
            fable_model and source == "anthropic"
            and binding["profile_id"] == f"text/anthropic/claude_fable/{model}"
            and binding["interface_id"] == binding["profile_id"] + "#anthropic-messages-default"
            and binding["contract_id"] == contract_prefix + "_native_messages"
            and target["provider_id"] == "anthropic_official"
        ):
            raise CatalogValidationError(
                f"Workflow {binding_id!r} has no exact scoped research permission"
            )
    else:
        policies = [
            row for row in (payload.get("test_bindings") or {}).values()
            if row.get("extension_type") == "model_test_policy"
            and row.get("interface_id") == binding["interface_id"]
            and binding["contract_id"] in (row.get("reference_contract_ids") or [])
        ]
        if (
            profile.get("profile_state", "executable") != "executable"
            or interface.get("enabled", True) is not True
            or interface.get("executable", True) is not True
            or interface.get("test_binding_status", "required") != "required"
            or contract.get("test_binding_status", "required") != "required"
            or any(contract.get(field, True) is not True for field in ("enabled", "executable"))
            or not policies
            or any(row.get("parameter_test_enabled") is not True for row in policies)
            or any(str(row.get("disabled_reason") or "").strip() for row in policies)
            or any(
                row.get(field, True) is not True
                for row in policies for field in ("enabled", "executable", "runner_enabled")
            )
        ):
            raise CatalogValidationError(
                f"Workflow {binding_id!r} cannot bypass disabled reference policy"
            )
    workflow = binding["workflow"]
    if "factory_id" not in workflow:
        if workflow["id"] != binding["workflow_id"] or workflow["target"] != {
            "source_id": source,
            "profile_id": binding["profile_id"],
            "interface_id": binding["interface_id"],
            "contract_id": binding["contract_id"],
            "execution_target": target,
        }:
            raise CatalogValidationError(
                f"Workflow {binding_id!r} definition target or id does not match its binding"
            )


def validate_catalog(payload: dict[str, Any], *, compiled: bool | None = None) -> None:
    if not isinstance(payload, dict):
        raise CatalogValidationError("Catalog must be an object")
    # The digest identifies the serialized artifact as published.  Legacy
    # schema-v2/v3 artifacts may omit the later media safety fields, so verify
    # their declared digest before applying fail-closed compatibility defaults.
    declared_digest = str(payload.get("catalog_digest") or "")
    raw_digest = catalog_digest(payload) if declared_digest else ""
    payload = _with_media_safety_defaults(payload)
    version = int(payload.get("mpdb_schema_version") or 0)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CatalogValidationError(f"Unsupported mpdb_schema_version={version}")
    if compiled is True:
        core_format_version = int(
            payload.get("compiled_core_format_version") or 0
        )
        if core_format_version != COMPILED_CORE_FORMAT_VERSION:
            raise CatalogValidationError(
                "Unsupported compiled_core_format_version="
                f"{core_format_version}; expected "
                f"{COMPILED_CORE_FORMAT_VERSION}"
            )
    if not str(payload.get("catalog_version") or "").strip():
        raise CatalogValidationError("catalog_version is required")
    identity_contract = payload.get("identity_contract") or {}
    if not isinstance(identity_contract, dict):
        raise CatalogValidationError("identity_contract must be an object")
    for key, expected in IDENTITY_CONTRACT.items():
        if identity_contract.get(key) != expected:
            raise CatalogValidationError(
                f"identity_contract.{key} must equal {expected!r}"
            )
    errors = list(_walk_for_secrets(payload))
    if errors:
        raise CatalogValidationError("; ".join(errors[:5]))

    sources = payload.get("sources") or {}
    modalities = payload.get("modalities") or {}
    families = payload.get("families") or {}
    models = payload.get("canonical_models") or {}
    contracts = payload.get("contracts") or {}
    profiles = payload.get("profiles") or {}
    route_templates = payload.get("route_templates") or {}
    if not all(
        isinstance(item, dict)
        for item in (
            modalities,
            sources,
            families,
            models,
            contracts,
            profiles,
            route_templates,
        )
    ):
        raise CatalogValidationError("Catalog registries must be objects")

    unknown_modalities = sorted(set(modalities) - SUPPORTED_MODALITIES)
    missing_modalities = sorted(SUPPORTED_MODALITIES - set(modalities))
    if unknown_modalities or missing_modalities:
        raise CatalogValidationError(
            "Catalog modalities must be exactly text, image, and video; "
            f"missing={missing_modalities}, unknown={unknown_modalities}"
        )
    for modality, row in modalities.items():
        if not isinstance(row, dict) or not str(row.get("label") or "").strip():
            raise CatalogValidationError(
                f"Modality {modality!r} must define a label"
            )
        if (
            modality in NON_PRESSURE_MODALITIES
            and row.get("pressure_test_enabled") is not False
        ):
            raise CatalogValidationError(
                f"Modality {modality!r} must set pressure_test_enabled=false"
            )

    for source_id, source in sources.items():
        _check_identifier(str(source_id), label="source_id")
        if not isinstance(source, dict):
            raise CatalogValidationError(
                f"Source {source_id!r} must be an object"
            )
        if not str(source.get("label") or "").strip():
            raise CatalogValidationError(
                f"Source {source_id!r} must define a label"
            )
        source_type = str(source.get("source_type") or "")
        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise CatalogValidationError(
                f"Source {source_id!r} has invalid source_type={source_type!r}"
            )
        authority = str(source.get("authority") or "")
        expected_authority = SOURCE_TYPE_AUTHORITIES[source_type]
        if authority != expected_authority:
            raise CatalogValidationError(
                f"Source {source_id!r} authority must be "
                f"{expected_authority!r}, got {authority!r}"
            )
        official_sources = source.get("official_sources") or []
        if not isinstance(official_sources, list) or not official_sources:
            raise CatalogValidationError(
                f"Source {source_id!r} must define official_sources"
            )
        official_domains = source.get("official_domains") or []
        if not isinstance(official_domains, list) or not official_domains:
            raise CatalogValidationError(
                f"Source {source_id!r} must define unique official_domains"
            )
        normalized_official_domains = [
            str(item).strip().casefold() for item in official_domains
        ]
        if len(normalized_official_domains) != len(
            set(normalized_official_domains)
        ):
            raise CatalogValidationError(
                f"Source {source_id!r} must define unique official_domains"
            )
        invalid_official_domains = [
            str(domain)
            for domain in official_domains
            if str(domain) != str(domain).strip()
            or not OFFICIAL_DOMAIN_RE.fullmatch(str(domain).casefold())
        ]
        if invalid_official_domains:
            raise CatalogValidationError(
                f"Source {source_id!r} has invalid official_domains: "
                f"{invalid_official_domains}"
            )
        official_url_prefixes = source.get("official_url_prefixes") or []
        if not isinstance(official_url_prefixes, list) or len(
            official_url_prefixes
        ) != len(set(str(item) for item in official_url_prefixes)):
            raise CatalogValidationError(
                f"Source {source_id!r} official_url_prefixes must be a "
                "unique list"
            )
        invalid_official_url_prefixes = [
            str(prefix)
            for prefix in official_url_prefixes
            if not _is_valid_official_url_prefix(urlparse(str(prefix)))
        ]
        if invalid_official_url_prefixes:
            raise CatalogValidationError(
                f"Source {source_id!r} has invalid official_url_prefixes: "
                f"{invalid_official_url_prefixes}"
            )
        unmatched_source_urls = [
            str(url)
            for url in official_sources
            if not _official_url_matches_source(str(url), source)
        ]
        if unmatched_source_urls:
            raise CatalogValidationError(
                f"Source {source_id!r} has URLs outside official_domains: "
                f"{unmatched_source_urls}"
            )
        request_model_id_semantics = str(
            source.get("request_model_id_semantics") or ""
        )
        if request_model_id_semantics not in SUPPORTED_REQUEST_MODEL_ID_SEMANTICS:
            raise CatalogValidationError(
                f"Source {source_id!r} has invalid request_model_id_semantics="
                f"{request_model_id_semantics!r}"
            )
    for family_id, family in families.items():
        _check_identifier(str(family_id), label="family_id")
        if not isinstance(family, dict) or not str(family.get("label") or "").strip():
            raise CatalogValidationError(
                f"Family {family_id!r} must be an object with a label"
            )
    for model_id, model in models.items():
        _check_identifier(str(model_id), label="canonical_model_id")
        if not isinstance(model, dict):
            raise CatalogValidationError(
                f"Canonical model {model_id!r} must be an object"
            )
        family_id = str(model.get("family_id") or "")
        owner_source_id = str(model.get("owner_source_id") or "")
        model_slug = str(model.get("model_slug") or "")
        if family_id not in families:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} references unknown family {family_id!r}"
            )
        if owner_source_id not in sources:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} references unknown owner source "
                f"{owner_source_id!r}"
            )
        expected_model_id = f"{family_id}/{model_slug}"
        if not model_slug or model_id != expected_model_id:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} does not match identity {expected_model_id!r}"
            )
        model_modalities = list(model.get("modalities") or [])
        if not model_modalities or len(model_modalities) != len(set(model_modalities)):
            raise CatalogValidationError(
                f"Canonical model {model_id!r} modalities must be non-empty and unique"
            )
        invalid_model_modalities = sorted(
            set(str(item) for item in model_modalities) - SUPPORTED_MODALITIES
        )
        if invalid_model_modalities:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} has invalid modalities {invalid_model_modalities}"
            )
        official_sources = model.get("official_sources") or []
        if not isinstance(official_sources, list) or not official_sources:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} must define official_sources"
            )
        unmatched_model_urls = [
            str(url)
            for url in official_sources
            if not _official_url_matches_source(
                str(url), sources[owner_source_id]
            )
        ]
        if unmatched_model_urls:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} has URLs outside owner Source "
                f"{owner_source_id!r} official_domains: {unmatched_model_urls}"
            )
        verification_status = str(model.get("verification_status") or "")
        if verification_status != CANONICAL_MODEL_VERIFICATION_STATUS:
            raise CatalogValidationError(
                f"Canonical model {model_id!r} verification_status must be "
                f"{CANONICAL_MODEL_VERIFICATION_STATUS!r}"
            )

    for contract_id, contract in contracts.items():
        if not isinstance(contract, dict):
            raise CatalogValidationError(
                f"Contract {contract_id!r} must be an object"
            )
        official_sources = contract.get("official_sources") or []
        if not isinstance(official_sources, list) or not official_sources:
            raise CatalogValidationError(
                f"Contract {contract_id!r} must define official_sources"
            )
        scalar_source_id = str(contract.get("source_id") or "")
        raw_source_ids = contract.get("source_ids")
        source_ids = raw_source_ids if isinstance(raw_source_ids, list) else []
        if _requires_provenance(payload):
            if (
                not scalar_source_id
                or len(source_ids) != 1
                or source_ids != [scalar_source_id]
                or scalar_source_id not in sources
            ):
                raise CatalogValidationError(
                    f"Contract {contract_id!r} must define one scalar source_id "
                    "and the exact matching single-item source_ids compatibility "
                    "alias"
                )
        elif (
            len(source_ids) != 1
            or str(source_ids[0] or "") not in sources
            or (
                scalar_source_id
                and scalar_source_id != str(source_ids[0] or "")
            )
        ):
            raise CatalogValidationError(
                f"Contract {contract_id!r} must define exactly one known "
                "source_id"
            )
        source_id = _contract_source_id(contract)
        if str(contract.get("verification_status") or "") != (
            CONTRACT_VERIFICATION_STATUS
        ):
            raise CatalogValidationError(
                f"Contract {contract_id!r} verification_status must be "
                f"{CONTRACT_VERIFICATION_STATUS!r}"
            )
        test_binding_status = str(
            contract.get("test_binding_status") or "required"
        )
        if test_binding_status not in SUPPORTED_TEST_BINDING_STATUSES:
            raise CatalogValidationError(
                f"Contract {contract_id!r} has invalid test_binding_status="
                f"{test_binding_status!r}"
            )
        unmatched_contract_urls = [
            str(url)
            for url in official_sources
            if not _official_url_matches_source(
                str(url), sources[source_id]
            )
        ]
        if unmatched_contract_urls:
            raise CatalogValidationError(
                f"Contract {contract_id!r} has URLs outside Source "
                f"{source_id!r} official_domains: {unmatched_contract_urls}"
            )
        parent_contract_id = str(
            contract.get("parent_contract_id") or ""
        )
        if parent_contract_id:
            parent_contract = contracts.get(parent_contract_id) or {}
            parent_source_id = _contract_source_id(parent_contract)
            if parent_source_id != source_id:
                raise CatalogValidationError(
                    f"Contract {contract_id!r} cannot inherit from "
                    f"cross-source Contract {parent_contract_id!r}"
                )
    for contract_id, contract in contracts.items():
        _check_identifier(str(contract_id), label="contract_id")
        if not isinstance(contract, dict):
            raise CatalogValidationError(
                f"Contract {contract_id!r} must be an object"
            )
        if not str(contract.get("label") or "").strip():
            raise CatalogValidationError(
                f"Contract {contract_id!r} must define a label"
            )
        api_form = str(contract.get("api_form") or "")
        family_id = str(contract.get("family_id") or "")
        if not api_form:
            raise CatalogValidationError(f"Contract {contract_id!r} lacks api_form")
        if family_id and family_id not in families:
            raise CatalogValidationError(
                f"Contract {contract_id!r} references unknown family {family_id!r}"
            )
        for parameter, state in ((contract or {}).get("parameter_capabilities") or {}).items():
            if isinstance(state, dict):
                state = state.get("state")
            if state not in SUPPORTED_CAPABILITY_STATES:
                raise CatalogValidationError(
                    f"Contract {contract_id!r} parameter {parameter!r} has invalid state {state!r}"
                )

    for template_id, template in route_templates.items():
        _check_identifier(str(template_id), label="route_template_id")
        if not isinstance(template, dict):
            raise CatalogValidationError(
                f"Route template {template_id!r} must be an object"
            )
        modality = str(template.get("modality") or "")
        family_id = str(template.get("family_id") or "")
        routing_mode = str(template.get("routing_mode") or "")
        expected = f"{modality}/{family_id}/{routing_mode}"
        if modality not in SUPPORTED_MODALITIES:
            raise CatalogValidationError(
                f"Route template {template_id!r} has invalid modality={modality!r}"
            )
        if family_id not in families:
            raise CatalogValidationError(
                f"Route template {template_id!r} references unknown family {family_id!r}"
            )
        if not routing_mode or template_id != expected:
            raise CatalogValidationError(
                f"Route template {template_id!r} does not match identity {expected!r}"
            )
        api_forms = template.get("api_forms") or {}
        if not isinstance(api_forms, dict) or not api_forms:
            raise CatalogValidationError(
                f"Route template {template_id!r} requires api_forms"
            )
        for api_form, form in api_forms.items():
            if not isinstance(form, dict):
                raise CatalogValidationError(
                    f"Route template {template_id!r} form {api_form!r} must be an object"
                )
            contract_ids = list(form.get("contract_ids") or [])
            if not contract_ids or len(contract_ids) != len(set(contract_ids)):
                raise CatalogValidationError(
                    f"Route template {template_id!r} form {api_form!r} contract_ids must be non-empty and unique"
                )
            default_contract_id = str(form.get("default_contract_id") or "")
            if default_contract_id not in contract_ids:
                raise CatalogValidationError(
                    f"Route template {template_id!r} form {api_form!r} has an invalid default contract"
                )
            for contract_id in contract_ids:
                if contract_id not in contracts:
                    raise CatalogValidationError(
                        f"Route template {template_id!r} references unknown contract {contract_id!r}"
                    )
                contract = contracts[contract_id]
                if str(contract.get("api_form") or "") != api_form:
                    raise CatalogValidationError(
                        f"Route template {template_id!r} contract {contract_id!r} uses a different API form"
                    )
                contract_family = str(contract.get("family_id") or "")
                if contract_family and contract_family != family_id:
                    raise CatalogValidationError(
                        f"Route template {template_id!r} contract {contract_id!r} uses a different family"
                    )

    interface_registry = payload.get("interfaces") or {}
    scoped_aliases: dict[tuple[str, str, str], str] = {}
    referenced_model_ids: set[str] = set()
    referenced_source_ids: set[str] = set()
    referenced_family_ids: set[str] = set()
    for profile_id, profile in profiles.items():
        _check_identifier(str(profile_id), label="profile_id")
        if not isinstance(profile, dict):
            raise CatalogValidationError(
                f"Profile {profile_id!r} must be an object"
            )
        modality = str(profile.get("modality") or "")
        source_id = str(profile.get("source_id") or "")
        family_id = str(profile.get("family_id") or "")
        model_slug = str(profile.get("model_slug") or "")
        profile_state = str(profile.get("profile_state") or "executable")
        if profile_state not in SUPPORTED_PROFILE_STATES:
            raise CatalogValidationError(
                f"Profile {profile_id!r} has invalid profile_state={profile_state!r}"
            )
        if modality not in SUPPORTED_MODALITIES:
            raise CatalogValidationError(
                f"Profile {profile_id!r} has invalid modality={modality!r}"
            )
        if source_id not in sources:
            raise CatalogValidationError(
                f"Profile {profile_id!r} references unknown source {source_id!r}"
            )
        expected_source_authority = str(
            (sources[source_id] or {}).get("authority") or ""
        )
        profile_source_authority = str(
            profile.get("source_authority") or ""
        )
        if compiled is not False and (
            profile_source_authority != expected_source_authority
        ):
            raise CatalogValidationError(
                f"Profile {profile_id!r} source_authority must match its Source"
            )
        if compiled is False and profile_source_authority and (
            profile_source_authority != expected_source_authority
        ):
            raise CatalogValidationError(
                f"Profile {profile_id!r} source_authority must match its Source"
            )
        if family_id not in families:
            raise CatalogValidationError(
                f"Profile {profile_id!r} references unknown family {family_id!r}"
            )
        canonical_model_id = str(profile.get("canonical_model_id") or "")
        if canonical_model_id not in models:
            raise CatalogValidationError(
                f"Profile {profile_id!r} references unknown model {canonical_model_id!r}"
            )
        canonical_model = models[canonical_model_id]
        if str(canonical_model.get("family_id") or "") != family_id:
            raise CatalogValidationError(
                f"Profile {profile_id!r} family does not match canonical model"
            )
        if str(canonical_model.get("model_slug") or "") != model_slug:
            raise CatalogValidationError(
                f"Profile {profile_id!r} slug does not match canonical model"
            )
        if modality not in (canonical_model.get("modalities") or []):
            raise CatalogValidationError(
                f"Profile {profile_id!r} modality is not declared by canonical model"
            )
        owner_source_id = str(canonical_model.get("owner_source_id") or "")
        if (
            str((sources[source_id] or {}).get("authority") or "")
            == "origin_vendor"
            and source_id != owner_source_id
        ):
            raise CatalogValidationError(
                f"Profile {profile_id!r} uses origin source {source_id!r}, but "
                f"the canonical owner is {owner_source_id!r}"
            )
        referenced_model_ids.add(canonical_model_id)
        referenced_source_ids.add(source_id)
        referenced_family_ids.add(family_id)
        expected = _profile_id(modality, source_id, family_id, model_slug)
        if profile_id != expected:
            raise CatalogValidationError(
                f"Profile {profile_id!r} does not match hierarchy {expected!r}"
            )
        request_model_ids = list(profile.get("request_model_ids") or [])
        if not request_model_ids or len(request_model_ids) != len(
            {str(item).casefold() for item in request_model_ids}
        ):
            raise CatalogValidationError(
                f"Profile {profile_id!r} request_model_ids must be non-empty and unique"
            )
        profile_official_sources = profile.get("official_sources") or []
        if (
            not isinstance(profile_official_sources, list)
            or not profile_official_sources
        ):
            raise CatalogValidationError(
                f"Profile {profile_id!r} must define official_sources"
            )
        unmatched_profile_urls = [
            str(url)
            for url in profile_official_sources
            if not _official_url_matches_source(
                str(url), sources[source_id]
            )
        ]
        if unmatched_profile_urls:
            raise CatalogValidationError(
                f"Profile {profile_id!r} has URLs outside its Source "
                f"official_domains: {unmatched_profile_urls}"
            )
        verification_status = str(profile.get("verification_status") or "")
        if verification_status != PROFILE_VERIFICATION_STATUS:
            raise CatalogValidationError(
                f"Profile {profile_id!r} verification_status must be "
                f"{PROFILE_VERIFICATION_STATUS!r}"
            )
        request_model_id_semantics = str(
            profile.get("request_model_id_semantics") or ""
        )
        if request_model_id_semantics not in SUPPORTED_REQUEST_MODEL_ID_SEMANTICS:
            raise CatalogValidationError(
                f"Profile {profile_id!r} has invalid request_model_id_semantics="
                f"{request_model_id_semantics!r}"
            )
        expected_semantics = str(
            (sources.get(source_id) or {}).get("request_model_id_semantics")
            or "exact_source_model_id"
        )
        if request_model_id_semantics != expected_semantics:
            raise CatalogValidationError(
                f"Profile {profile_id!r} request_model_id_semantics must be "
                f"{expected_semantics!r} for source {source_id!r}"
            )
        for alias in request_model_ids:
            if not str(alias).strip():
                raise CatalogValidationError(
                    f"Profile {profile_id!r} contains an empty request model id"
                )
            key = (modality, source_id, str(alias).casefold())
            previous = scoped_aliases.setdefault(key, str(profile_id))
            if previous != profile_id:
                raise CatalogValidationError(
                    f"Request model alias {alias!r} is ambiguous between {previous!r} and {profile_id!r}"
                )

    orphan_models = sorted(set(models) - referenced_model_ids)
    orphan_sources = sorted(set(sources) - referenced_source_ids)
    orphan_families = sorted(set(families) - referenced_family_ids)
    if orphan_models or orphan_sources or orphan_families:
        raise CatalogValidationError(
            "Every canonical model, source, and family must be reachable from a profile; "
            f"orphan_models={orphan_models}, orphan_sources={orphan_sources}, "
            f"orphan_families={orphan_families}"
        )

    if compiled is not False:
        if not isinstance(interface_registry, dict):
            raise CatalogValidationError("interfaces must be an object")
        profile_interface_ids: set[str] = set()
        for profile_id, profile in profiles.items():
            interface_ids = list((profile or {}).get("interface_ids") or [])
            profile_state = str(
                (profile or {}).get("profile_state") or "executable"
            )
            if len(interface_ids) != len(set(interface_ids)):
                raise CatalogValidationError(
                    f"Profile {profile_id!r} interface_ids must be unique"
                )
            if profile_state != "identity_only" and not interface_ids:
                raise CatalogValidationError(
                    f"Executable Profile {profile_id!r} interface_ids must be non-empty"
                )
            missing_interfaces = sorted(set(interface_ids) - set(interface_registry))
            if missing_interfaces:
                raise CatalogValidationError(
                    f"Profile {profile_id!r} references unknown interfaces {missing_interfaces}"
                )
            profile_interface_ids.update(str(item) for item in interface_ids)
        orphan_interfaces = sorted(set(interface_registry) - profile_interface_ids)
        if orphan_interfaces:
            raise CatalogValidationError(
                f"Interfaces are not reachable from profiles: {orphan_interfaces}"
            )
        for interface_id, interface in interface_registry.items():
            _check_identifier(str(interface_id), label="interface_id", interface=True)
            if not isinstance(interface, dict):
                raise CatalogValidationError(
                    f"Interface {interface_id!r} must be an object"
                )
            if "safety_binding" in interface:
                raise CatalogValidationError(
                    f"Interface {interface_id!r} must not embed project Test "
                    "Binding metadata"
                )
            profile_id = str(interface.get("profile_id") or "")
            if profile_id not in profiles:
                raise CatalogValidationError(
                    f"Interface {interface_id!r} references unknown profile {profile_id!r}"
                )
            profile = profiles[profile_id]
            for field in ("modality", "source_id", "family_id", "model_slug"):
                if interface.get(field) != profile.get(field):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} {field} does not match its profile"
                    )
            api_form = str((interface or {}).get("api_form") or "")
            routing_mode = str((interface or {}).get("routing_mode") or "")
            if not api_form or not routing_mode:
                raise CatalogValidationError(
                    f"Interface {interface_id!r} requires api_form and routing_mode"
                )
            test_binding_status = str(
                (interface or {}).get("test_binding_status") or "required"
            )
            if test_binding_status not in SUPPORTED_TEST_BINDING_STATUSES:
                raise CatalogValidationError(
                    f"Interface {interface_id!r} has invalid test_binding_status="
                    f"{test_binding_status!r}"
                )
            for boolean_field in ("enabled", "executable"):
                if boolean_field in interface and not isinstance(
                    interface[boolean_field], bool
                ):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} {boolean_field} must be boolean"
                    )
            if test_binding_status == "retired" and (
                bool((interface or {}).get("enabled", True))
                or bool((interface or {}).get("executable", True))
            ):
                raise CatalogValidationError(
                    f"Retired Interface {interface_id!r} must be disabled and non-executable"
                )
            default_api_version = str(
                (interface or {}).get("default_api_version") or ""
            )
            raw_api_versions = (interface or {}).get("api_versions")
            if bool(default_api_version) != (raw_api_versions is not None):
                raise CatalogValidationError(
                    f"Interface {interface_id!r} must define default_api_version "
                    "and api_versions together"
                )
            if raw_api_versions is not None:
                if not isinstance(raw_api_versions, dict) or not raw_api_versions:
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} api_versions must be a non-empty object"
                    )
                if default_api_version not in raw_api_versions:
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} default_api_version must name an api_versions variant"
                    )
                for api_version, raw_variant in raw_api_versions.items():
                    if not str(api_version).strip() or not isinstance(
                        raw_variant, dict
                    ):
                        raise CatalogValidationError(
                            f"Interface {interface_id!r} api_versions variants must be named objects"
                        )
                    forbidden_variant_fields = {
                        "endpoint",
                        "base_url",
                        "api_endpoint",
                    } & set(raw_variant)
                    if forbidden_variant_fields:
                        raise CatalogValidationError(
                            f"Interface {interface_id!r} api_versions[{api_version!r}] "
                            "must not contain endpoint or base URL fields"
                        )
                    stability = str(raw_variant.get("stability") or "")
                    if stability not in SUPPORTED_API_VERSION_STABILITIES:
                        raise CatalogValidationError(
                            f"Interface {interface_id!r} api_versions[{api_version!r}] "
                            "stability must be 'stable' or 'beta'"
                        )
                    expected_stability = EXPECTED_API_VERSION_STABILITIES.get(
                        str(api_version)
                    )
                    if expected_stability and stability != expected_stability:
                        raise CatalogValidationError(
                            f"Interface {interface_id!r} api_versions[{api_version!r}] "
                            f"stability must be {expected_stability!r}"
                        )
                    path_template = raw_variant.get("path_template")
                    if path_template is not None and (
                        not isinstance(path_template, str)
                        or not path_template.startswith("/")
                        or "://" in path_template
                    ):
                        raise CatalogValidationError(
                            f"Interface {interface_id!r} api_versions[{api_version!r}] "
                            "path_template must be an absolute URL path"
                        )
            interface_request_model_ids = list(
                (interface or {}).get("request_model_ids") or []
            )
            if interface_request_model_ids:
                normalized_interface_ids = {
                    str(item).casefold() for item in interface_request_model_ids
                }
                if len(interface_request_model_ids) != len(
                    normalized_interface_ids
                ) or any(
                    not str(item).strip()
                    for item in interface_request_model_ids
                ):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} request_model_ids must be "
                        "non-empty strings and unique"
                    )
                normalized_profile_ids = {
                    str(item).casefold()
                    for item in profile.get("request_model_ids") or []
                }
                if not normalized_interface_ids.issubset(
                    normalized_profile_ids
                ):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} request_model_ids must be "
                        "a subset of its Profile request_model_ids"
                    )
            contract_ids = list((interface or {}).get("contract_ids") or [])
            if not contract_ids or len(contract_ids) != len(set(contract_ids)):
                raise CatalogValidationError(
                    f"Interface {interface_id!r} contract_ids must be non-empty and unique"
                )
            default_contract_id = str(
                (interface or {}).get("default_contract_id") or ""
            )
            if default_contract_id and default_contract_id not in contract_ids:
                raise CatalogValidationError(
                    f"Interface {interface_id!r} default contract is not allowed"
                )
            for contract_id in contract_ids:
                if contract_id not in contracts:
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} references unknown contract {contract_id!r}"
                    )
                contract = contracts[contract_id]
                if str(interface.get("source_id") or "") != (
                    _contract_source_id(contract)
                ):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} contract {contract_id!r} "
                        "belongs to a different official source"
                    )
                if str(contract.get("api_form") or "") != api_form:
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} contract {contract_id!r} uses a different API form"
                    )
                contract_family = str(contract.get("family_id") or "")
                if contract_family and contract_family != profile.get("family_id"):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} contract {contract_id!r} uses a different family"
                    )

    for legacy_id, target_id in (payload.get("legacy_aliases") or {}).items():
        if compiled is not False and target_id not in interface_registry:
            raise CatalogValidationError(
                f"Legacy alias {legacy_id!r} references unknown interface {target_id!r}"
            )
    has_extension_version = "test_extension_schema_version" in payload
    has_test_bindings = "test_bindings" in payload
    has_test_provenance = "test_binding_provenance_records" in payload
    if has_extension_version != has_test_bindings:
        raise CatalogValidationError(
            "test_extension_schema_version and test_bindings must be "
            "composed together"
        )
    if has_test_bindings:
        extension_schema_version = int(
            payload.get("test_extension_schema_version") or 0
        )
        if extension_schema_version not in {1, TEST_EXTENSION_SCHEMA_VERSION}:
            raise CatalogValidationError(
                "test_extension_schema_version must be 1 or "
                f"{TEST_EXTENSION_SCHEMA_VERSION}"
            )
        if extension_schema_version >= TEST_EXTENSION_SCHEMA_VERSION and not (
            has_test_provenance
        ):
            raise CatalogValidationError(
                "Test extensions v2 require test_binding_provenance_records"
            )
        if extension_schema_version == 1 and has_test_provenance:
            raise CatalogValidationError(
                "Legacy test extensions v1 cannot declare v2 provenance"
            )
        if not isinstance(payload.get("test_bindings"), dict):
            raise CatalogValidationError("test_bindings must be an object")
    parameter_bindings_by_contract: dict[str, list[str]] = {}
    model_policies_by_interface: dict[str, list[str]] = {}
    safety_metadata_by_interface: dict[str, str] = {}
    workflows_by_id: dict[str, str] = {}
    for binding_id, binding in (payload.get("test_bindings") or {}).items():
        _check_identifier(str(binding_id), label="test_binding_id")
        if not isinstance(binding, dict):
            raise CatalogValidationError(
                f"Test binding {binding_id!r} must be an object"
            )
        interface_id = str(binding.get("interface_id") or "")
        contract_id = str(binding.get("contract_id") or "")
        if compiled is not False and interface_id and interface_id not in interface_registry:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} references unknown interface {interface_id!r}"
            )
        if contract_id and contract_id not in contracts:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} references unknown contract {contract_id!r}"
            )
        extension_type = str((binding or {}).get("extension_type") or "")
        if not extension_type:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} requires extension_type"
            )
        if extension_type == "test_workflow":
            _validate_workflow_binding(str(binding_id), binding, payload)
            workflow_id = binding["workflow_id"]
            if workflow_id in workflows_by_id:
                raise CatalogValidationError(
                    f"Workflow id {workflow_id!r} has multiple bindings"
                )
            workflows_by_id[workflow_id] = str(binding_id)
        if not interface_id and not contract_id:
            raise CatalogValidationError(
                f"Test binding {binding_id!r} must reference an interface or contract"
            )
        if interface_id and contract_id:
            interface_contracts = set(
                (interface_registry.get(interface_id) or {}).get("contract_ids") or []
            )
            if contract_id not in interface_contracts:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} contract does not belong to its interface"
                )
        if extension_type == "parameter":
            if not contract_id:
                raise CatalogValidationError(
                    f"Parameter binding {binding_id!r} requires contract_id"
                )
            test_cases = binding.get("test_cases")
            if (
                not isinstance(test_cases, list)
                or not test_cases
                or any(
                    not isinstance(test_case, str) or not test_case.strip()
                    for test_case in test_cases
                )
                or len(test_cases) != len(set(test_cases))
            ):
                raise CatalogValidationError(
                    f"Parameter binding {binding_id!r} test_cases must be "
                    "non-empty and unique"
                )
            coverage = binding.get("parameter_coverage")
            if not isinstance(coverage, dict):
                raise CatalogValidationError(
                    f"Parameter binding {binding_id!r} requires parameter_coverage"
                )
            contract_parameters = set(
                (contracts[contract_id].get("parameter_capabilities") or {})
            )
            missing_coverage = sorted(
                parameter
                for parameter in contract_parameters
                if not str(coverage.get(parameter) or "").strip()
            )
            if missing_coverage:
                raise CatalogValidationError(
                    f"Parameter binding {binding_id!r} lacks coverage for "
                    f"{missing_coverage}"
                )
            contract_status = str(
                contracts[contract_id].get("test_binding_status")
                or "required"
            )
            if contract_status != "required":
                if contract_status != "not_certified":
                    raise CatalogValidationError(
                        f"Parameter binding {binding_id!r} cannot retain "
                        f"unsupported Contract status={contract_status!r}"
                    )
                false_fields = (
                    "runner_enabled",
                    "enabled",
                    "executable",
                    "parameter_test_enabled",
                    "pressure_test_enabled",
                )
                invalid_false_fields = [
                    field
                    for field in false_fields
                    if binding.get(field) is not False
                ]
                if invalid_false_fields:
                    raise CatalogValidationError(
                        f"Read-only parameter binding {binding_id!r} must set "
                        f"{sorted(invalid_false_fields)}=false"
                    )
                if str(binding.get("test_binding_status") or "") != (
                    contract_status
                ):
                    raise CatalogValidationError(
                        f"Read-only parameter binding {binding_id!r} must "
                        "preserve its Contract test_binding_status"
                    )
                if str(binding.get("execution_state") or "") != (
                    NON_CERTIFIED_PARAMETER_EXECUTION_STATE
                ):
                    raise CatalogValidationError(
                        f"Read-only parameter binding {binding_id!r} must "
                        "remain metadata-only"
                    )
                if not str(binding.get("disabled_reason") or "").strip():
                    raise CatalogValidationError(
                        f"Read-only parameter binding {binding_id!r} requires "
                        "disabled_reason"
                    )
            parameter_bindings_by_contract.setdefault(contract_id, []).append(
                str(binding_id)
            )
        if extension_type == "safety_metadata":
            required_safety_fields = (
                "interface_id",
                "contract_id",
                "source_id",
                "modality",
                "family_id",
                "model_slug",
                "api_form",
                "test_cases",
                "execution_state",
                "runner_enabled",
                "enabled",
                "executable",
                "test_binding_status",
                "parameter_test_enabled",
                "pressure_test_enabled",
                "disabled_reason",
                "certification_scope",
            )
            missing_safety_fields = [
                field for field in required_safety_fields if field not in binding
            ]
            if missing_safety_fields:
                raise CatalogValidationError(
                    f"Safety metadata {binding_id!r} is missing fields: "
                    + ", ".join(missing_safety_fields)
                )
            expected_binding_id = f"safety/{interface_id.replace('#', '/')}"
            if str(binding_id) != expected_binding_id:
                raise CatalogValidationError(
                    f"Safety metadata {binding_id!r} must use binding id "
                    f"{expected_binding_id!r}"
                )
            safety_cases = binding.get("test_cases")
            if (
                not isinstance(safety_cases, list)
                or not safety_cases
                or any(
                    not isinstance(test_case, str) or not test_case.strip()
                    for test_case in safety_cases
                )
                or len(safety_cases) != len(set(safety_cases))
            ):
                raise CatalogValidationError(
                    f"Safety metadata {binding_id!r} test_cases must be "
                    "non-empty and unique"
                )
            for field in (
                "runner_enabled",
                "enabled",
                "executable",
                "parameter_test_enabled",
                "pressure_test_enabled",
            ):
                if binding.get(field) is not False:
                    raise CatalogValidationError(
                        f"Safety metadata {binding_id!r} must set {field}=false"
                    )
            if str(binding.get("test_binding_status") or "") != "not_certified":
                raise CatalogValidationError(
                    f"Safety metadata {binding_id!r} must be not_certified"
                )
            if str(binding.get("execution_state") or "") != (
                "metadata_only_non_migration_runner_gate_required"
            ):
                raise CatalogValidationError(
                    f"Safety metadata {binding_id!r} must remain metadata-only"
                )
            for field in (
                "source_id",
                "modality",
                "family_id",
                "model_slug",
                "api_form",
                "disabled_reason",
                "certification_scope",
            ):
                if not isinstance(binding.get(field), str) or not str(
                    binding.get(field)
                ).strip():
                    raise CatalogValidationError(
                        f"Safety metadata {binding_id!r} {field} must be a "
                        "non-empty string"
                    )
            if compiled is not False:
                safety_interface = interface_registry[interface_id]
                mismatched_owner_fields = {
                    field: {
                        "binding": binding.get(field),
                        "interface": safety_interface.get(field),
                    }
                    for field in (
                        "source_id",
                        "modality",
                        "family_id",
                        "model_slug",
                        "api_form",
                        "disabled_reason",
                        "certification_scope",
                    )
                    if binding.get(field) != safety_interface.get(field)
                }
                if mismatched_owner_fields:
                    raise CatalogValidationError(
                        f"Safety metadata {binding_id!r} does not match its "
                        f"Interface owner: {mismatched_owner_fields}"
                    )
                if (
                    safety_interface.get("enabled") is not False
                    or safety_interface.get("executable") is not False
                    or str(
                        safety_interface.get("test_binding_status") or ""
                    )
                    != "not_certified"
                ):
                    raise CatalogValidationError(
                        f"Safety metadata {binding_id!r} must target a disabled, "
                        "non-executable, not-certified Interface"
                    )
                previous_safety_binding = safety_metadata_by_interface.setdefault(
                    interface_id, str(binding_id)
                )
                if previous_safety_binding != str(binding_id):
                    raise CatalogValidationError(
                        f"Interface {interface_id!r} has multiple Safety metadata "
                        "bindings"
                    )
        if extension_type == "model_test_policy":
            if not interface_id:
                raise CatalogValidationError(
                    f"Model policy {binding_id!r} requires interface_id"
                )
            interface_modality = str(
                (interface_registry.get(interface_id) or {}).get("modality")
                or ""
            )
            interface_status = str(
                (interface_registry.get(interface_id) or {}).get(
                    "test_binding_status"
                )
                or "required"
            )
            if interface_status != "required":
                if interface_status != "not_certified":
                    raise CatalogValidationError(
                        "Interfaces whose test_binding_status is not "
                        "'required' must not have model test policies unless "
                        "they are strictly disabled not-certified metadata; "
                        f"interface={interface_id!r}"
                    )
                interface = interface_registry[interface_id]
                if (
                    interface.get("enabled") is not False
                    or interface.get("executable") is not False
                ):
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must target a "
                        "disabled, non-executable Interface"
                    )
                false_fields = (
                    "runner_enabled",
                    "enabled",
                    "executable",
                    "parameter_test_enabled",
                    "pressure_test_enabled",
                )
                invalid_false_fields = [
                    field
                    for field in false_fields
                    if binding.get(field) is not False
                ]
                if invalid_false_fields:
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must set "
                        f"{sorted(invalid_false_fields)}=false"
                    )
                if str(binding.get("test_binding_status") or "") != (
                    interface_status
                ):
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must preserve "
                        "its Interface test_binding_status"
                    )
                if str(binding.get("execution_state") or "") != (
                    NON_CERTIFIED_PARAMETER_EXECUTION_STATE
                ):
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must remain "
                        "metadata-only"
                    )
                if (
                    not str(binding.get("disabled_reason") or "").strip()
                    or binding.get("disabled_reason")
                    != interface.get("disabled_reason")
                ):
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must preserve "
                        "its Interface disabled_reason"
                    )
            if interface_modality in NON_PRESSURE_MODALITIES:
                if binding.get("pressure_test_enabled") is not False:
                    raise CatalogValidationError(
                        f"Media test binding {binding_id!r} must set "
                        "pressure_test_enabled=false"
                    )
                populated_pressure_fields = [
                    field
                    for field in PRESSURE_POLICY_FIELDS
                    if binding.get(field)
                ]
                if populated_pressure_fields:
                    raise CatalogValidationError(
                        f"Media test binding {binding_id!r} cannot define "
                        "pressure policy fields: "
                        f"{sorted(populated_pressure_fields)}"
                    )
            model_policies_by_interface.setdefault(interface_id, []).append(
                str(binding_id)
            )
            required_policy_fields = (
                "suite_family_id",
                "canonical_family_id",
                "compatibility_family_alias",
                "reference_contract_ids",
                "default_reference_contract_id",
            )
            missing_policy_fields = [
                field
                for field in required_policy_fields
                if field not in binding
            ]
            if missing_policy_fields:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} is missing model policy fields: "
                    + ", ".join(missing_policy_fields)
                )
            if not isinstance(binding.get("suite_family_id"), str) or not str(
                binding.get("suite_family_id")
            ).strip():
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} suite_family_id must be a non-empty string"
                )
            if not isinstance(binding.get("canonical_family_id"), str) or not str(
                binding.get("canonical_family_id")
            ).strip():
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} canonical_family_id must be a non-empty string"
                )
            suite_family_id = str(binding["suite_family_id"])
            canonical_family_id = str(binding["canonical_family_id"])
            if suite_family_id not in families:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} has unknown suite_family_id={suite_family_id!r}"
                )
            if canonical_family_id not in families:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} has unknown canonical_family_id={canonical_family_id!r}"
                )
            if interface_id:
                interface_family_id = str(
                    (interface_registry.get(interface_id) or {}).get(
                        "family_id"
                    )
                    or ""
                )
                if canonical_family_id != interface_family_id:
                    raise CatalogValidationError(
                        f"Test binding {binding_id!r} canonical family does not match its interface"
                    )
            raw_reference_contract_ids = binding.get("reference_contract_ids")
            if not isinstance(raw_reference_contract_ids, list):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} reference_contract_ids must be a list"
                )
            reference_contract_ids = list(raw_reference_contract_ids)
            if (
                not reference_contract_ids
                or any(
                    not isinstance(contract_id, str)
                    or not contract_id.strip()
                    for contract_id in reference_contract_ids
                )
                or len(reference_contract_ids) != len(set(reference_contract_ids))
            ):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} reference_contract_ids must be non-empty and unique"
                )
            if interface_status == "not_certified":
                promoted_contracts = sorted(
                    str(contract_id)
                    for contract_id in reference_contract_ids
                    if str(
                        (contracts.get(str(contract_id)) or {}).get(
                            "test_binding_status"
                        )
                        or "required"
                    )
                    != "not_certified"
                )
                if promoted_contracts:
                    raise CatalogValidationError(
                        f"Read-only model policy {binding_id!r} must only "
                        "reference not-certified Contracts; "
                        f"contracts={promoted_contracts}"
                    )
            default_reference_contract_id = str(
                binding.get("default_reference_contract_id") or ""
            )
            if default_reference_contract_id not in reference_contract_ids:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} has an invalid default reference contract"
                )
            policy_contract_id = str(binding.get("contract_id") or "")
            if (
                policy_contract_id
                and policy_contract_id not in reference_contract_ids
            ):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} contract_id is outside its "
                    "reference contracts"
                )
            pressure_profiles = binding.get("pressure_profiles") or {}
            if not isinstance(pressure_profiles, dict):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} pressure_profiles must be an object"
                )
            foreign_pressure_contracts = sorted(
                str(contract_id)
                for contract_id in pressure_profiles
                if str(contract_id) not in reference_contract_ids
            )
            if foreign_pressure_contracts:
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} pressure_profiles contains "
                    "contracts outside its source-local references: "
                    f"{foreign_pressure_contracts}"
                )
            interface_api_form = str(
                (interface_registry.get(interface_id) or {}).get("api_form")
                or ""
            )
            interface_source_id = str(
                (interface_registry.get(interface_id) or {}).get("source_id")
                or ""
            )
            for reference_contract_id in reference_contract_ids:
                if reference_contract_id not in contracts:
                    raise CatalogValidationError(
                        f"Test binding {binding_id!r} references unknown suite contract {reference_contract_id!r}"
                    )
                reference_contract = contracts[reference_contract_id]
                if _contract_source_id(reference_contract) != interface_source_id:
                    raise CatalogValidationError(
                        f"Test binding {binding_id!r} suite contract belongs "
                        "to a different official source"
                    )
                if interface_api_form and str(
                    reference_contract.get("api_form") or ""
                ) != interface_api_form:
                    raise CatalogValidationError(
                        f"Test binding {binding_id!r} suite contract uses a different API form"
                    )
                reference_family_id = str(
                    reference_contract.get("family_id") or ""
                )
                if reference_family_id and reference_family_id != suite_family_id:
                    raise CatalogValidationError(
                        f"Test binding {binding_id!r} suite contract uses a different suite family"
                    )
            if not isinstance(binding.get("compatibility_family_alias"), bool):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} compatibility_family_alias must be boolean"
                )
            compatibility_alias = binding["compatibility_family_alias"]
            if compatibility_alias != (
                suite_family_id != canonical_family_id
            ):
                raise CatalogValidationError(
                    f"Test binding {binding_id!r} compatibility family marker is inconsistent"
                )

    if compiled is not False and has_test_bindings:
        missing_parameter_contracts = sorted(
            {
                str(contract_id)
                for interface in interface_registry.values()
                if str(
                    (interface or {}).get("test_binding_status") or "required"
                )
                == "required"
                for contract_id in (interface.get("contract_ids") or [])
                if str(
                    (contracts.get(str(contract_id)) or {}).get(
                        "test_binding_status"
                    )
                    or "required"
                )
                == "required"
                if contract_id not in parameter_bindings_by_contract
            }
        )
        if missing_parameter_contracts:
            raise CatalogValidationError(
                "Every required Interface contract requires a parameter test binding; "
                f"missing={missing_parameter_contracts}"
            )
        missing_model_policies = sorted(
            interface_id
            for interface_id, interface in interface_registry.items()
            if str(
                (interface or {}).get("test_binding_status") or "required"
            )
            == "required"
            if interface_id not in model_policies_by_interface
        )
        if missing_model_policies:
            raise CatalogValidationError(
                "Every required Interface requires an explicit model test policy; "
                f"missing={missing_model_policies}"
            )
        missing_policy_contract_bindings = sorted(
            {
                str(contract_id)
                for binding in (payload.get("test_bindings") or {}).values()
                if (binding or {}).get("extension_type")
                == "model_test_policy"
                for contract_id in (
                    (binding or {}).get("reference_contract_ids") or []
                )
                if str(
                    (contracts.get(str(contract_id)) or {}).get(
                        "test_binding_status"
                    )
                    or "required"
                )
                == "required"
                if contract_id not in parameter_bindings_by_contract
            }
        )
        if missing_policy_contract_bindings:
            raise CatalogValidationError(
                "Every model policy reference contract requires a parameter "
                f"test binding; missing={missing_policy_contract_bindings}"
            )

    referenced_contract_ids = {
        str(contract_id)
        for interface in interface_registry.values()
        for contract_id in ((interface or {}).get("contract_ids") or [])
    }
    referenced_contract_ids.update(
        str(contract_id)
        for template in route_templates.values()
        for form in ((template or {}).get("api_forms") or {}).values()
        for contract_id in ((form or {}).get("contract_ids") or [])
    )
    referenced_contract_ids.update(
        str((binding or {}).get("contract_id") or "")
        for binding in (payload.get("test_bindings") or {}).values()
        if (binding or {}).get("contract_id")
    )
    referenced_contract_ids.update(
        str(contract_id)
        for binding in (payload.get("test_bindings") or {}).values()
        for contract_id in ((binding or {}).get("reference_contract_ids") or [])
    )
    referenced_contract_ids.update(
        str((contract or {}).get("parent_contract_id") or "")
        for contract in contracts.values()
        if (contract or {}).get("parent_contract_id")
    )
    orphan_contracts = sorted(set(contracts) - referenced_contract_ids)
    if orphan_contracts:
        raise CatalogValidationError(
            f"Contracts are not reachable from interfaces, templates, or test bindings: {orphan_contracts}"
        )

    _validate_core_provenance(payload, compiled=compiled)
    _validate_api_form_identity_boundary(payload)
    _validate_test_binding_provenance(payload)

    if compiled is not False:
        expected_view = _source_first_view(profiles)
        actual_view = ((payload.get("views") or {}).get(
            "modality_source_family_model"
        ))
        if actual_view != expected_view:
            raise CatalogValidationError(
                "views.modality_source_family_model is missing or stale"
            )
        if not declared_digest:
            raise CatalogValidationError("Compiled catalog_digest is required")
        if declared_digest != raw_digest:
            raise CatalogValidationError(
                f"Compiled catalog_digest mismatch: declared={declared_digest}, actual={raw_digest}"
            )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_sqlite(
    path: Path,
    catalog: dict[str, Any],
    *,
    extension_digest: str = "",
    schema_identities: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    try:
        connection = sqlite3.connect(temp_name)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE modalities (modality TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE sources (source_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE provenance_records (
                    provenance_record_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(target_type, target_id)
                );
                CREATE TABLE families (family_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE canonical_models (
                    canonical_model_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL REFERENCES families(family_id),
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE contracts (
                    contract_id TEXT PRIMARY KEY,
                    family_id TEXT REFERENCES families(family_id),
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    api_form TEXT NOT NULL,
                    parent_contract_id TEXT REFERENCES contracts(contract_id)
                        DEFERRABLE INITIALLY DEFERRED,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE profiles (
                    profile_id TEXT PRIMARY KEY,
                    modality TEXT NOT NULL REFERENCES modalities(modality),
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    family_id TEXT NOT NULL REFERENCES families(family_id),
                    canonical_model_id TEXT NOT NULL REFERENCES canonical_models(canonical_model_id),
                    model_slug TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE interfaces (
                    interface_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                    api_form TEXT NOT NULL,
                    default_contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE route_templates (
                    template_id TEXT PRIMARY KEY,
                    modality TEXT NOT NULL REFERENCES modalities(modality),
                    family_id TEXT NOT NULL REFERENCES families(family_id),
                    routing_mode TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE legacy_aliases (
                    legacy_id TEXT PRIMARY KEY,
                    interface_id TEXT NOT NULL REFERENCES interfaces(interface_id)
                );
                CREATE TABLE contract_sources (
                    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    PRIMARY KEY(contract_id, source_id)
                );
                CREATE TABLE interface_contracts (
                    interface_id TEXT NOT NULL REFERENCES interfaces(interface_id),
                    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                    is_default INTEGER NOT NULL CHECK(is_default IN (0, 1)),
                    PRIMARY KEY(interface_id, contract_id)
                );
                CREATE TABLE route_template_contracts (
                    template_id TEXT NOT NULL REFERENCES route_templates(template_id),
                    api_form TEXT NOT NULL,
                    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                    is_default INTEGER NOT NULL CHECK(is_default IN (0, 1)),
                    PRIMARY KEY(template_id, api_form, contract_id)
                );
                CREATE INDEX profile_hierarchy_idx ON profiles(modality, source_id, family_id, model_slug);
                CREATE INDEX interface_form_idx ON interfaces(profile_id, api_form);
                CREATE INDEX route_template_hierarchy_idx ON route_templates(modality, family_id, routing_mode);
                CREATE INDEX provenance_source_idx ON provenance_records(source_id, target_type);
                """
            )
            metadata = {
                "sqlite_schema_version": str(SQLITE_SCHEMA_VERSION),
                "artifact_format_versions": json.dumps(
                    EXPECTED_ARTIFACT_FORMAT_VERSIONS,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "mpdb_schema_version": str(catalog["mpdb_schema_version"]),
                "catalog_version": str(catalog["catalog_version"]),
                "catalog_digest": str(catalog["catalog_digest"]),
                "test_extension_digest": str(extension_digest),
                "released_at": str(catalog.get("released_at") or ""),
                "identity_contract": json.dumps(
                    catalog["identity_contract"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "provenance_schema_version": str(
                    catalog.get("provenance_schema_version") or ""
                ),
            }
            metadata.update(
                {
                    f"schema_id.{name}": str(schema_id)
                    for name, schema_id in sorted(
                        (schema_identities or {}).items()
                    )
                }
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            connection.executemany(
                "INSERT INTO modalities(modality, payload_json) VALUES (?, ?)",
                [
                    (modality, _canonical_json_bytes(row).decode("utf-8"))
                    for modality, row in sorted(catalog["modalities"].items())
                ],
            )
            for table, key_name, rows in (
                ("sources", "source_id", catalog["sources"]),
                ("families", "family_id", catalog["families"]),
            ):
                connection.executemany(
                    f"INSERT INTO {table}({key_name}, payload_json) VALUES (?, ?)",
                    [
                        (row_id, _canonical_json_bytes(row).decode("utf-8"))
                        for row_id, row in sorted(rows.items())
                    ],
                )
            connection.executemany(
                "INSERT INTO canonical_models(canonical_model_id, family_id, payload_json) VALUES (?, ?, ?)",
                [
                    (
                        model_id,
                        str(row.get("family_id") or ""),
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for model_id, row in sorted(catalog["canonical_models"].items())
                ],
            )
            connection.executemany(
                "INSERT INTO provenance_records(provenance_record_id, source_id, target_type, target_id, retrieved_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        record_id,
                        str(row.get("source_id") or ""),
                        str((row.get("target") or {}).get("type") or ""),
                        str((row.get("target") or {}).get("id") or ""),
                        str(row.get("retrieved_at") or ""),
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for record_id, row in sorted(
                        (catalog.get("provenance_records") or {}).items()
                    )
                ],
            )
            connection.executemany(
                "INSERT INTO contracts(contract_id, family_id, source_id, api_form, parent_contract_id, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        contract_id,
                        str(row.get("family_id"))
                        if row.get("family_id")
                        else None,
                        str(row.get("source_id") or ""),
                        str(row.get("api_form") or ""),
                        str(row.get("parent_contract_id"))
                        if row.get("parent_contract_id")
                        else None,
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for contract_id, row in sorted(catalog["contracts"].items())
                ],
            )
            connection.executemany(
                "INSERT INTO profiles(profile_id, modality, source_id, family_id, canonical_model_id, model_slug, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        profile_id,
                        str(row["modality"]),
                        str(row["source_id"]),
                        str(row["family_id"]),
                        str(row["canonical_model_id"]),
                        str(row["model_slug"]),
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for profile_id, row in sorted(catalog["profiles"].items())
                ],
            )
            connection.executemany(
                "INSERT INTO interfaces(interface_id, profile_id, api_form, default_contract_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        interface_id,
                        str(row["profile_id"]),
                        str(row.get("api_form") or ""),
                        str(row.get("default_contract_id") or ""),
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for interface_id, row in sorted(catalog["interfaces"].items())
                ],
            )
            connection.executemany(
                "INSERT INTO route_templates(template_id, modality, family_id, routing_mode, payload_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        template_id,
                        str(row.get("modality") or ""),
                        str(row.get("family_id") or ""),
                        str(row.get("routing_mode") or ""),
                        _canonical_json_bytes(row).decode("utf-8"),
                    )
                    for template_id, row in sorted(
                        catalog["route_templates"].items()
                    )
                ],
            )
            connection.executemany(
                "INSERT INTO legacy_aliases(legacy_id, interface_id) VALUES (?, ?)",
                sorted(catalog["legacy_aliases"].items()),
            )
            connection.executemany(
                "INSERT INTO contract_sources(contract_id, source_id) VALUES (?, ?)",
                [
                    (contract_id, str(row.get("source_id") or ""))
                    for contract_id, row in sorted(catalog["contracts"].items())
                ],
            )
            connection.executemany(
                "INSERT INTO interface_contracts(interface_id, contract_id, is_default) VALUES (?, ?, ?)",
                [
                    (
                        interface_id,
                        str(contract_id),
                        int(contract_id == row.get("default_contract_id")),
                    )
                    for interface_id, row in sorted(catalog["interfaces"].items())
                    for contract_id in row.get("contract_ids") or []
                ],
            )
            connection.executemany(
                "INSERT INTO route_template_contracts(template_id, api_form, contract_id, is_default) VALUES (?, ?, ?, ?)",
                [
                    (
                        template_id,
                        str(api_form),
                        str(contract_id),
                        int(contract_id == form.get("default_contract_id")),
                    )
                    for template_id, row in sorted(
                        catalog["route_templates"].items()
                    )
                    for api_form, form in sorted(
                        (row.get("api_forms") or {}).items()
                    )
                    for contract_id in form.get("contract_ids") or []
                ],
            )
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise CatalogValidationError(
                    f"SQLite foreign key check failed: {foreign_key_errors[:5]}"
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise CatalogValidationError(
                    f"SQLite integrity check failed: {integrity!r}"
                )
            connection.commit()
        finally:
            connection.close()
        os.replace(temp_name, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogValidationError(f"{label.capitalize()} must be an object: {path}")
    return payload


def _format_validation_path(parts: Iterable[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f"[{json.dumps(str(part), ensure_ascii=False)}]"
    return path


def _validate_json_schema(
    instance: Any,
    schema_path: Path,
    *,
    label: str,
) -> str:
    """Validate one real artifact with the bundled Draft 2020-12 schema.

    Artifact verification is a fail-closed operation.  The normal read/query
    API does not require ``jsonschema``, but build and verify must not silently
    downgrade to the handwritten semantic validator when it is unavailable.
    """

    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - environment dependency guard
        raise CatalogValidationError(
            "Artifact Schema validation requires the 'jsonschema' package"
        ) from exc

    schema = _read_json_object(schema_path, label="artifact schema")
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise CatalogValidationError(
            f"Invalid JSON Schema {schema_path}: {exc.message}"
        ) from exc
    validator = validator_class(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        summaries = [
            f"{_format_validation_path(error.absolute_path)}: {error.message}"
            for error in errors[:5]
        ]
        if len(errors) > len(summaries):
            summaries.append(f"... {len(errors) - len(summaries)} more")
        raise CatalogValidationError(
            f"{label} does not conform to {schema_path.name}: "
            + "; ".join(summaries)
        )
    return str(schema.get("$id") or "")


def _schema_identity(path: Path) -> str:
    payload = _read_json_object(path, label="artifact schema")
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - environment dependency guard
        raise CatalogValidationError(
            "Artifact Schema validation requires the 'jsonschema' package"
        ) from exc
    try:
        validator_class = jsonschema.validators.validator_for(payload)
        validator_class.check_schema(payload)
    except jsonschema.exceptions.SchemaError as exc:
        raise CatalogValidationError(
            f"Invalid JSON Schema {path}: {exc.message}"
        ) from exc
    schema_id = str(payload.get("$id") or "")
    if not schema_id:
        raise CatalogValidationError(f"Artifact schema {path} has no $id")
    return schema_id


def _validated_schema_identities(
    schema_paths: dict[str, Path],
) -> dict[str, str]:
    identities = {
        name: _schema_identity(path)
        for name, path in sorted(schema_paths.items())
    }
    expected = {
        name: EXPECTED_SCHEMA_IDENTITIES[name]
        for name in schema_paths
    }
    if identities != expected:
        raise CatalogValidationError(
            "Artifact schema identities do not match the declared physical "
            f"formats: actual={identities}, expected={expected}"
        )
    return identities


def _artifact_record(
    path: Path,
    *,
    manifest_path: Path,
    schema_id: str,
) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise CatalogValidationError(f"Missing build artifact: {resolved}")
    return {
        "path": os.path.relpath(resolved, manifest_path.resolve().parent),
        "sha256": _sha256_file(resolved),
        "size": resolved.stat().st_size,
        "schema_id": schema_id,
    }


def _sqlite_schema_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index') AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()


def _sqlite_table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]


def _sqlite_table_rows(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[tuple[Any, ...]]:
    quoted_name = '"' + table_name.replace('"', '""') + '"'
    rows = connection.execute(f"SELECT * FROM {quoted_name}").fetchall()
    return sorted(
        rows,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _verify_sqlite_core_parity(
    sqlite_path: Path,
    compiled_core: dict[str, Any],
    *,
    extension_digest: str,
    schema_identities: dict[str, str],
) -> None:
    """Rebuild SQLite from the compiled core and compare every table row."""

    try:
        with tempfile.TemporaryDirectory(prefix="mpdb-sqlite-verify-") as temp_dir:
            expected_path = Path(temp_dir) / "catalog.sqlite3"
            _write_sqlite(
                expected_path,
                compiled_core,
                extension_digest=extension_digest,
                schema_identities=schema_identities,
            )
            actual_uri = f"file:{sqlite_path.resolve().as_posix()}?mode=ro"
            with sqlite3.connect(actual_uri, uri=True) as actual, sqlite3.connect(
                expected_path
            ) as expected:
                if actual.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise CatalogValidationError("SQLite integrity_check failed")
                if actual.execute("PRAGMA foreign_key_check").fetchall():
                    raise CatalogValidationError("SQLite foreign_key_check failed")

                actual_tables = _sqlite_table_names(actual)
                expected_tables = _sqlite_table_names(expected)
                if actual_tables != expected_tables:
                    raise CatalogValidationError(
                        "SQLite table registry does not match compiled-core format: "
                        f"actual={actual_tables}, expected={expected_tables}"
                    )
                if _sqlite_schema_rows(actual) != _sqlite_schema_rows(expected):
                    raise CatalogValidationError(
                        "SQLite table/index schema does not match compiled-core format"
                    )
                for table_name in expected_tables:
                    actual_rows = _sqlite_table_rows(actual, table_name)
                    expected_rows = _sqlite_table_rows(expected, table_name)
                    if len(actual_rows) != len(expected_rows):
                        raise CatalogValidationError(
                            f"SQLite table {table_name!r} row count mismatch: "
                            f"actual={len(actual_rows)}, expected={len(expected_rows)}"
                        )
                    if actual_rows != expected_rows:
                        raise CatalogValidationError(
                            f"SQLite table {table_name!r} payload/relationship rows "
                            "do not match compiled core"
                        )
    except CatalogValidationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CatalogValidationError(
            f"Cannot verify SQLite artifact {sqlite_path}: {exc}"
        ) from exc


def verify_artifact_manifest(
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed on schema, hash, version, and cross-format mismatches."""

    target = Path(manifest_path) if manifest_path else MANIFEST_PATH
    manifest = _read_json_object(target, label="artifact manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CatalogValidationError("Manifest artifacts must be an object")

    has_extensions = bool(manifest.get("test_extension_digest"))
    expected_artifact_names = set(REQUIRED_MANIFEST_ARTIFACTS)
    if has_extensions:
        expected_artifact_names.update(OPTIONAL_MANIFEST_ARTIFACTS)
    actual_artifact_names = set(artifacts)
    if actual_artifact_names != expected_artifact_names:
        raise CatalogValidationError(
            "Manifest artifact registry mismatch: "
            f"missing={sorted(expected_artifact_names - actual_artifact_names)}, "
            f"unexpected={sorted(actual_artifact_names - expected_artifact_names)}"
        )
    if manifest.get("artifact_format_versions") != (
        EXPECTED_ARTIFACT_FORMAT_VERSIONS
    ):
        raise CatalogValidationError(
            "Unsupported artifact_format_versions: "
            f"actual={manifest.get('artifact_format_versions')!r}, "
            f"expected={EXPECTED_ARTIFACT_FORMAT_VERSIONS!r}"
        )

    resolved_artifacts: dict[str, Path] = {}
    seen_paths: set[Path] = set()
    for name, raw in sorted(artifacts.items()):
        if not isinstance(raw, dict):
            raise CatalogValidationError(
                f"Manifest artifact {name!r} must be an object"
            )
        declared_path = Path(str(raw.get("path") or ""))
        if declared_path.is_absolute():
            raise CatalogValidationError(
                f"Manifest artifact {name!r} path must be relative"
            )
        artifact_path = (target.parent / declared_path).resolve()
        if artifact_path == target.resolve():
            raise CatalogValidationError(
                f"Manifest artifact {name!r} must not reference the manifest itself"
            )
        if artifact_path in seen_paths:
            raise CatalogValidationError(
                f"Manifest artifact {name!r} reuses path {artifact_path}"
            )
        seen_paths.add(artifact_path)
        if not artifact_path.is_file():
            raise CatalogValidationError(
                f"Manifest artifact {name!r} is missing: {artifact_path}"
            )
        if artifact_path.stat().st_size != raw.get("size"):
            raise CatalogValidationError(
                f"Manifest artifact {name!r} size mismatch"
            )
        if _sha256_file(artifact_path) != raw.get("sha256"):
            raise CatalogValidationError(
                f"Manifest artifact {name!r} sha256 mismatch"
            )
        expected_schema_id = EXPECTED_SCHEMA_IDENTITIES[
            ARTIFACT_SCHEMA_KEYS[name]
        ]
        if raw.get("schema_id") != expected_schema_id:
            raise CatalogValidationError(
                f"Manifest artifact {name!r} schema identity mismatch: "
                f"actual={raw.get('schema_id')!r}, "
                f"expected={expected_schema_id!r}"
            )
        resolved_artifacts[name] = artifact_path

    schema_paths = {
        "source_catalog": resolved_artifacts["source_catalog_schema"],
        "compiled_core": resolved_artifacts["compiled_core_schema"],
        "test_extensions": resolved_artifacts["test_extensions_schema"],
        "artifact_manifest": resolved_artifacts["artifact_manifest_schema"],
    }
    schema_identities = _validated_schema_identities(schema_paths)
    schema_identities["sqlite"] = SQLITE_SCHEMA_ID

    manifest_schema_id = _validate_json_schema(
        manifest,
        schema_paths["artifact_manifest"],
        label="artifact manifest",
    )
    if manifest_schema_id != ARTIFACT_MANIFEST_SCHEMA_ID:
        raise CatalogValidationError(
            "Artifact manifest validator has an unexpected schema identity"
        )

    source_path = resolved_artifacts["source_yaml"]
    source = _read_yaml(source_path)
    source_schema_id = _validate_json_schema(
        source,
        schema_paths["source_catalog"],
        label="source catalog",
    )
    if source_schema_id != SOURCE_CATALOG_SCHEMA_ID:
        raise CatalogValidationError(
            "Source catalog validator has an unexpected schema identity"
        )

    core_path = resolved_artifacts["json"]
    compiled_core = _read_json_object(core_path, label="compiled core")
    core_schema_id = _validate_json_schema(
        compiled_core,
        schema_paths["compiled_core"],
        label="compiled core",
    )
    if core_schema_id != COMPILED_CORE_SCHEMA_ID:
        raise CatalogValidationError(
            "Compiled-core validator has an unexpected schema identity"
        )
    validate_catalog(compiled_core, compiled=True)

    manifest_metadata = {
        "mpdb_schema_version": compiled_core.get("mpdb_schema_version"),
        "catalog_version": compiled_core.get("catalog_version"),
        "catalog_digest": compiled_core.get("catalog_digest"),
        "generated_at": compiled_core.get("released_at") or "",
        "identity_contract": compiled_core.get("identity_contract"),
    }
    actual_manifest_metadata = {
        key: manifest.get(key) for key in manifest_metadata
    }
    if actual_manifest_metadata != manifest_metadata:
        raise CatalogValidationError(
            "Manifest metadata does not match compiled core"
        )
    if source.get("mpdb_schema_version") != manifest.get(
        "mpdb_schema_version"
    ):
        raise CatalogValidationError(
            "Source catalog mpdb_schema_version does not match manifest"
        )
    if source.get("catalog_version") != manifest.get("catalog_version"):
        raise CatalogValidationError(
            "Source catalog catalog_version does not match manifest"
        )
    if source.get("identity_contract") != manifest.get("identity_contract"):
        raise CatalogValidationError(
            "Source catalog identity_contract does not match manifest"
        )

    rebuilt_core = compile_catalog(source)
    if rebuilt_core != compiled_core:
        raise CatalogValidationError(
            "Compiled core does not match its source catalog"
        )

    extensions: dict[str, Any] | None = None
    if has_extensions:
        extension_path = resolved_artifacts["test_extensions_yaml"]
        extensions = load_test_extensions(extension_path)
        extension_schema_id = _validate_json_schema(
            extensions,
            schema_paths["test_extensions"],
            label="test extensions",
        )
        if extension_schema_id != TEST_EXTENSIONS_SCHEMA_ID:
            raise CatalogValidationError(
                "Test-extension validator has an unexpected schema identity"
            )
        actual_extension_digest = test_extension_digest(extensions)
        if actual_extension_digest != manifest.get("test_extension_digest"):
            raise CatalogValidationError(
                "Test extension digest does not match manifest"
            )
        composed = compose_catalog(compiled_core, extensions)
        if composed.get("test_extension_digest") != actual_extension_digest:
            raise CatalogValidationError(
                "Composed test extension digest is inconsistent"
            )

    expected_counts = {
        "profiles": len(compiled_core.get("profiles") or {}),
        "interfaces": len(compiled_core.get("interfaces") or {}),
        "contracts": len(compiled_core.get("contracts") or {}),
        "test_bindings": len((extensions or {}).get("test_bindings") or {}),
        "provenance_records": len(
            compiled_core.get("provenance_records") or {}
        ),
        "test_binding_provenance_records": len(
            (extensions or {}).get("provenance_records") or {}
        ),
    }
    if manifest.get("counts") != expected_counts:
        raise CatalogValidationError("Manifest registry counts are stale")

    _verify_sqlite_core_parity(
        resolved_artifacts["sqlite"],
        compiled_core,
        extension_digest=str(manifest.get("test_extension_digest") or ""),
        schema_identities=schema_identities,
    )
    return manifest


def build_catalog(
    source_path: str | Path | None = None,
    *,
    test_extensions_path: str | Path | None = None,
    output_json: str | Path | None = None,
    output_sqlite: str | Path | None = None,
    output_manifest: str | Path | None = None,
) -> dict[str, Any]:
    resolved_source_path = Path(source_path) if source_path else SOURCE_CATALOG_PATH
    resolved_extensions_path = (
        Path(test_extensions_path)
        if test_extensions_path
        else resolved_source_path.with_name(TEST_EXTENSIONS_PATH.name)
    )
    source = load_source_catalog(resolved_source_path)
    _validate_json_schema(
        source,
        SOURCE_CATALOG_SCHEMA_PATH,
        label="source catalog",
    )
    compiled = compile_catalog(source)
    _validate_json_schema(
        compiled,
        COMPILED_CORE_SCHEMA_PATH,
        label="compiled core",
    )
    extensions = (
        load_test_extensions(resolved_extensions_path)
        if resolved_extensions_path.exists()
        else None
    )
    if extensions is not None:
        _validate_json_schema(
            extensions,
            TEST_EXTENSIONS_SCHEMA_PATH,
            label="test extensions",
        )
    composed = (
        compose_catalog(compiled, extensions)
        if extensions is not None
        else compiled
    )
    json_path = Path(output_json) if output_json else COMPILED_CATALOG_PATH
    sqlite_path = Path(output_sqlite) if output_sqlite else SQLITE_CATALOG_PATH
    manifest_path = Path(output_manifest) if output_manifest else MANIFEST_PATH
    _write_json(json_path, compiled)
    schema_identities = _validated_schema_identities(
        {
            "source_catalog": SOURCE_CATALOG_SCHEMA_PATH,
            "compiled_core": COMPILED_CORE_SCHEMA_PATH,
            "test_extensions": TEST_EXTENSIONS_SCHEMA_PATH,
            "artifact_manifest": ARTIFACT_MANIFEST_SCHEMA_PATH,
        }
    )
    schema_identities["sqlite"] = SQLITE_SCHEMA_ID
    extension_digest = (
        test_extension_digest(extensions) if extensions is not None else ""
    )
    _write_sqlite(
        sqlite_path,
        compiled,
        extension_digest=extension_digest,
        schema_identities=schema_identities,
    )
    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "schema_id": schema_identities["artifact_manifest"],
        "artifact_format_versions": copy.deepcopy(
            EXPECTED_ARTIFACT_FORMAT_VERSIONS
        ),
        "mpdb_schema_version": compiled["mpdb_schema_version"],
        "catalog_version": compiled["catalog_version"],
        "catalog_digest": compiled["catalog_digest"],
        "test_extension_digest": extension_digest,
        "generated_at": compiled.get("released_at") or "",
        "identity_contract": copy.deepcopy(compiled["identity_contract"]),
        "counts": {
            "profiles": len(compiled["profiles"]),
            "interfaces": len(compiled["interfaces"]),
            "contracts": len(compiled["contracts"]),
            "test_bindings": len((extensions or {}).get("test_bindings") or {}),
            "provenance_records": len(
                compiled.get("provenance_records") or {}
            ),
            "test_binding_provenance_records": len(
                (extensions or {}).get("provenance_records") or {}
            ),
        },
        "artifacts": {
            "source_yaml": _artifact_record(
                resolved_source_path,
                manifest_path=manifest_path,
                schema_id=schema_identities["source_catalog"],
            ),
            "json": _artifact_record(
                json_path,
                manifest_path=manifest_path,
                schema_id=schema_identities["compiled_core"],
            ),
            "sqlite": _artifact_record(
                sqlite_path,
                manifest_path=manifest_path,
                schema_id=schema_identities["sqlite"],
            ),
            "source_catalog_schema": _artifact_record(
                SOURCE_CATALOG_SCHEMA_PATH,
                manifest_path=manifest_path,
                schema_id=schema_identities["source_catalog"],
            ),
            "compiled_core_schema": _artifact_record(
                COMPILED_CORE_SCHEMA_PATH,
                manifest_path=manifest_path,
                schema_id=schema_identities["compiled_core"],
            ),
            "test_extensions_schema": _artifact_record(
                TEST_EXTENSIONS_SCHEMA_PATH,
                manifest_path=manifest_path,
                schema_id=schema_identities["test_extensions"],
            ),
            "artifact_manifest_schema": _artifact_record(
                ARTIFACT_MANIFEST_SCHEMA_PATH,
                manifest_path=manifest_path,
                schema_id=schema_identities["artifact_manifest"],
            ),
        },
    }
    if extensions is not None:
        manifest["artifacts"]["test_extensions_yaml"] = _artifact_record(
            resolved_extensions_path,
            manifest_path=manifest_path,
            schema_id=schema_identities["test_extensions"],
        )
    _validate_json_schema(
        manifest,
        ARTIFACT_MANIFEST_SCHEMA_PATH,
        label="artifact manifest",
    )
    _write_json(manifest_path, manifest)
    verify_artifact_manifest(manifest_path)
    return composed


__all__ = [
    "CatalogValidationError",
    "IDENTITY_CONTRACT",
    "build_catalog",
    "catalog_digest",
    "compile_catalog",
    "compose_catalog",
    "load_source_catalog",
    "load_test_extensions",
    "test_extension_digest",
    "validate_catalog",
    "verify_artifact_manifest",
]
