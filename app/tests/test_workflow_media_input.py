"""Offline media workflows preserve source, wire, semantics and proof limits."""
import copy
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml

from lib.credential_security import ProviderCredential
from lib.media_input_fixtures import remote_video_fixture, video_fixture
from lib.test_runner import IntegrityError, compile_plan, execute_plan
from lib.test_runner.adapters import media_input
from lib.test_runner.service import _workflow_candidates

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("Unexpected real network"))


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def binding(source="openai", provider="openai_official", model="gpt-4o", form="openai_chat_completions"):
    family = {"google_ai_studio": "gemini", "zhipu": "glm"}.get(source, "gpt")
    profile = f"text/{source}/{family}/{model}"
    slug = "zai-coding-chat" if source == "zhipu" else "offline"
    contract = "zai_coding_" + model.replace("-", "_").replace(".", "_") + "_chat" if source == "zhipu" else "openai_chat_base"
    return {"source_id": source, "profile_id": profile, "interface_id": profile + "#" + slug,
            "contract_id": contract, "test_binding_id": "test_workflow/media-input/offline",
            "execution_target": {"provider_id": provider, "request_model_id": model, "api_form": form,
                                 "transport_adapter_id": media_input.TRANSPORTS[form]},
            "workflow_id": f"media-input/{source}/{model}/{form}"}


def receipt(text="red", *, model="gpt-4o", usage=None):
    return {"http_status": 200, "response_complete": True, "response": {
        "id": "chatcmpl-offline", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 500, "completion_tokens": 2, "total_tokens": 502}}}


def test_successful_recognition_is_not_full_media_token_proof(config, tmp_path):
    plan, registry = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_png"])
    sent = []
    def send(request, **kwargs):
        sent.append(request)
        return receipt()
    report = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    steps = report["runs"][0]["steps"]
    assert steps["image_png.observe"]["record"]["verdict"]["semantic_pass"] is True
    assert steps["image_png"]["status"] == "inconclusive"
    assert steps["image_png"]["verdict"]["overall_pass"] is False
    assert steps["image_png.observe"]["record"]["token_audit"]["schema_version"] == 4
    assert len(sent) == report["runs"][0]["business_request_count"] == 1
    assert sent[0]["body"]["max_tokens"] >= 256
    assert not report["status"] == "passed"


@pytest.mark.parametrize("response", [receipt("blue"), receipt(model="other-model"),
    receipt(usage={"prompt_tokens": 500, "completion_tokens": 2, "total_tokens": 10})])
def test_http_200_cannot_hide_semantic_identity_or_usage_failure(config, tmp_path, response):
    plan, registry = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_png"])
    result = execute_plan(plan, registry, lambda *a, **k: response, evidence_dir=tmp_path)
    step = result["runs"][0]["steps"]["image_png.observe"]
    assert step["status"] == "failed" and step["record"]["verdict"]["failures"]


def test_invalid_media_rejection_requires_real_accepted_baseline(config, tmp_path):
    plan, registry = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_reject_invalid_base64"])
    sent = []
    def send(request, **kwargs):
        sent.append(request)
        if "not-valid-base64" in json.dumps(request):
            return {"http_status": 400, "response_complete": True,
                    "response": {"error": {"type": "invalid_request_error", "message": "image_url base64 is invalid"}}}
        return receipt()
    result = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    assert len(sent) == plan["limits"]["max_requests"] == 2
    steps = result["runs"][0]["steps"]
    assert steps["image_png.observe"]["status"] == "passed"
    assert steps["image_reject_invalid_base64"]["verdict"]["rejection_pass"] is True
    assert steps["image_reject_invalid_base64"]["status"] == "inconclusive"
    assert steps["image_reject_invalid_base64"]["accepted"] is False
    assert result["status"] != "passed"


def test_wrong_baseline_blocks_negative_wire_request(config, tmp_path):
    plan, registry = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_reject_invalid_base64"])
    sent = []
    def send(request, **kwargs):
        sent.append(request)
        return receipt("blue")
    result = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    assert len(sent) == 1
    assert result["runs"][0]["steps"]["image_reject_invalid_base64"]["status"] == "blocked"


