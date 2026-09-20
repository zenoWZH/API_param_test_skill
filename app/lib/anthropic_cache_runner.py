"""Bounded native transport for versioned Anthropic cache control plans."""
from pathlib import Path
import requests

from .deepseek_beta_runner import _execute, _official_client_origin_matches
from .anthropic_cache_reference import CachePlan, ENDPOINT, COUNT_ENDPOINT, TRANSPORT, API_VERSION, canonical_bytes, evaluate_cache_plan, next_cache_request
from .parameter_output_limit import enforce_parameter_test_output_limit


def validate_cache_client(client, plan, *, minimum_output_tokens=256):
    if type(plan) is not CachePlan:
        raise ValueError("Cache dispatch requires the immutable job plan, not a preview")
    interface = client.api_interfaces.get(TRANSPORT) or {}
    if (not _official_client_origin_matches(client, "anthropic_official")
            or client._transport_url(TRANSPORT) != ENDPOINT or plan.endpoint != ENDPOINT
            or plan.snapshot["execution_target"]["provider_id"] != client.provider
            or interface.get("auth") != "anthropic" or interface.get("anthropic_version", API_VERSION) != API_VERSION):
        raise ValueError("Cache dispatch requires the exact official Anthropic Messages route")
    requests_ = plan.requests
    expected = ["generation"] * 3 if plan.schema_version == 2 else ["count", "count", "generation", "generation", "generation"]
    if [request.kind for request in requests_] != expected:
        raise ValueError("Cache dispatch kinds differ from the frozen plan version")
    for request in requests_:
        if request.kind == "count":
            if request.endpoint != COUNT_ENDPOINT or "max_tokens" in request.body or "stream" in request.body:
                raise ValueError("Cache count request crossed its native input-count boundary")
            continue  # Count endpoints have no generated-output token cap.
        limited = request.body
        enforce_parameter_test_output_limit(limited, TRANSPORT, minimum=minimum_output_tokens)
        if request.endpoint != ENDPOINT or canonical_bytes(limited) != request.body_bytes:
            raise ValueError("Configured floor or endpoint changes the frozen cache generation")


def execute_cache_plan(client, plan, directory: Path, *, minimum_output_tokens=256, on_record=None, execution_plan=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A real cache report directory is required")
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        return _execute(client, plan, directory, session.request, minimum_output_tokens=minimum_output_tokens,
            on_record=on_record, execution_plan=execution_plan, validate_plan=validate_cache_client, evaluate_plan=evaluate_cache_plan,
            next_request=next_cache_request, artifact_prefix="anthropic_cache", auth_mode="anthropic")
