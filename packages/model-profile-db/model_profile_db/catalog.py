from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .compiler import compose_catalog, load_test_extensions, validate_catalog
from .paths import COMPILED_CATALOG_PATH, TEST_EXTENSIONS_PATH


def _profile_with_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(profile)
    result.setdefault("profile_state", "executable")
    return result


def _interface_with_defaults(interface: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(interface)
    result.setdefault("test_binding_status", "required")
    return result


def _modality_with_defaults(modality: str, row: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(row)
    if modality in {"image", "video"}:
        result.setdefault("pressure_test_enabled", False)
    return result


def _test_binding_with_defaults(
    binding: dict[str, Any], interfaces: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(binding)
    interface = interfaces.get(str(result.get("interface_id") or "")) or {}
    if (
        result.get("extension_type") == "model_test_policy"
        and str(interface.get("modality") or "") in {"image", "video"}
    ):
        result.setdefault("pressure_test_enabled", False)
    return result


def _test_binding_context(binding: dict[str, Any]) -> str | None:
    """Return the explicit selector used to distinguish policy contexts.

    ``context`` is the stable selector name exposed by the query API.  Current
    extension data predates that field and uses ``test_scope`` for the same
    purpose, so the latter is accepted as a compatibility spelling.  Neither
    value is inferred from a provider or route.
    """

    value = binding.get("context")
    if value is None:
        value = binding.get("test_scope")
    text = str(value or "").strip()
    return text or None


class ParameterConfigResolutionError(ValueError):
    """A source-first parameter selection is missing or ambiguous."""


class WorkflowResolutionError(ValueError):
    """An exact workflow/reference/execution selection is missing or ambiguous."""


class Catalog:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        include_test_extensions: bool = True,
    ) -> None:
        candidate = copy.deepcopy(payload)
        has_extensions = "test_bindings" in candidate
        if not include_test_extensions and has_extensions:
            candidate.pop("test_bindings", None)
            candidate.pop("test_extension_schema_version", None)
            candidate.pop("test_extension_digest", None)
            has_extensions = False
        validate_catalog(candidate, compiled=True)
        self._payload = candidate
        self._test_extensions_loaded = has_extensions

    @property
    def payload(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)

    @property
    def version(self) -> str:
        return str(self._payload["catalog_version"])

    @property
    def digest(self) -> str:
        return str(self._payload.get("catalog_digest") or "")

    @property
    def identity_contract(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload["identity_contract"])

    @property
    def test_extensions_loaded(self) -> bool:
        return self._test_extensions_loaded

    def database_info(self) -> dict[str, Any]:
        registries = (
            "modalities",
            "sources",
            "families",
            "canonical_models",
            "profiles",
            "interfaces",
            "route_templates",
            "contracts",
            "legacy_aliases",
            "test_bindings",
        )
        return {
            "mpdb_schema_version": self._payload["mpdb_schema_version"],
            "catalog_version": self.version,
            "catalog_digest": self.digest,
            "test_extension_digest": self._payload.get(
                "test_extension_digest"
            ),
            "released_at": self._payload.get("released_at"),
            "identity_contract": self.identity_contract,
            "test_extension_schema_version": self._payload.get(
                "test_extension_schema_version"
            ),
            "test_extensions_loaded": self.test_extensions_loaded,
            "counts": {
                registry: len(self._payload.get(registry) or {})
                for registry in registries
            },
        }

    def list_modalities(self) -> list[dict[str, Any]]:
        return [
            {"modality": modality, **_modality_with_defaults(modality, row)}
            for modality, row in sorted(self._payload["modalities"].items())
        ]

    def list_sources(self, modality: str | None = None) -> list[dict[str, Any]]:
        allowed: set[str] | None = None
        if modality:
            allowed = {
                str(profile["source_id"])
                for profile in self._payload["profiles"].values()
                if profile.get("modality") == modality
            }
        return [
            {"source_id": source_id, **copy.deepcopy(source)}
            for source_id, source in sorted(self._payload["sources"].items())
            if allowed is None or source_id in allowed
        ]

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = self._payload["sources"].get(source_id)
        if not isinstance(source, dict):
            raise KeyError(f"Unknown official reference source {source_id!r}")
        return {"source_id": source_id, **copy.deepcopy(source)}

    def list_families(
        self,
        *,
        modality: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        allowed = {
            str(profile["family_id"])
            for profile in self._payload["profiles"].values()
            if (not modality or profile.get("modality") == modality)
            and (source is None or profile.get("source_id") == source)
        }
        return [
            {"family_id": family_id, **copy.deepcopy(row)}
            for family_id, row in sorted(self._payload["families"].items())
            if family_id in allowed
        ]

    def get_family(self, family_id: str) -> dict[str, Any]:
        family = self._payload["families"].get(family_id)
        if not isinstance(family, dict):
            raise KeyError(f"Unknown model family {family_id!r}")
        return {"family_id": family_id, **copy.deepcopy(family)}

    def list_models(
        self,
        *,
        modality: str | None = None,
        source: str | None = None,
        family: str | None = None,
    ) -> list[dict[str, Any]]:
        matching_profiles = self.list_profiles(
            modality=modality,
            source=source,
            family=family,
        )
        profile_ids_by_model: dict[str, list[str]] = {}
        for profile in matching_profiles:
            model_id = str(profile.get("canonical_model_id") or "")
            profile_ids_by_model.setdefault(model_id, []).append(
                str(profile["profile_id"])
            )
        return [
            {
                "canonical_model_id": model_id,
                **copy.deepcopy(self._payload["canonical_models"][model_id]),
                "profile_ids": sorted(profile_ids),
            }
            for model_id, profile_ids in sorted(profile_ids_by_model.items())
        ]

    def get_model(self, canonical_model_id: str) -> dict[str, Any]:
        model = self._payload["canonical_models"].get(canonical_model_id)
        if not isinstance(model, dict):
            raise KeyError(f"Unknown canonical model {canonical_model_id!r}")
        profile_ids = sorted(
            profile_id
            for profile_id, profile in self._payload["profiles"].items()
            if profile.get("canonical_model_id") == canonical_model_id
        )
        return {
            "canonical_model_id": canonical_model_id,
            **copy.deepcopy(model),
            "profile_ids": profile_ids,
        }

    def list_profiles(
        self,
        *,
        modality: str | None = None,
        source: str | None = None,
        family: str | None = None,
        model: str | None = None,
        api_form: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for profile_id, raw in sorted(self._payload["profiles"].items()):
            if modality and raw.get("modality") != modality:
                continue
            if source is not None and raw.get("source_id") != source:
                continue
            if family and raw.get("family_id") != family:
                continue
            if model:
                requested = str(model).casefold()
                model_ids = {
                    str(item).casefold()
                    for item in (
                        raw.get("model_slug"),
                        raw.get("canonical_model_id"),
                        *(raw.get("request_model_ids") or []),
                    )
                    if item is not None
                }
                if requested not in model_ids:
                    continue
            interface_ids = list(raw.get("interface_ids") or [])
            if api_form and not any(
                self._payload["interfaces"][interface_id].get("api_form") == api_form
                for interface_id in interface_ids
            ):
                continue
            result.append(
                {"profile_id": profile_id, **_profile_with_defaults(raw)}
            )
        return result

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self._payload["profiles"].get(profile_id)
        if not isinstance(profile, dict):
            raise KeyError(f"Unknown model profile {profile_id!r}")
        result = _profile_with_defaults(profile)
        result["interfaces"] = [
            _interface_with_defaults(
                self._payload["interfaces"][interface_id]
            )
            for interface_id in result.get("interface_ids") or []
        ]
        return result

    def get_interface(self, interface_id: str) -> dict[str, Any]:
        interface = self._payload["interfaces"].get(interface_id)
        if not isinstance(interface, dict):
            raise KeyError(f"Unknown model interface {interface_id!r}")
        result = _interface_with_defaults(interface)
        contract_ids = list(result.get("contract_ids") or [])
        result["contracts"] = {
            contract_id: copy.deepcopy(self._payload["contracts"][contract_id])
            for contract_id in contract_ids
        }
        result["test_bindings"] = [
            {
                "test_binding_id": binding_id,
                **_test_binding_with_defaults(binding, self._payload["interfaces"]),
            }
            for binding_id, binding in sorted(
                (self._payload.get("test_bindings") or {}).items()
            )
            if binding.get("interface_id") == interface_id
        ]
        return result

    def list_interfaces(
        self,
        *,
        modality: str | None = None,
        source: str | None = None,
        family: str | None = None,
        model: str | None = None,
        api_form: str | None = None,
        routing_mode: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        profile_ids = {
            str(profile["profile_id"])
            for profile in self.list_profiles(
                modality=modality,
                source=source,
                family=family,
                model=model,
            )
        }
        return [
            _interface_with_defaults(row)
            for _interface_id, row in sorted(self._payload["interfaces"].items())
            if row.get("profile_id") in profile_ids
            and (not api_form or row.get("api_form") == api_form)
            and (not routing_mode or row.get("routing_mode") == routing_mode)
            and (enabled is None or bool(row.get("enabled", True)) is enabled)
        ]

    def list_contracts(
        self,
        *,
        source: str | None = None,
        family: str | None = None,
        api_form: str | None = None,
        routing_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {"contract_id": contract_id, **copy.deepcopy(row)}
            for contract_id, row in sorted(self._payload["contracts"].items())
            if (
                source is None
                or source in {str(value) for value in row.get("source_ids") or []}
            )
            and (not family or row.get("family_id") == family)
            and (not api_form or row.get("api_form") == api_form)
            and (not routing_mode or row.get("routing_mode") == routing_mode)
        ]

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        contract = self._payload["contracts"].get(contract_id)
        if not isinstance(contract, dict):
            raise KeyError(f"Unknown interface contract {contract_id!r}")
        return {"contract_id": contract_id, **copy.deepcopy(contract)}

    def list_test_bindings(
        self,
        *,
        source: str | None = None,
        interface_id: str | None = None,
        contract_id: str | None = None,
        extension_type: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "test_binding_id": binding_id,
                **_test_binding_with_defaults(row, self._payload["interfaces"]),
            }
            for binding_id, row in sorted(
                (self._payload.get("test_bindings") or {}).items()
            )
            if self._test_binding_matches_source(row, source)
            and (not interface_id or row.get("interface_id") == interface_id)
            and (not contract_id or row.get("contract_id") == contract_id)
            and (
                not extension_type
                or row.get("extension_type") == extension_type
            )
        ]

    def _test_binding_matches_source(
        self,
        binding: dict[str, Any],
        source: str | None,
    ) -> bool:
        """Match a binding through its persistent Interface/Contract owner.

        Model-policy bindings are owned by an Interface, while parameter
        bindings are owned by a Contract.  Runtime execution targets and route
        hints are deliberately not consulted.  Missing ownership fails closed
        whenever a source filter was supplied.
        """

        if source is None:
            return True
        interface_id = str(binding.get("interface_id") or "")
        if interface_id:
            interface = self._payload["interfaces"].get(interface_id)
            return (
                isinstance(interface, dict)
                and str(interface.get("source_id") or "") == source
            )
        contract_id = str(binding.get("contract_id") or "")
        contract = self._payload["contracts"].get(contract_id)
        return isinstance(contract, dict) and source in {
            str(value) for value in contract.get("source_ids") or []
        }

    def get_test_binding(self, test_binding_id: str) -> dict[str, Any]:
        binding = (self._payload.get("test_bindings") or {}).get(
            test_binding_id
        )
        if not isinstance(binding, dict):
            raise KeyError(f"Unknown test binding {test_binding_id!r}")
        return {
            "test_binding_id": test_binding_id,
            **_test_binding_with_defaults(binding, self._payload["interfaces"]),
        }

    def _require_test_extensions(self) -> None:
        if not self.test_extensions_loaded:
            raise RuntimeError(
                "Source-first parameter configuration requires the optional "
                "test-extension artifact; reload with "
                "include_test_extensions=True."
            )

    def _get_exact_source_first_profile(
        self,
        *,
        source_id: str,
        modality: str,
        family_id: str,
        model_slug: str,
    ) -> dict[str, Any]:
        # Resolve each registry level before looking up the composite Profile.
        # This deliberately does not accept request-model IDs, aliases, routes,
        # or provider names in place of an exact model_slug/source_id.
        self.get_source(source_id)
        if modality not in self._payload["modalities"]:
            raise KeyError(f"Unknown modality {modality!r}")
        self.get_family(family_id)
        profile_id = f"{modality}/{source_id}/{family_id}/{model_slug}"
        raw_profile = self._payload["profiles"].get(profile_id)
        if not isinstance(raw_profile, dict):
            raise KeyError(
                "No exact source-first Profile for "
                f"source_id={source_id!r}, modality={modality!r}, "
                f"family_id={family_id!r}, model_slug={model_slug!r}"
            )
        expected_identity = {
            "profile_id": profile_id,
            "source_id": source_id,
            "modality": modality,
            "family_id": family_id,
            "model_slug": model_slug,
        }
        mismatches = [
            field
            for field, expected in expected_identity.items()
            if str(raw_profile.get(field) or "") != expected
        ]
        if mismatches:
            raise ParameterConfigResolutionError(
                "Source-first Profile identity mismatch: "
                + ", ".join(sorted(mismatches))
            )
        return {"profile_id": profile_id, **_profile_with_defaults(raw_profile)}

    def resolve_parameter_config(
        self,
        *,
        source_id: str,
        modality: str,
        family_id: str,
        model_slug: str,
        interface_id: str,
        api_form: str,
        test_binding_id: str | None = None,
        suite: str | None = None,
        context: str | None = None,
        contract_id: str | None = None,
        parameter_test_binding_id: str | None = None,
        execution_target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve one executable parameter-test leaf without source inference.

        The caller must provide every persistent identity level through the
        Interface and API form.  A model-policy Test Binding may be omitted
        only when exactly one candidate remains; multiple suites or contexts
        require an explicit selector.  Runtime provider/route information is
        never consulted and, when supplied, is returned only under the opaque
        ``execution_target`` boundary.
        """

        self._require_test_extensions()
        if execution_target is not None and not isinstance(execution_target, dict):
            raise TypeError("execution_target must be an object or None")

        profile = self._get_exact_source_first_profile(
            source_id=source_id,
            modality=modality,
            family_id=family_id,
            model_slug=model_slug,
        )
        if str(profile.get("profile_state") or "executable") != "executable":
            raise ParameterConfigResolutionError(
                f"Profile {profile['profile_id']!r} is not executable"
            )

        raw_interface = self._payload["interfaces"].get(interface_id)
        if not isinstance(raw_interface, dict):
            raise KeyError(f"Unknown model interface {interface_id!r}")
        expected_interface_identity = {
            "profile_id": profile["profile_id"],
            "source_id": source_id,
            "modality": modality,
            "family_id": family_id,
            "model_slug": model_slug,
            "api_form": api_form,
        }
        interface_mismatches = [
            field
            for field, expected in expected_interface_identity.items()
            if str(raw_interface.get(field) or "") != str(expected)
        ]
        if interface_id not in set(profile.get("interface_ids") or []):
            interface_mismatches.append("interface_id")
        if interface_mismatches:
            raise ParameterConfigResolutionError(
                "Interface does not belong to the exact source-first Profile/API: "
                + ", ".join(sorted(set(interface_mismatches)))
            )
        interface = self.get_interface(interface_id)
        interface_status = str(interface.get("test_binding_status") or "required")
        if (
            not bool(interface.get("enabled", True))
            or not bool(interface.get("executable", True))
            or interface_status != "required"
        ):
            raise ParameterConfigResolutionError(
                f"Interface {interface_id!r} is disabled, non-executable, or "
                f"not certified for test binding (status={interface_status!r})"
            )

        policy_candidates = self.list_test_bindings(
            interface_id=interface_id,
            extension_type="model_test_policy",
        )
        if test_binding_id is not None:
            try:
                requested_policy = self.get_test_binding(test_binding_id)
            except KeyError:
                raise KeyError(
                    f"Unknown model-policy test binding {test_binding_id!r}"
                ) from None
            if (
                requested_policy.get("extension_type") != "model_test_policy"
                or requested_policy.get("interface_id") != interface_id
            ):
                raise ParameterConfigResolutionError(
                    f"Test binding {test_binding_id!r} does not belong to "
                    f"Interface {interface_id!r}"
                )
            policy_candidates = [requested_policy]
        if suite is not None:
            policy_candidates = [
                binding
                for binding in policy_candidates
                if str(binding.get("suite") or "") == suite
            ]
        if context is not None:
            policy_candidates = [
                binding
                for binding in policy_candidates
                if _test_binding_context(binding) == context
            ]
        if not policy_candidates:
            raise KeyError(
                "No model-policy Test Binding for the exact Interface, suite, "
                "and context selection"
            )
        if len(policy_candidates) != 1:
            raise ParameterConfigResolutionError(
                "Ambiguous model-policy Test Binding; provide test_binding_id "
                "or explicit suite/context. candidates="
                + repr(
                    sorted(
                        str(binding["test_binding_id"])
                        for binding in policy_candidates
                    )
                )
            )
        policy = policy_candidates[0]
        if not bool(policy.get("parameter_test_enabled", False)):
            raise ParameterConfigResolutionError(
                f"Test binding {policy['test_binding_id']!r} does not enable "
                "parameter tests"
            )
        if str(policy.get("disabled_reason") or "").strip():
            raise ParameterConfigResolutionError(
                f"Test binding {policy['test_binding_id']!r} is disabled"
            )

        reference_contract_ids = [
            str(value) for value in policy.get("reference_contract_ids") or []
        ]
        selected_contract_id = str(
            contract_id or policy.get("default_reference_contract_id") or ""
        )
        if not selected_contract_id:
            raise ParameterConfigResolutionError(
                f"Test binding {policy['test_binding_id']!r} has no explicit "
                "default contract"
            )
        if selected_contract_id not in reference_contract_ids:
            raise ParameterConfigResolutionError(
                f"Contract {selected_contract_id!r} is outside Test Binding "
                f"{policy['test_binding_id']!r}"
            )
        contract = self.get_contract(selected_contract_id)
        contract_source_ids = [
            str(value) for value in contract.get("source_ids") or []
        ]
        contract_status = str(contract.get("test_binding_status") or "required")
        expected_suite_family = str(policy.get("suite_family_id") or family_id)
        contract_mismatches: list[str] = []
        if contract_source_ids != [source_id]:
            contract_mismatches.append("source_ids")
        if str(contract.get("api_form") or "") != api_form:
            contract_mismatches.append("api_form")
        if str(contract.get("family_id") or "") != expected_suite_family:
            contract_mismatches.append("family_id")
        if contract_status != "required":
            contract_mismatches.append("test_binding_status")
        if contract_mismatches:
            raise ParameterConfigResolutionError(
                f"Contract {selected_contract_id!r} is not an executable "
                "source-local parameter contract: "
                + ", ".join(sorted(contract_mismatches))
            )

        parameter_candidates = self.list_test_bindings(
            contract_id=selected_contract_id,
            extension_type="parameter",
        )
        if parameter_test_binding_id is not None:
            try:
                requested_parameter_binding = self.get_test_binding(
                    parameter_test_binding_id
                )
            except KeyError:
                raise KeyError(
                    f"Unknown parameter Test Binding {parameter_test_binding_id!r}"
                ) from None
            if (
                requested_parameter_binding.get("extension_type") != "parameter"
                or requested_parameter_binding.get("contract_id")
                != selected_contract_id
            ):
                raise ParameterConfigResolutionError(
                    f"Parameter Test Binding {parameter_test_binding_id!r} does "
                    f"not belong to Contract {selected_contract_id!r}"
                )
            parameter_candidates = [requested_parameter_binding]
        if not parameter_candidates:
            raise KeyError(
                f"No parameter Test Binding for Contract {selected_contract_id!r}"
            )
        if len(parameter_candidates) != 1:
            raise ParameterConfigResolutionError(
                "Ambiguous parameter Test Binding; provide "
                "parameter_test_binding_id. candidates="
                + repr(
                    sorted(
                        str(binding["test_binding_id"])
                        for binding in parameter_candidates
                    )
                )
            )
        parameter_binding = parameter_candidates[0]
        test_cases = list(parameter_binding.get("test_cases") or [])
        if not test_cases:
            raise ParameterConfigResolutionError(
                f"Parameter Test Binding {parameter_binding['test_binding_id']!r} "
                "has no test cases"
            )

        return {
            "source_id": source_id,
            "modality": modality,
            "family_id": family_id,
            "model_slug": model_slug,
            "profile_id": str(profile["profile_id"]),
            "interface_id": interface_id,
            "api_form": api_form,
            "test_binding_id": str(policy["test_binding_id"]),
            "suite": policy.get("suite"),
            "context": _test_binding_context(policy),
            "contract_id": selected_contract_id,
            "parameter_test_binding_id": str(
                parameter_binding["test_binding_id"]
            ),
            "test_cases": copy.deepcopy(test_cases),
            "profile": copy.deepcopy(profile),
            "interface": copy.deepcopy(interface),
            "test_binding": copy.deepcopy(policy),
            "contract": copy.deepcopy(contract),
            "parameter_test_binding": copy.deepcopy(parameter_binding),
            "catalog_version": self.version,
            "catalog_digest": self.digest,
            "test_extension_digest": self._payload.get(
                "test_extension_digest"
            ),
            "execution_target": copy.deepcopy(execution_target),
            "execution_target_boundary": {
                "included": execution_target is not None,
                "used_for_reference_resolution": False,
            },
        }

    def list_parameter_configs(
        self,
        *,
        source_id: str,
        modality: str | None = None,
        family_id: str | None = None,
        model_slug: str | None = None,
        interface_id: str | None = None,
        api_form: str | None = None,
        test_binding_id: str | None = None,
        suite: str | None = None,
        context: str | None = None,
        contract_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List only uniquely executable source-first parameter leaves."""

        self._require_test_extensions()
        self.get_source(source_id)
        if modality is not None and modality not in self._payload["modalities"]:
            raise KeyError(f"Unknown modality {modality!r}")
        if family_id is not None:
            self.get_family(family_id)

        rows: list[dict[str, Any]] = []
        for raw_profile_id, raw_profile in sorted(self._payload["profiles"].items()):
            if raw_profile.get("source_id") != source_id:
                continue
            if modality is not None and raw_profile.get("modality") != modality:
                continue
            if family_id is not None and raw_profile.get("family_id") != family_id:
                continue
            if model_slug is not None and raw_profile.get("model_slug") != model_slug:
                continue
            for candidate_interface_id in raw_profile.get("interface_ids") or []:
                if interface_id is not None and candidate_interface_id != interface_id:
                    continue
                candidate_interface = self._payload["interfaces"][
                    candidate_interface_id
                ]
                candidate_api_form = str(candidate_interface.get("api_form") or "")
                if api_form is not None and candidate_api_form != api_form:
                    continue
                policies = self.list_test_bindings(
                    interface_id=candidate_interface_id,
                    extension_type="model_test_policy",
                )
                for policy in policies:
                    candidate_policy_id = str(policy["test_binding_id"])
                    if (
                        test_binding_id is not None
                        and candidate_policy_id != test_binding_id
                    ):
                        continue
                    if suite is not None and str(policy.get("suite") or "") != suite:
                        continue
                    if context is not None and _test_binding_context(policy) != context:
                        continue
                    for candidate_contract_id in policy.get(
                        "reference_contract_ids"
                    ) or []:
                        if (
                            contract_id is not None
                            and candidate_contract_id != contract_id
                        ):
                            continue
                        try:
                            resolved = self.resolve_parameter_config(
                                source_id=source_id,
                                modality=str(raw_profile["modality"]),
                                family_id=str(raw_profile["family_id"]),
                                model_slug=str(raw_profile["model_slug"]),
                                interface_id=str(candidate_interface_id),
                                api_form=candidate_api_form,
                                test_binding_id=candidate_policy_id,
                                suite=suite,
                                context=context,
                                contract_id=str(candidate_contract_id),
                            )
                        except (KeyError, ParameterConfigResolutionError):
                            # Listing is an executable projection. Identity-only,
                            # disabled, retired, not-certified, or incomplete
                            # leaves stay in the catalog but are never promoted
                            # into runnable parameter configuration.
                            continue
                        rows.append(
                            {
                                key: copy.deepcopy(resolved[key])
                                for key in (
                                    "source_id",
                                    "modality",
                                    "family_id",
                                    "model_slug",
                                    "profile_id",
                                    "interface_id",
                                    "api_form",
                                    "test_binding_id",
                                    "suite",
                                    "context",
                                    "contract_id",
                                    "parameter_test_binding_id",
                                    "test_cases",
                                )
                            }
                        )
        return rows

    def source_first_parameter_view(
        self, source_id: str | None = None
    ) -> dict[str, Any]:
        """Project executable parameter leaves as source -> modality -> family.

        This is a read-time selection view only. Persistent Profile identity
        remains modality/source/family/model and no ``test_source_id`` registry
        is created.
        """

        self._require_test_extensions()
        source_ids = (
            [source_id]
            if source_id is not None
            else sorted(self._payload["sources"])
        )
        result: dict[str, Any] = {}
        for candidate_source_id in source_ids:
            self.get_source(candidate_source_id)
            source_node: dict[str, Any] = {}
            result[candidate_source_id] = source_node
            for row in self.list_parameter_configs(
                source_id=candidate_source_id
            ):
                model_node = (
                    source_node.setdefault(row["modality"], {})
                    .setdefault(row["family_id"], {})
                    .setdefault(
                        row["model_slug"],
                        {
                            "profile_id": row["profile_id"],
                            "interfaces": {},
                        },
                    )
                )
                interface_node = model_node["interfaces"].setdefault(
                    row["interface_id"],
                    {
                        "api_form": row["api_form"],
                        "test_bindings": {},
                    },
                )
                binding_node = interface_node["test_bindings"].setdefault(
                    row["test_binding_id"],
                    {
                        "extension_type": "model_test_policy",
                        "suite": row["suite"],
                        "context": row["context"],
                        "contracts": {},
                    },
                )
                binding_node["contracts"][row["contract_id"]] = {
                    "parameter_test_binding_id": row[
                        "parameter_test_binding_id"
                    ],
                    "test_cases": copy.deepcopy(row["test_cases"]),
                }
        return result

    def list_workflows(
        self,
        *,
        source_id: str | None = None,
        profile_id: str | None = None,
        interface_id: str | None = None,
        contract_id: str | None = None,
        workflow_id: str | None = None,
        provider_id: str | None = None,
        request_model_id: str | None = None,
        api_form: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List workflow bindings; API/provider filters apply to execution only.

        Official ownership remains the explicit source/profile/interface/contract
        tuple. This projection does not change legacy parameter/policy selection.
        """
        self._require_test_extensions()
        reference_filters = {
            "source_id": source_id, "profile_id": profile_id,
            "interface_id": interface_id, "contract_id": contract_id,
            "workflow_id": workflow_id, "enabled": enabled,
        }
        execution_filters = {
            "provider_id": provider_id, "request_model_id": request_model_id,
            "api_form": api_form,
        }
        return [
            {"test_binding_id": binding_id, **copy.deepcopy(row)}
            for binding_id, row in sorted(self._payload.get("test_bindings", {}).items())
            if row.get("extension_type") == "test_workflow"
            and all(value is None or row.get(key) == value for key, value in reference_filters.items())
            and all(
                value is None or row["execution_target"].get(key) == value
                for key, value in execution_filters.items()
            )
        ]

    def resolve_workflow(
        self,
        *,
        source_id: str,
        profile_id: str,
        interface_id: str,
        contract_id: str,
        execution_target: dict[str, Any],
        workflow_id: str | None = None,
        test_binding_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one enabled workflow with an exact execution identity.

        Execution permissions belong only to this workflow. In particular a
        gateway workflow never enables its official native reference interface.
        The production dispatcher must independently validate provider routing.
        """
        self._require_test_extensions()
        if not isinstance(execution_target, dict) or not execution_target:
            raise WorkflowResolutionError("Workflow requires an exact execution_target")
        if any(not isinstance(value, str) or not value.strip() for value in (
            source_id, profile_id, interface_id, contract_id,
        )):
            raise WorkflowResolutionError("Workflow requires every exact reference selector")
        candidates = [
            row for row in self.list_workflows(
                source_id=source_id, profile_id=profile_id,
                interface_id=interface_id, contract_id=contract_id,
                workflow_id=workflow_id, enabled=True,
            )
            if row["execution_target"] == execution_target
            and (test_binding_id is None or row["test_binding_id"] == test_binding_id)
        ]
        if len(candidates) != 1:
            raise WorkflowResolutionError(
                "Expected exactly one enabled workflow for the exact reference "
                f"and execution identity; found {len(candidates)}"
            )
        result = candidates[0]
        result["reference"] = {
            "source": self.get_source(source_id),
            "profile": self.get_profile(profile_id),
            "interface": self.get_interface(interface_id),
            "contract": self.get_contract(contract_id),
        }
        return result

    def list_route_templates(
        self,
        *,
        modality: str | None = None,
        family: str | None = None,
        routing_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {"template_id": template_id, **copy.deepcopy(row)}
            for template_id, row in sorted(self._payload["route_templates"].items())
            if (not modality or row.get("modality") == modality)
            and (not family or row.get("family_id") == family)
            and (not routing_mode or row.get("routing_mode") == routing_mode)
        ]

    def get_route_template(self, template_id: str) -> dict[str, Any]:
        template = self._payload["route_templates"].get(template_id)
        if not isinstance(template, dict):
            raise KeyError(f"Unknown route template {template_id!r}")
        result = {"template_id": template_id, **copy.deepcopy(template)}
        contract_ids = {
            contract_id
            for form in (template.get("api_forms") or {}).values()
            for contract_id in (form.get("contract_ids") or [])
        }
        result["contracts"] = {
            contract_id: copy.deepcopy(self._payload["contracts"][contract_id])
            for contract_id in sorted(contract_ids)
        }
        return result

    def resolve_request_model(
        self,
        source_id: str,
        request_model_id: str,
        api_form: str | None = None,
        routing_mode: str | None = None,
        *,
        modality: str | None = None,
    ) -> dict[str, Any]:
        candidates = [
            profile
            for profile in self.list_profiles(
                modality=modality,
                source=source_id,
                model=request_model_id,
            )
            if request_model_id.casefold()
            in {str(item).casefold() for item in profile.get("request_model_ids") or []}
        ]
        if not candidates:
            raise KeyError(
                f"No profile for source={source_id!r}, request_model={request_model_id!r}, api_form={api_form!r}"
            )
        executable_candidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("profile_state") or "executable")
            != "identity_only"
        ]
        if not executable_candidates:
            identity_profile_ids = sorted(
                str(candidate["profile_id"]) for candidate in candidates
            )
            raise ValueError(
                f"Model source={source_id!r}, request_model={request_model_id!r} "
                "resolves only to identity_only Profile(s) with no executable "
                f"Interface: {identity_profile_ids}"
            )
        interfaces: list[dict[str, Any]] = []
        for candidate in executable_candidates:
            for interface_id in candidate.get("interface_ids") or []:
                interface = self._payload["interfaces"][interface_id]
                interface_request_ids = list(
                    interface.get("request_model_ids")
                    or candidate.get("request_model_ids")
                    or []
                )
                if (
                    bool(interface.get("enabled", True))
                    and bool(interface.get("executable", True))
                    and (not api_form or interface.get("api_form") == api_form)
                ) and (
                    not routing_mode
                    or interface.get("routing_mode") == routing_mode
                ) and request_model_id.casefold() in {
                    str(item).casefold() for item in interface_request_ids
                }:
                    interfaces.append(interface)
        if not interfaces:
            raise KeyError(
                f"No enabled executable interface for source={source_id!r}, request_model={request_model_id!r}, "
                f"api_form={api_form!r}, routing_mode={routing_mode!r}"
            )
        if len(interfaces) != 1:
            raise ValueError(
                f"Ambiguous model binding for source={source_id!r}, request_model={request_model_id!r}; "
                f"routing_mode={routing_mode!r}; "
                f"interfaces={sorted(item['interface_id'] for item in interfaces)}"
            )
        return self.get_interface(str(interfaces[0]["interface_id"]))

    def resolve_legacy_id(self, legacy_id: str) -> str:
        target = self._payload["legacy_aliases"].get(legacy_id)
        if not target:
            raise KeyError(f"Unknown legacy profile id {legacy_id!r}")
        return str(target)


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Compiled catalog must be an object: {path}")
    return payload


@lru_cache(maxsize=8)
def _load_cached(
    path: str,
    mtime_ns: int,
    include_test_extensions: bool,
    test_extensions_path: str,
    test_extensions_mtime_ns: int,
) -> Catalog:
    del mtime_ns, test_extensions_mtime_ns
    payload = _load_payload(Path(path))
    if include_test_extensions and test_extensions_path:
        payload = compose_catalog(
            payload,
            load_test_extensions(Path(test_extensions_path)),
        )
    return Catalog(
        payload,
        include_test_extensions=include_test_extensions,
    )


def load_catalog(
    path: str | Path | None = None,
    *,
    include_test_extensions: bool = True,
    test_extensions_path: str | Path | None = None,
) -> Catalog:
    target = Path(path) if path else COMPILED_CATALOG_PATH
    if not target.exists():
        raise RuntimeError(
            f"Missing compiled model profile catalog: {target}. Run `mpdb build`."
        )
    resolved = target.resolve()
    extension_target = (
        Path(test_extensions_path)
        if test_extensions_path is not None
        else (
            TEST_EXTENSIONS_PATH
            if path is None
            else target.with_name(TEST_EXTENSIONS_PATH.name)
        )
    )
    resolved_extension = (
        extension_target.resolve()
        if include_test_extensions and extension_target.exists()
        else None
    )
    return _load_cached(
        str(resolved),
        resolved.stat().st_mtime_ns,
        include_test_extensions,
        str(resolved_extension) if resolved_extension else "",
        resolved_extension.stat().st_mtime_ns if resolved_extension else 0,
    )


def database_info() -> dict[str, Any]:
    return load_catalog().database_info()


def list_sources(modality: str | None = None) -> list[dict[str, Any]]:
    return load_catalog().list_sources(modality)


def list_modalities() -> list[dict[str, Any]]:
    return load_catalog().list_modalities()


def list_profiles(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_profiles(**filters)


def list_families(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_families(**filters)


def list_models(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_models(**filters)


def get_profile(profile_id: str) -> dict[str, Any]:
    return load_catalog().get_profile(profile_id)


def get_interface(interface_id: str) -> dict[str, Any]:
    return load_catalog().get_interface(interface_id)


def list_route_templates(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_route_templates(**filters)


def get_route_template(template_id: str) -> dict[str, Any]:
    return load_catalog().get_route_template(template_id)


def get_source(source_id: str) -> dict[str, Any]:
    return load_catalog().get_source(source_id)


def get_family(family_id: str) -> dict[str, Any]:
    return load_catalog().get_family(family_id)


def get_model(canonical_model_id: str) -> dict[str, Any]:
    return load_catalog().get_model(canonical_model_id)


def list_interfaces(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_interfaces(**filters)


def list_contracts(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_contracts(**filters)


def get_contract(contract_id: str) -> dict[str, Any]:
    return load_catalog().get_contract(contract_id)


def list_test_bindings(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_test_bindings(**filters)


def get_test_binding(test_binding_id: str) -> dict[str, Any]:
    return load_catalog().get_test_binding(test_binding_id)


def list_parameter_configs(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_parameter_configs(**filters)


def source_first_parameter_view(
    source_id: str | None = None,
) -> dict[str, Any]:
    return load_catalog().source_first_parameter_view(source_id)


def resolve_parameter_config(**selection: Any) -> dict[str, Any]:
    return load_catalog().resolve_parameter_config(**selection)


def list_workflows(**filters: Any) -> list[dict[str, Any]]:
    return load_catalog().list_workflows(**filters)


def resolve_workflow(**selection: Any) -> dict[str, Any]:
    return load_catalog().resolve_workflow(**selection)


def resolve_request_model(
    source_id: str,
    request_model_id: str,
    api_form: str | None = None,
    routing_mode: str | None = None,
    *,
    modality: str | None = None,
) -> dict[str, Any]:
    return load_catalog().resolve_request_model(
        source_id,
        request_model_id,
        api_form,
        routing_mode,
        modality=modality,
    )


def resolve_legacy_id(legacy_id: str) -> str:
    return load_catalog().resolve_legacy_id(legacy_id)


def diff_catalogs(old: Catalog | dict[str, Any], new: Catalog | dict[str, Any]) -> dict[str, Any]:
    old_payload = old.payload if isinstance(old, Catalog) else copy.deepcopy(old)
    new_payload = new.payload if isinstance(new, Catalog) else copy.deepcopy(new)
    metadata_fields = (
        "mpdb_schema_version",
        "catalog_version",
        "catalog_digest",
        "released_at",
        "identity_contract",
        "test_extension_schema_version",
        "test_extension_digest",
    )
    result: dict[str, Any] = {
        "metadata": {
            "changed": [
                field
                for field in metadata_fields
                if old_payload.get(field) != new_payload.get(field)
            ],
            "old": {
                field: copy.deepcopy(old_payload.get(field))
                for field in metadata_fields
            },
            "new": {
                field: copy.deepcopy(new_payload.get(field))
                for field in metadata_fields
            },
        }
    }
    for registry in (
        "modalities",
        "sources",
        "families",
        "canonical_models",
        "profiles",
        "interfaces",
        "route_templates",
        "contracts",
        "legacy_aliases",
        "test_bindings",
    ):
        old_rows = old_payload.get(registry) or {}
        new_rows = new_payload.get(registry) or {}
        changed = sorted(
            key
            for key in set(old_rows).intersection(new_rows)
            if old_rows[key] != new_rows[key]
        )
        result[registry] = {
            "added": sorted(set(new_rows) - set(old_rows)),
            "removed": sorted(set(old_rows) - set(new_rows)),
            "changed": changed,
        }
    return result


__all__ = [
    "Catalog",
    "ParameterConfigResolutionError",
    "WorkflowResolutionError",
    "list_workflows",
    "resolve_workflow",
    "database_info",
    "diff_catalogs",
    "get_interface",
    "get_contract",
    "get_family",
    "get_model",
    "get_profile",
    "get_route_template",
    "get_source",
    "get_test_binding",
    "list_contracts",
    "list_families",
    "list_interfaces",
    "list_modalities",
    "list_models",
    "list_parameter_configs",
    "list_profiles",
    "list_route_templates",
    "list_sources",
    "list_test_bindings",
    "load_catalog",
    "resolve_legacy_id",
    "resolve_parameter_config",
    "resolve_request_model",
    "source_first_parameter_view",
]