@pytest.mark.parametrize("mutation", ["body", "model", "path", "expected", "cap"])
def test_rehashed_tampering_fails_rebuild_before_key_access(config, mutation):
    plan, registry = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_png"])
    definition = copy.deepcopy(plan["definition"])
    case = definition["steps"][0]["inputs"]["case"]
    if mutation == "body":
        case["body"]["messages"][0]["content"][-1]["text"] = "The answer is red"
    elif mutation == "model":
        case["body"]["model"] = "gpt-4o-mini"
    elif mutation == "path":
        definition["target"]["path"] = "/responses"
    elif mutation == "expected":
        case["expected_text"] = "blue"
    else:
        case["body"]["max_tokens"] = 255
    changed = compile_plan(definition, registry, selected_cases=["image_png"])
    with pytest.raises(IntegrityError, match="exact current source"):
        media_input.registry_for_media_input_plan(config, changed)


def test_source_drift_rejects_the_prepared_plan(config, monkeypatch):
    plan, _ = media_input.prepare_media_input_plan(config, binding(), selected_cases=["image_png"])
    monkeypatch.setattr(media_input.matrix, "source_digest", lambda: "a" * 64)
    with pytest.raises(IntegrityError):
        media_input.registry_for_media_input_plan(config, plan)


def test_gateway_origin_cannot_reuse_an_official_source_binding(config):
    config["providers"]["openai_official"]["base_url"] = "https://gateway.example/v1"
    with pytest.raises(ValueError, match="official HTTPS"):
        media_input.build_definition(config, binding())


def test_media_is_opt_in_and_existing_workflow_defaults_remain(config):
    media = {"workflow_id": "media-input/openai/gpt-4o/openai_chat_completions", "test_binding_id": "media",
             "workflow": {"factory_id": "media_input"}, "execution_target": {"api_form": "openai_chat_completions"}}
    existing = {"workflow_id": "existing", "test_binding_id": "existing", "workflow": {"factory_id": "fable_research"},
                "execution_target": {"api_form": "openai_chat_completions"}}
    class Catalog:
        def list_workflows(self, **kwargs): return [media, existing]
    payload = {"provider": "openai_official", "model": "gpt-4o"}
    assert _workflow_candidates(config, payload, Catalog())[2] == [existing]
    assert _workflow_candidates(config, {**payload, "workflow_binding_id": "media"}, Catalog())[2] == [media]


def test_gemini_native_dispatch_uses_google_auth_and_exact_model_path(config, tmp_path):
    config["providers"]["gemini"]["api_interfaces"]["token_count"]["enabled"] = False
    selected = binding("google_ai_studio", "gemini", "gemini-2.5-flash", "gemini_generate_content")
    plan, registry = media_input.prepare_media_input_plan(config, selected, selected_cases=["image_png"])
    secret, calls, key_reads = "media-offline-canary", [], []
    payload = {"modelVersion": "gemini-2.5-flash", "candidates": [{"finishReason": "STOP",
               "content": {"role": "model", "parts": [{"text": "red"}]}}],
               "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 2, "totalTokenCount": 502}}
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def iter_content(self, size): yield json.dumps({**payload, "diagnostic": secret}).encode()
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def mount(self, *args): pass
        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return Response()
    def credential(*args):
        key_reads.append(True)
        return ProviderCredential.create(provider="gemini", secret=secret,
                                         base_urls=["https://generativelanguage.googleapis.com"])
    sender = media_input.make_dispatcher(config, plan, session_factory=Session, credential_factory=credential)
    assert key_reads == []
    result = execute_plan(plan, registry, sender, evidence_dir=tmp_path)
    assert len(calls) == len(key_reads) == 1
    call = calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert call["headers"]["x-goog-api-key"] == secret and "Authorization" not in call["headers"]
    assert json.loads(call["data"])["generationConfig"]["maxOutputTokens"] >= 256
    assert call["allow_redirects"] is False and not sender.operation_auth
    assert secret not in json.dumps(result)
    assert result["runs"][0]["steps"]["image_png.observe"]["status"] == "passed"


@pytest.mark.parametrize("count,expected", [(500, "passed"), (9999, "failed"), (None, "inconclusive"), (True, "inconclusive")])
def test_native_counter_covers_original_audio_and_is_budgeted(config, tmp_path, count, expected):
    selected = binding("google_ai_studio", "gemini", "gemini-2.5-flash", "gemini_generate_content")
    plan, registry = media_input.prepare_media_input_plan(config, selected, selected_cases=["audio_wav"])
    sent = []
    def send(request, **kwargs):
        sent.append(copy.deepcopy(request))
        if request["path"].endswith(":countTokens"):
            return {"http_status": 200, "response_complete": True, "response": {"totalTokens": count}}
        return {"http_status": 200, "response_complete": True, "response": {
            "modelVersion": "gemini-2.5-flash", "candidates": [{"finishReason": "STOP", "content": {
                "role": "model", "parts": [{"text": "The lantern is green. The number is seven."}]}}],
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 12, "totalTokenCount": 512}}}
    report = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    assert len(sent) == plan["limits"]["max_requests"] == report["runs"][0]["business_request_count"] == 2
    counted = sent[1]["body"]["generateContentRequest"]
    assert counted["model"] == "models/gemini-2.5-flash"
    assert {k: v for k, v in counted.items() if k != "model"} == sent[0]["body"]
    assert report["runs"][0]["steps"]["audio_wav.observe"]["record"]["external_media_checks"] == []
    step = report["runs"][0]["steps"]["audio_wav"]
    assert step["status"] == expected
    if expected == "passed":
        assert step["identity_token_audit"]["independent_count"]["input"]["covers_full_input"] is True
        assert report["status"] == "passed"


