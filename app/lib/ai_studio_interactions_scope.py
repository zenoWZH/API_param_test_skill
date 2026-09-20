"""Exact approval #4 fixture and scope shared by its author and bounded sender."""
import re

SOURCE = "google_ai_studio"
MODEL = "gemini-3.7-flash"
FORM = "gemini_interactions"
PROFILE_ID = "text/google_ai_studio/gemini/gemini-3.7-flash"
SLUG = "gemini-interactions-stateful-bounded"
INTERFACE_ID = PROFILE_ID + "#" + SLUG
CONTRACT_ID = "gemini_3_7_flash_interactions_stateful_bounded"
PARAMETER_ID = "parameter/" + CONTRACT_ID
POLICY_ID = "interface/" + PROFILE_ID + "/" + SLUG
CASE_ID = "ai_studio_interactions_nonce_chain"
APPROVAL = "R7-STATEFUL-INTERACTIONS-LIVE"
APPROVAL_KEY = "approved_ai_studio_interactions_lifecycle_20260907"
SCOPE = "approved_four_request_ai_studio_text_state_lifecycle_live_unverified"
API_VERSION = "v1beta"
PATH = "/v1beta/interactions"
URL = "https://generativelanguage.googleapis.com" + PATH
NONCE_PREFIX = "AI_STUDIO_STATE_"
NONCE_PATTERN = r"AI_STUDIO_STATE_[0-9a-f]{32}"
NONCE_ENTROPY_BITS = 128
NONCE_TEMPLATE = "{state_nonce}"
TEST_NONCE = NONCE_PREFIX + "0123456789abcdef0123456789abcdef"
ACK = "ACK_STORED"
FIRST_INPUT_TEMPLATE = "Remember this exact reference nonce for my next turn: " + NONCE_TEMPLATE + ". Reply only " + ACK + "; do not repeat the nonce."
NEXT_INPUT = "Return only the exact reference nonce from my previous turn. Do not add any other text."
LIMITS = {"max_requests": 4, "timeout_sec": 120, "max_run_seconds": 300, "max_output_tokens": 2048,
          "max_input_chars": 512, "retention": "delete_created_resources_finally", "max_polls": 0,
          "poll_interval_sec": 1, "cleanup_timeout_sec": 30, "max_response_bytes": 2 * 1024 * 1024}
OFFICIAL_DOCS = ["https://ai.google.dev/api/interactions-api",
                 "https://ai.google.dev/static/api/interactions.openapi.json",
                 "https://ai.google.dev/gemini-api/docs/interactions-overview"]


def validate_nonce(nonce):
    if not isinstance(nonce, str) or not re.fullmatch(NONCE_PATTERN, nonce):
        raise ValueError("state nonce must contain exactly 128 bits encoded as 32 lowercase hex digits")
    return nonce


def first_input(nonce):
    return FIRST_INPUT_TEMPLATE.replace(NONCE_TEMPLATE, validate_nonce(nonce))


def body_for(previous=None, *, nonce=NONCE_TEMPLATE):
    parent_input = FIRST_INPUT_TEMPLATE if nonce == NONCE_TEMPLATE else first_input(nonce)
    body = {"model": MODEL, "input": parent_input if previous is None else NEXT_INPUT,
            "store": True, "stream": False, "generation_config": {"max_output_tokens": 2048}}
    if previous is not None:
        body.update(previous_interaction_id=previous, background=False)
    return body
