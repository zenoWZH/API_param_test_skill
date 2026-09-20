from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


CACHED_CONTENT_API_FORM = "gemini_generate_content"
INTERACTIONS_API_FORM = "gemini_interactions"

_CACHE_NAME_RE = re.compile(r"^cachedContents/[A-Za-z0-9._~-]+$")
_INTERACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:~-]+$")
_KNOWN_INTERACTION_STATUSES = frozenset(
    {
        "queued",
        "in_progress",
        "completed",
        "requires_action",
        "failed",
        "cancelled",
        "incomplete",
        "budget_exceeded",
    }
)
_TERMINAL_INTERACTION_STATUSES = frozenset(
    {
        "completed",
        "requires_action",
        "failed",
        "cancelled",
        "incomplete",
        "budget_exceeded",
    }
)


class CachedContentAdapter(Protocol):
    """Offline adapter boundary for the explicit-cache lifecycle runner."""

    offline_only: bool

    def create_cached_content(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def generate_content(
        self, model: str, request: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def delete_cached_content(self, name: str) -> None: ...


class InteractionsAdapter(Protocol):
    """Offline adapter boundary for the stateful Interactions runner."""

    offline_only: bool

    def create_interaction(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def get_interaction(self, interaction_id: str) -> Mapping[str, Any]: ...

    def delete_interaction(self, interaction_id: str) -> None: ...


@dataclass(frozen=True)
class OfflineModelTarget:
    """Exact MPDB Profile/Interface identity used by an offline runner.

    ``interface_enabled`` and ``interface_executable`` are deliberately
    observational.  The runners never promote or mutate them and always require
    an adapter whose ``offline_only`` marker is exactly ``True``.
    """

    source_id: str
    modality: str
    family_id: str
    model_slug: str
    request_model_id: str
    profile_id: str
    interface_id: str
    api_form: str
    profile_state: str
    interface_enabled: bool
    interface_executable: bool
    catalog_version: str = ""
    catalog_digest: str = ""

    @classmethod
    def from_catalog(
        cls,
        catalog: Any,
        *,
        interface_id: str,
        request_model_id: str,
        expected_api_form: str,
    ) -> "OfflineModelTarget":
        """Resolve one exact Interface without applying runtime fallbacks."""

        interface = copy.deepcopy(catalog.get_interface(interface_id))
        profile_id = str(interface.get("profile_id") or "")
        if not profile_id:
            raise ValueError("MPDB Interface is missing profile_id")
        profile = copy.deepcopy(catalog.get_profile(profile_id))

        actual_interface_id = str(interface.get("interface_id") or interface_id)
        if actual_interface_id != interface_id:
            raise ValueError("MPDB Interface identity does not match the requested ID")
        if str(interface.get("profile_id") or "") != str(
            profile.get("profile_id") or ""
        ):
            raise ValueError("MPDB Interface/Profile linkage mismatch")
        for identity_field in ("source_id", "modality", "family_id", "model_slug"):
            if str(interface.get(identity_field) or "") != str(
                profile.get(identity_field) or ""
            ):
                raise ValueError(
                    "MPDB Interface/Profile identity mismatch for "
                    f"{identity_field}"
                )
        api_form = str(interface.get("api_form") or "")
        if api_form != expected_api_form:
            raise ValueError(
                "MPDB Interface api_form mismatch: "
                f"expected={expected_api_form!r}, actual={api_form!r}"
            )

        requested_model_id = str(request_model_id)
        if not requested_model_id or requested_model_id != requested_model_id.strip():
            raise ValueError("request_model_id must be an exact non-empty wire ID")
        interface_model_ids = {
            str(value).strip()
            for value in (interface.get("request_model_ids") or [])
            if value is not None and str(value).strip()
        }
        profile_model_ids = {
            str(value).strip()
            for value in (profile.get("request_model_ids") or [])
            if value is not None and str(value).strip()
        }
        candidate_model_ids = (
            interface_model_ids
            or profile_model_ids
            or {str(profile.get("model_slug") or "").strip()}
        )
        candidate_model_ids.discard("")
        if requested_model_id not in candidate_model_ids:
            raise ValueError(
                "request_model_id is not declared by the exact MPDB "
                "Profile/Interface"
            )

        target = cls(
            source_id=str(profile.get("source_id") or ""),
            modality=str(profile.get("modality") or ""),
            family_id=str(profile.get("family_id") or ""),
            model_slug=str(profile.get("model_slug") or ""),
            request_model_id=requested_model_id,
            profile_id=str(profile.get("profile_id") or ""),
            interface_id=actual_interface_id,
            api_form=api_form,
            profile_state=str(profile.get("profile_state") or "executable"),
            interface_enabled=bool(interface.get("enabled", True)),
            interface_executable=bool(interface.get("executable", True)),
            catalog_version=str(getattr(catalog, "version", "") or ""),
            catalog_digest=str(getattr(catalog, "digest", "") or ""),
        )
        target.validate(expected_api_form=expected_api_form)
        return target

    def validate(self, *, expected_api_form: str) -> None:
        if self.api_form != expected_api_form:
            raise ValueError(
                f"offline runner requires api_form={expected_api_form!r}"
            )
        fields = {
            "source_id": self.source_id,
            "modality": self.modality,
            "family_id": self.family_id,
            "model_slug": self.model_slug,
            "request_model_id": self.request_model_id,
            "profile_id": self.profile_id,
            "interface_id": self.interface_id,
        }
        missing = sorted(name for name, value in fields.items() if not str(value))
        if missing:
            raise ValueError(
                "offline runner target is missing exact MPDB identity fields: "
                + ", ".join(missing)
            )
        expected_profile_id = "/".join(
            (self.modality, self.source_id, self.family_id, self.model_slug)
        )
        if self.profile_id != expected_profile_id:
            raise ValueError("offline runner target has a non-canonical Profile ID")
        if self.interface_id.rsplit("#", 1)[0] != self.profile_id:
            raise ValueError("offline runner Interface is not owned by its Profile")

    def ledger_identity(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "profile_id": self.profile_id,
            "interface_id": self.interface_id,
            "api_form": self.api_form,
            "interface_enabled_observed": self.interface_enabled,
            "interface_executable_observed": self.interface_executable,
            "profile_state_observed": self.profile_state,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "execution_mode": "offline_only",
        }


@dataclass(frozen=True)
class LifecycleLedgerEvent:
    sequence: int
    runner: str
    action: str
    state: str
    resource_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "runner": self.runner,
            "action": self.action,
            "state": self.state,
            "resource_id": self.resource_id,
            "details": copy.deepcopy(self.details),
        }


class LifecycleLedger:
    """Deterministic, payload-free audit ledger for offline lifecycle tests."""

    def __init__(self) -> None:
        self._events: list[LifecycleLedgerEvent] = []

    @property
    def events(self) -> tuple[LifecycleLedgerEvent, ...]:
        return tuple(self._events)

    def record(
        self,
        *,
        runner: str,
        action: str,
        state: str,
        resource_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> LifecycleLedgerEvent:
        event = LifecycleLedgerEvent(
            sequence=len(self._events) + 1,
            runner=runner,
            action=action,
            state=state,
            resource_id=resource_id,
            details=copy.deepcopy(dict(details or {})),
        )
        self._events.append(event)
        return event

    def snapshot(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._events]


class OfflineLifecycleError(RuntimeError):
    """A primary lifecycle error accompanied by one or more cleanup errors."""

    def __init__(
        self,
        *,
        runner: str,
        stage: str,
        primary_error: Exception | None,
        cleanup_errors: tuple[Exception, ...],
    ) -> None:
        self.runner = runner
        self.stage = stage
        self.primary_error = primary_error
        self.cleanup_errors = cleanup_errors
        reason = "cleanup failed" if primary_error is None else "run and cleanup failed"
        super().__init__(f"{runner} {reason} at stage={stage}")


class InteractionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedContentLifecycleResult:
    target: OfflineModelTarget
    cache_name: str
    create_response: dict[str, Any]
    use_response: dict[str, Any]
    deleted: bool
    ledger: tuple[LifecycleLedgerEvent, ...]


class CachedContentLifecycleRunner:
    """Run create/use/delete against an explicitly offline cache adapter."""

    runner_name = "cached_content_lifecycle"

    def __init__(
        self,
        *,
        target: OfflineModelTarget,
        adapter: CachedContentAdapter,
        ledger: LifecycleLedger | None = None,
    ) -> None:
        target.validate(expected_api_form=CACHED_CONTENT_API_FORM)
        _require_offline_adapter(adapter)
        self.target = target
        self.adapter = adapter
        self.ledger = ledger or LifecycleLedger()

    def run(
        self,
        *,
        create_request: Mapping[str, Any],
        use_request: Mapping[str, Any],
    ) -> CachedContentLifecycleResult:
        create_payload = copy.deepcopy(dict(create_request))
        use_payload = copy.deepcopy(dict(use_request))
        _validate_cached_content_requests(self.target, create_payload, use_payload)

        self.ledger.record(
            runner=self.runner_name,
            action="validate_target",
            state="ready",
            details=self.target.ledger_identity(),
        )

        stage = "create"
        cache_name: str | None = None
        created: dict[str, Any] = {}
        generated: dict[str, Any] = {}
        deleted = False
        primary_error: Exception | None = None
        cleanup_errors: list[Exception] = []
        try:
            created = _mapping_copy(
                self.adapter.create_cached_content(create_payload),
                operation="create_cached_content",
            )
            # Capture a syntactically safe resource name before validating the
            # rest of the response so later validation failures still enter the
            # same finally cleanup path.
            cache_name = _cache_name(created)
            _validate_cached_content_resource(created, self.target)
            self.ledger.record(
                runner=self.runner_name,
                action="create",
                state="created",
                resource_id=cache_name,
            )

            stage = "use"
            use_payload["cachedContent"] = cache_name
            generated = _mapping_copy(
                self.adapter.generate_content(
                    self.target.request_model_id,
                    use_payload,
                ),
                operation="generate_content",
            )
            _validate_cached_content_use_response(generated, self.target)
            self.ledger.record(
                runner=self.runner_name,
                action="use",
                state="used",
                resource_id=cache_name,
                details=_cached_token_details(generated),
            )
        except Exception as exc:  # cleanup is intentionally centralized below
            primary_error = exc
            self.ledger.record(
                runner=self.runner_name,
                action="error",
                state="failed",
                resource_id=cache_name,
                details={"stage": stage, "error_type": type(exc).__name__},
            )
        finally:
            if cache_name is not None:
                try:
                    self.adapter.delete_cached_content(cache_name)
                    deleted = True
                    self.ledger.record(
                        runner=self.runner_name,
                        action="delete",
                        state="deleted",
                        resource_id=cache_name,
                    )
                except Exception as exc:
                    cleanup_errors.append(exc)
                    self.ledger.record(
                        runner=self.runner_name,
                        action="delete",
                        state="delete_failed",
                        resource_id=cache_name,
                        details={"error_type": type(exc).__name__},
                    )

        if cleanup_errors:
            raise OfflineLifecycleError(
                runner=self.runner_name,
                stage=stage,
                primary_error=primary_error,
                cleanup_errors=tuple(cleanup_errors),
            ) from primary_error
        if primary_error is not None:
            raise primary_error
        assert cache_name is not None
        return CachedContentLifecycleResult(
            target=self.target,
            cache_name=cache_name,
            create_response=created,
            use_response=generated,
            deleted=deleted,
            ledger=self.ledger.events,
        )


class InMemoryCachedContentAdapter:
    """Deterministic cache mock; it contains no HTTP or SDK client."""

    offline_only = True

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[dict[str, Any]] = []
        self._resources: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    @property
    def active_resource_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))

    def create_cached_content(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        payload = copy.deepcopy(dict(request))
        self.calls.append({"operation": "create", "request": payload})
        self._fail("create")
        name = f"cachedContents/offline-cache-{self._next_id}"
        self._next_id += 1
        resource = {
            "name": name,
            "model": payload["model"],
            "usageMetadata": {"totalTokenCount": 32},
        }
        self._resources[name] = copy.deepcopy(resource)
        return resource

    def generate_content(
        self, model: str, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        payload = copy.deepcopy(dict(request))
        self.calls.append(
            {"operation": "use", "model": str(model), "request": payload}
        )
        self._fail("use")
        cache_name = str(payload.get("cachedContent") or "")
        if cache_name not in self._resources:
            raise KeyError("cached content resource is not active")
        return {
            "modelVersion": str(model),
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "offline cached response"}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 40,
                "cachedContentTokenCount": 32,
                "candidatesTokenCount": 4,
                "totalTokenCount": 44,
            },
        }

    def delete_cached_content(self, name: str) -> None:
        self.calls.append({"operation": "delete", "name": str(name)})
        self._fail("delete")
        if name not in self._resources:
            raise KeyError("cached content resource is not active")
        del self._resources[name]

    def _fail(self, operation: str) -> None:
        if self.fail_on == operation:
            raise RuntimeError(f"offline cache mock injected {operation} failure")


@dataclass(frozen=True)
class StatefulInteractionsResult:
    target: OfflineModelTarget
    root_interaction: dict[str, Any]
    child_interaction: dict[str, Any]
    poll_count: int
    deleted_interaction_ids: tuple[str, ...]
    ledger: tuple[LifecycleLedgerEvent, ...]


class StatefulInteractionsRunner:
    """Run store/previous/background/poll/delete using an offline adapter."""

    runner_name = "stateful_interactions"

    def __init__(
        self,
        *,
        target: OfflineModelTarget,
        adapter: InteractionsAdapter,
        ledger: LifecycleLedger | None = None,
    ) -> None:
        target.validate(expected_api_form=INTERACTIONS_API_FORM)
        _require_offline_adapter(adapter)
        self.target = target
        self.adapter = adapter
        self.ledger = ledger or LifecycleLedger()

    def run(
        self,
        *,
        initial_input: str,
        followup_input: str,
        max_polls: int = 8,
    ) -> StatefulInteractionsResult:
        if not str(initial_input).strip() or not str(followup_input).strip():
            raise ValueError("Interactions lifecycle inputs must be non-empty")
        if (
            isinstance(max_polls, bool)
            or not isinstance(max_polls, int)
            or max_polls <= 0
        ):
            raise ValueError("max_polls must be a positive integer")

        self.ledger.record(
            runner=self.runner_name,
            action="validate_target",
            state="ready",
            details=self.target.ledger_identity(),
        )

        stage = "store"
        root: dict[str, Any] = {}
        child: dict[str, Any] = {}
        root_id: str | None = None
        child_id: str | None = None
        cleanup_ids: list[str] = []
        deleted_ids: list[str] = []
        poll_count = 0
        primary_error: Exception | None = None
        cleanup_errors: list[Exception] = []
        try:
            root_request = {
                "model": self.target.request_model_id,
                "input": str(initial_input),
                "store": True,
                "background": False,
            }
            root = _mapping_copy(
                self.adapter.create_interaction(root_request),
                operation="create_interaction",
            )
            root_id = _interaction_id(root)
            cleanup_ids.append(root_id)
            observed_root_id, root_status = _interaction_identity(
                root,
                expected_model=self.target.request_model_id,
            )
            if observed_root_id != root_id:  # defensive: both helpers are strict
                raise InteractionStateError("root interaction identity changed")
            if root_status != "completed":
                raise InteractionStateError(
                    "stored root interaction must complete before chaining"
                )
            self.ledger.record(
                runner=self.runner_name,
                action="store",
                state="stored",
                resource_id=root_id,
                details={"status": root_status},
            )

            stage = "previous"
            child_request = {
                "model": self.target.request_model_id,
                "input": str(followup_input),
                "store": True,
                "background": True,
                "previous_interaction_id": root_id,
            }
            child = _mapping_copy(
                self.adapter.create_interaction(child_request),
                operation="create_interaction",
            )
            child_id = _interaction_id(child)
            cleanup_ids.append(child_id)
            observed_child_id, child_status = _interaction_identity(
                child,
                expected_model=self.target.request_model_id,
            )
            if observed_child_id != child_id:  # defensive: both helpers are strict
                raise InteractionStateError("child interaction identity changed")
            if child_id == root_id:
                raise InteractionStateError(
                    "follow-up interaction reused the root interaction ID"
                )
            self.ledger.record(
                runner=self.runner_name,
                action="previous",
                state="linked",
                resource_id=child_id,
                details={"previous_interaction_id": root_id},
            )
            echoed_previous = child.get("previous_interaction_id")
            if echoed_previous is not None and str(echoed_previous) != root_id:
                raise InteractionStateError(
                    "follow-up interaction echoed a different previous ID"
                )
            self.ledger.record(
                runner=self.runner_name,
                action="background",
                state="submitted",
                resource_id=child_id,
                details={"status": child_status},
            )

            stage = "poll"
            while child_status not in _TERMINAL_INTERACTION_STATUSES:
                if poll_count >= max_polls:
                    raise InteractionStateError(
                        "background interaction exceeded the offline poll limit"
                    )
                child = _mapping_copy(
                    self.adapter.get_interaction(child_id),
                    operation="get_interaction",
                )
                observed_id, child_status = _interaction_identity(
                    child,
                    expected_model=self.target.request_model_id,
                )
                if observed_id != child_id:
                    raise InteractionStateError(
                        "poll returned a different interaction ID"
                    )
                poll_count += 1
                self.ledger.record(
                    runner=self.runner_name,
                    action="poll",
                    state="polled",
                    resource_id=child_id,
                    details={"poll": poll_count, "status": child_status},
                )

            if child_status != "completed":
                raise InteractionStateError(
                    "background interaction ended in non-success state "
                    f"{child_status!r}"
                )
            self.ledger.record(
                runner=self.runner_name,
                action="complete",
                state="completed",
                resource_id=child_id,
                details={"poll_count": poll_count},
            )
        except Exception as exc:  # cleanup is intentionally centralized below
            primary_error = exc
            self.ledger.record(
                runner=self.runner_name,
                action="error",
                state="failed",
                resource_id=child_id or root_id,
                details={"stage": stage, "error_type": type(exc).__name__},
            )
        finally:
            for interaction_id in reversed(cleanup_ids):
                try:
                    self.adapter.delete_interaction(interaction_id)
                    deleted_ids.append(interaction_id)
                    self.ledger.record(
                        runner=self.runner_name,
                        action="delete",
                        state="deleted",
                        resource_id=interaction_id,
                    )
                except Exception as exc:
                    cleanup_errors.append(exc)
                    self.ledger.record(
                        runner=self.runner_name,
                        action="delete",
                        state="delete_failed",
                        resource_id=interaction_id,
                        details={"error_type": type(exc).__name__},
                    )

        if cleanup_errors:
            raise OfflineLifecycleError(
                runner=self.runner_name,
                stage=stage,
                primary_error=primary_error,
                cleanup_errors=tuple(cleanup_errors),
            ) from primary_error
        if primary_error is not None:
            raise primary_error
        return StatefulInteractionsResult(
            target=self.target,
            root_interaction=root,
            child_interaction=child,
            poll_count=poll_count,
            deleted_interaction_ids=tuple(deleted_ids),
            ledger=self.ledger.events,
        )


class InMemoryInteractionsAdapter:
    """Deterministic Interactions state-machine mock with no network client."""

    offline_only = True

    def __init__(
        self,
        *,
        background_statuses: tuple[str, ...] = (
            "queued",
            "in_progress",
            "completed",
        ),
        fail_on: str | None = None,
    ) -> None:
        if not background_statuses:
            raise ValueError("background_statuses must not be empty")
        unknown = sorted(set(background_statuses) - _KNOWN_INTERACTION_STATUSES)
        if unknown:
            raise ValueError(
                "unknown mock interaction statuses: " + ", ".join(unknown)
            )
        self.background_statuses = tuple(background_statuses)
        self.fail_on = fail_on
        self.calls: list[dict[str, Any]] = []
        self._resources: dict[str, dict[str, Any]] = {}
        self._remaining_statuses: dict[str, list[str]] = {}
        self._next_id = 1

    @property
    def active_interaction_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))

    def create_interaction(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        payload = copy.deepcopy(dict(request))
        operation = "create_background" if payload.get("background") is True else "create"
        self.calls.append({"operation": operation, "request": payload})
        self._fail(operation)

        previous_id = payload.get("previous_interaction_id")
        if previous_id is not None and str(previous_id) not in self._resources:
            raise KeyError("previous interaction is not active")
        interaction_id = f"offline-interaction-{self._next_id}"
        self._next_id += 1
        statuses = (
            self.background_statuses
            if payload.get("background") is True
            else ("completed",)
        )
        resource = {
            "id": interaction_id,
            "object": "interaction",
            "model": str(payload.get("model") or ""),
            "status": statuses[0],
            "steps": [],
            "usage": {"total_cached_tokens": 0, "total_tokens": 1},
        }
        if previous_id is not None:
            resource["previous_interaction_id"] = str(previous_id)
        if payload.get("store") is True:
            self._resources[interaction_id] = copy.deepcopy(resource)
            self._remaining_statuses[interaction_id] = list(statuses[1:])
        return resource

    def get_interaction(self, interaction_id: str) -> Mapping[str, Any]:
        self.calls.append({"operation": "get", "id": str(interaction_id)})
        self._fail("get")
        if interaction_id not in self._resources:
            raise KeyError("interaction is not active")
        remaining = self._remaining_statuses[interaction_id]
        if remaining:
            self._resources[interaction_id]["status"] = remaining.pop(0)
        return copy.deepcopy(self._resources[interaction_id])

    def delete_interaction(self, interaction_id: str) -> None:
        self.calls.append({"operation": "delete", "id": str(interaction_id)})
        self._fail("delete")
        if interaction_id not in self._resources:
            raise KeyError("interaction is not active")
        del self._resources[interaction_id]
        self._remaining_statuses.pop(interaction_id, None)

    def _fail(self, operation: str) -> None:
        if self.fail_on == operation:
            raise RuntimeError(
                f"offline interactions mock injected {operation} failure"
            )


def _require_offline_adapter(adapter: Any) -> None:
    if getattr(adapter, "offline_only", None) is not True:
        raise ValueError(
            "stateful lifecycle runners accept only adapters marked offline_only=True"
        )


def _mapping_copy(value: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{operation} must return a mapping")
    return copy.deepcopy(dict(value))


def _normalize_model_name(value: Any) -> str:
    name = str(value or "").strip()
    return name.removeprefix("models/")


def _validate_cached_content_requests(
    target: OfflineModelTarget,
    create_request: Mapping[str, Any],
    use_request: Mapping[str, Any],
) -> None:
    if _normalize_model_name(create_request.get("model")) != _normalize_model_name(
        target.request_model_id
    ):
        raise ValueError("cache create model does not match the exact MPDB target")
    if not isinstance(create_request.get("contents"), list) or not create_request.get(
        "contents"
    ):
        raise ValueError("cache create request requires non-empty contents")
    if not isinstance(use_request.get("contents"), list) or not use_request.get(
        "contents"
    ):
        raise ValueError("cache use request requires non-empty contents")
    if "cachedContent" in use_request or "cached_content" in use_request:
        raise ValueError(
            "cache resource identity is runner-owned and must not be pre-populated"
        )


def _cache_name(response: Mapping[str, Any]) -> str:
    name = str(response.get("name") or "")
    if not _CACHE_NAME_RE.fullmatch(name):
        raise ValueError("cache create response has an invalid resource name")
    return name


def _validate_cached_content_resource(
    response: Mapping[str, Any], target: OfflineModelTarget
) -> None:
    model = response.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("cache create response is missing model identity")
    if _normalize_model_name(model) != _normalize_model_name(
        target.request_model_id
    ):
        raise ValueError("cache create response model does not match the MPDB target")


def _validate_cached_content_use_response(
    response: Mapping[str, Any], target: OfflineModelTarget
) -> None:
    model_version = response.get("modelVersion")
    if not isinstance(model_version, str) or not model_version.strip():
        raise ValueError("cache use response is missing modelVersion identity")
    if model_version != target.request_model_id:
        raise ValueError("cache use response modelVersion does not match the MPDB target")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("cache use response requires non-empty candidates")
    if any(not isinstance(candidate, Mapping) or not candidate for candidate in candidates):
        raise ValueError("cache use response candidates must be non-empty objects")


def _cached_token_details(response: Mapping[str, Any]) -> dict[str, Any]:
    usage = response.get("usageMetadata")
    if not isinstance(usage, Mapping):
        return {}
    value = usage.get("cachedContentTokenCount")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return {}
    return {"cached_content_token_count": int(value)}


def _interaction_identity(
    response: Mapping[str, Any], *, expected_model: str
) -> tuple[str, str]:
    interaction_id = _interaction_id(response)
    if response.get("object") not in {None, "interaction"}:
        raise InteractionStateError("interaction response has an unexpected object type")
    model = str(response.get("model") or "")
    if not model:
        raise InteractionStateError("interaction response is missing model identity")
    if model != expected_model:
        raise InteractionStateError(
            "interaction response model does not match the exact MPDB target"
        )
    status = str(response.get("status") or "")
    if status not in _KNOWN_INTERACTION_STATUSES:
        raise InteractionStateError(
            f"interaction response has an unknown status {status!r}"
        )
    return interaction_id, status


def _interaction_id(response: Mapping[str, Any]) -> str:
    interaction_id = str(response.get("id") or "")
    if not _INTERACTION_ID_RE.fullmatch(interaction_id):
        raise InteractionStateError("interaction response has an invalid ID")
    return interaction_id
