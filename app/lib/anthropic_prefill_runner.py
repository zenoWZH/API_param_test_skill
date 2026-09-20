"""Dispatch one exact three-request native Anthropic prefill suite."""
from pathlib import Path
import requests

from .deepseek_beta_runner import _execute, _official_client_origin_matches
from .anthropic_prefill_reference import ENDPOINT, TRANSPORT, PrefillPlan, canonical_bytes, evaluate_prefill_plan, next_prefill_request
from .parameter_output_limit import enforce_parameter_test_output_limit


def validate_prefill_client(client, plan, *, minimum_output_tokens=256):
    if type(plan) is not PrefillPlan:
        raise ValueError("Prefill dispatch requires its immutable source-authored plan")
    if (not _official_client_origin_matches(client, "anthropic_official")
            or client._transport_url(TRANSPORT) != ENDPOINT or plan.endpoint != ENDPOINT
            or plan.snapshot["execution_target"]["provider_id"] != client.provider
            or (client.api_interfaces.get(TRANSPORT) or {}).get("auth") != "anthropic"):
        raise ValueError("Fixed prefill requires the exact official Anthropic Messages route")
    for request in plan.requests:
        limited = request.body
        enforce_parameter_test_output_limit(limited, TRANSPORT, minimum=minimum_output_tokens)
        if canonical_bytes(limited) != request.body_bytes:
            raise ValueError("Configured output floor changes the fixed prefill request")


def execute_prefill_plan(client, plan, directory: Path, *, minimum_output_tokens=256, on_record=None, execution_plan=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A real report directory is required")
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        return _execute(client, plan, directory, session.request,
            minimum_output_tokens=minimum_output_tokens, on_record=on_record, execution_plan=execution_plan,
            validate_plan=validate_prefill_client, evaluate_plan=evaluate_prefill_plan,
            next_request=next_prefill_request, artifact_prefix="anthropic_prefill", auth_mode="anthropic")