def test_counter_cannot_change_model_origin_or_omit_full_request(config):
    selected = binding("google_ai_studio", "gemini", "gemini-2.5-flash", "gemini_generate_content")
    for key, value in [("path", "/v1beta/models/other:countTokens"), ("base_url", "https://other.example"),
                       ("request_wrapper", "contents")]:
        changed = copy.deepcopy(config)
        changed["providers"]["gemini"]["api_interfaces"]["token_count"][key] = value
        with pytest.raises(ValueError, match="counter"):
            media_input.prepare_media_input_plan(changed, selected, selected_cases=["audio_wav"])


def test_model_specific_source_override_is_checked(config):
    config["providers"]["openai_official"]["models"].setdefault("reference_source_ids", {})["gpt-4o"] = "xai"
    with pytest.raises(ValueError, match="different official source"):
        media_input.build_definition(config, binding())


def glm_public_plan(config):
    selected = binding("zhipu", "zhipu_official", "glm-5.3-flash", "openai_chat_completions")
    return media_input.prepare_media_input_plan(config, selected, selected_cases=["real_video_official_url"])


def public_video_receipt(**overrides):
    fixture = remote_video_fixture("zhipu_elephants")
    return {"http_status": 200, "response_complete": True, "response": None,
            "response_bytes_sha256": fixture["sha256"], "response_byte_length": fixture["byte_length"], **overrides}


def test_public_video_verification_and_model_request_share_one_counted_budget(config, tmp_path):
    plan, registry = glm_public_plan(config)
    sent = []
    def send(request, **kwargs):
        sent.append(copy.deepcopy(request))
        return public_video_receipt() if request["method"] == "GET" else receipt("elephants", model="glm-5.3-flash")
    report = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    run = report["runs"][0]
    assert [request["method"] for request in sent] == ["GET", "POST", "GET"]
    assert len(sent) == plan["limits"]["max_requests"] == run["business_request_count"] == 3
    assert plan["ordered_steps"][0]["request_cap"] == 3
    assert sent[0]["public"] is True and sent[2]["public"] is True
    assert sent[0]["url"] == sent[2]["url"] == remote_video_fixture()["external_url"]
    record = run["steps"]["real_video_official_url.observe"]["record"]
    assert record["model_request_sent"] is True
    assert record["compatibility_observation_pass"] is True
    assert [check["phase"] for check in record["external_media_checks"]] == ["before", "after"]
    assert all(check["verified"] for check in record["external_media_checks"])
    assert [attempt["kind"] for attempt in run["attempts"]] == ["fixture", "api", "fixture"]
    assert run["steps"]["real_video_official_url"]["status"] == "inconclusive", "Recognition alone is not complete media token proof"


