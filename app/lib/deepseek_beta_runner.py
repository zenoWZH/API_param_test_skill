"""Bounded transport for the MPDB-owned Pro 0813 five-case suite."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import signal
import threading
import time

import requests

from .deepseek_beta_reference import (
    ENDPOINT, TRANSPORT, MAX_RESPONSE_BYTES, canonical_bytes,
    evaluate_deepseek_beta_reference, next_deepseek_beta_request,
)
from .parameter_output_limit import enforce_parameter_test_output_limit

TIMEOUT_SECONDS = 150


@contextmanager
def _deadline():
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("The fixed beta transport requires its CLI main thread")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("An existing process deadline prevents beta dispatch")
    old = signal.getsignal(signal.SIGALRM)
    def timeout(_signum, _frame):
        raise TimeoutError("Beta response deadline exceeded")
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _write_new(directory: Path, name: str, value):
    # The attempt ledger is never overwritten or resumed automatically.
    created = False
    try:
        with (directory / name).open("xb") as stream:
            created = True
            stream.write(canonical_bytes(value) + b"\n")
    finally:
        if created and name.startswith("anthropic_cache_"):
            from .approved_report_retention import register_cache_immutable
            register_cache_immutable(directory, name)


def _official_client_origin_matches(client, provider):
    origins = {"deepseek_official": "https://api.deepseek.com", "anthropic_official": "https://api.anthropic.com"}
    origin = origins.get(provider)
    credential = getattr(client, "_credential", None)
    return bool(origin and client.provider == provider and client.base_url in (origin, origin + "/v1")
        and getattr(credential, "provider", None) == provider
        and getattr(credential, "allowed_origins", None) == frozenset({origin}))


def validate_beta_client(client, plan, *, minimum_output_tokens=256):
    if (not _official_client_origin_matches(client, "deepseek_official")
            or client._transport_url(TRANSPORT) != ENDPOINT or plan.endpoint != ENDPOINT
            or plan.snapshot["execution_target"]["provider_id"] != client.provider
            or (client.api_interfaces.get(TRANSPORT) or {}).get("auth") != "bearer"):
        raise ValueError("Fixed beta suite requires the exact official provider and beta route")
    for item in plan.requests:
        limited = item.body
        enforce_parameter_test_output_limit(limited, TRANSPORT, minimum=minimum_output_tokens)
        if canonical_bytes(limited) != item.body_bytes:
            raise ValueError("Configured output floor changes the fixed beta request")


def _request_boundary(plan, item, artifact_prefix):
    """Keep the two count/generation paths exclusive to the cache plan type."""
    if artifact_prefix == "anthropic_cache":
        from .anthropic_cache_reference import CachePlan, COUNT_ENDPOINT, ENDPOINT as CACHE_ENDPOINT
        if type(plan) is not CachePlan or plan.endpoint != CACHE_ENDPOINT:
            raise ValueError("Cache transport requires its immutable versioned plan")
        kind, endpoint = item.kind, item.endpoint
        if kind not in ("count", "generation") or endpoint != (COUNT_ENDPOINT if kind == "count" else CACHE_ENDPOINT):
            raise ValueError("Cache request kind and official endpoint differ")
        return endpoint, kind
    if getattr(item, "kind", "generation") != "generation" or getattr(item, "endpoint", plan.endpoint) != plan.endpoint:
        raise ValueError("Existing fixed suites cannot change request kind or endpoint")
    return plan.endpoint, "generation"


def _execute(client, plan, directory, request, *, minimum_output_tokens, on_record,
             validate_plan=validate_beta_client, evaluate_plan=evaluate_deepseek_beta_reference,
             next_request=next_deepseek_beta_request, artifact_prefix="beta", auth_mode="bearer",
             execution_plan=None):
    from .test_runner.adapters.fixed_parameter import _kind, execute_fixed_parameter_plan
    if artifact_prefix != _kind(plan):
        raise ValueError("Fixed suite report namespace differs from its immutable source")
    if auth_mode != ("anthropic" if artifact_prefix.startswith("anthropic_") else "bearer"):
        raise ValueError("Fixed suite authentication mode differs from its source")
    records, observed = execute_fixed_parameter_plan(client, plan, directory, request,
        validate_client=validate_plan, minimum_output_tokens=minimum_output_tokens, on_record=on_record,
        artifact_writer=lambda name, value: _write_new(directory, name, value), execution_plan=execution_plan)
    if artifact_prefix == "anthropic_cache":
        from .approved_report_retention import register_cache_workflow
        register_cache_workflow(directory, observed["workflow_result"])
    return records, observed


def execute_deepseek_beta_plan(client, plan, directory: Path, *, minimum_output_tokens=256, on_record=None, execution_plan=None):
    """Dispatch each fixed body once; a retained start marker prevents reruns."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A real report directory is required")
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        return _execute(client, plan, directory, session.request,
                        minimum_output_tokens=minimum_output_tokens, on_record=on_record, execution_plan=execution_plan)