@pytest.mark.parametrize("failed", [
    public_video_receipt(response_complete=False, error_type="ConnectionError"),
    public_video_receipt(response_bytes_sha256="0" * 64),
    public_video_receipt(response_byte_length=1),
    public_video_receipt(http_status=404),
])
def test_unverified_public_video_never_reaches_the_model(config, tmp_path, failed):
    plan, registry = glm_public_plan(config)
    sent = []
    def send(request, **kwargs):
        sent.append(copy.deepcopy(request))
        assert request["method"] == "GET", "Unverified public input must not be sent to a model"
        return failed
    report = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    assert len(sent) == report["runs"][0]["business_request_count"] == 1
    step = report["runs"][0]["steps"]["real_video_official_url.observe"]
    assert step["status"] == "inconclusive"
    assert step["record"]["model_request_sent"] is False
    assert step["reason"] == "public_media_bytes_unverified"
    assert report["status"] != "passed"


def test_public_video_change_after_model_response_is_inconclusive(config, tmp_path):
    plan, registry = glm_public_plan(config)
    sent = []
    def send(request, **kwargs):
        sent.append(copy.deepcopy(request))
        if request["method"] == "POST":
            return receipt("elephants", model="glm-5.3-flash")
        return public_video_receipt(response_bytes_sha256="0" * 64) if len(sent) == 3 else public_video_receipt()
    report = execute_plan(plan, registry, send, evidence_dir=tmp_path)
    step = report["runs"][0]["steps"]["real_video_official_url.observe"]
    assert [request["method"] for request in sent] == ["GET", "POST", "GET"]
    assert step["status"] == "inconclusive" and step["reason"] == "public_media_changed_during_request"
    assert step["record"]["verdict"]["semantic_pass"] is True
    assert step["record"]["compatibility_observation_pass"] is False
    assert [check["verified"] for check in step["record"]["external_media_checks"]] == [True, False]
    assert report["status"] != "passed"


def test_http_dispatcher_keeps_public_gets_credential_free_before_and_after_authentication(config):
    plan, _ = glm_public_plan(config)
    calls, key_reads = [], []
    secret = "media-public-auth-canary"
    raw_video = base64.b64decode(video_fixture()["data_base64"])
    class Response:
        status_code = 200
        def __init__(self, public):
            self.headers = {"content-type": "video/mp4" if public else "application/json"}
            self.raw = raw_video if public else json.dumps({**receipt("elephants", model="glm-5.3-flash")["response"], "diagnostic": secret}).encode()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def iter_content(self, size): yield self.raw
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def mount(self, *args): pass
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            calls.append({"method": method, "url": url, **kwargs})
            return Response(method == "GET")
    def credential(*args):
        key_reads.append(True)
        return ProviderCredential.create(provider="zhipu_official", secret=secret, base_urls=["https://api.z.ai"])
    sender = media_input.make_dispatcher(config, plan, session_factory=Session, credential_factory=credential)
    context = SimpleNamespace(target=plan["target"])
    public_request = {"method": "GET", "url": remote_video_fixture()["external_url"], "public": True}
    first = sender(public_request, timeout=10, context=context)
    assert first["response_complete"] is True and key_reads == []
    case = plan["ordered_steps"][0]["inputs"]["case"]
    authenticated = sender({"method": "POST", "path": plan["target"]["path"], "body": case["body"], "capture_raw": True}, timeout=10, context=context)
    after = sender(public_request, timeout=10, context=context)
    assert authenticated["response_complete"] is True and after["response_complete"] is True
    assert key_reads == [True]
    assert calls[0]["headers"] == calls[2]["headers"] == {}
    assert calls[1]["headers"]["Authorization"] == "Bearer " + secret
    assert all(call["allow_redirects"] is False for call in calls)
    assert secret not in json.dumps([first, authenticated, after])
    assert secret.encode() not in base64.b64decode(authenticated["raw_base64"])
