import json
from types import SimpleNamespace

import pytest

from lib.test_runner import transport as module


class Credential:
    def auth_headers(self, **kwargs):
        return {"x-api-key": "synthetic-secret"}
    def redact(self, value):
        return value


class Response:
    status_code = 200
    headers = {"content-type": "application/json"}
    raw = b'{"ok":true}'
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def iter_content(self, size):
        yield self.raw


class Session:
    sent = []
    trust_env = True
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def mount(self, *args):
        pass
    def request(self, method, url, **kwargs):
        assert self.trust_env is False
        self.sent.append((method, url, kwargs))
        return Response()


@pytest.fixture
def sender(monkeypatch):
    Session.sent = []
    calls = []
    monkeypatch.setattr(module, "get_provider_interface", lambda *a: {
        "base_url": "https://api.example.test/v1", "auth": "anthropic", "anthropic_version": "2023-06-01"})
    def credential(*args):
        calls.append("credential")
        return Credential()
    dispatcher = module.HttpDispatcher({}, "fixture", "claude_messages", session_factory=Session,
                                       credential_factory=credential, public_urls=("https://public.example.test/fixture",))
    context = SimpleNamespace(target={"execution_target": {"provider_id": "fixture"}, "base_url": "https://api.example.test/v1"})
    return dispatcher, context, calls


def request():
    return {"method": "POST", "path": "/v1/messages", "headers": {"anthropic-beta": "fixture-beta"},
            "body": {"model": "fixture", "max_tokens": 256, "messages": [{"role": "user", "content": "fixture"}],
                     "speed": "standard", "stream": "intentionally-wrong-type"}}


def test_wire_body_and_case_headers_are_not_normalized(sender):
    dispatcher, context, calls = sender
    body = request()
    row = dispatcher(body, timeout=1, context=context)
    assert row["response_complete"] is True
    assert calls == ["credential"]
    method, url, sent = Session.sent[0]
    assert url == "https://api.example.test/v1/messages"
    assert json.loads(sent["data"]) == body["body"]
    assert sent["headers"]["anthropic-beta"] == "fixture-beta"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["allow_redirects"] is False
    assert "synthetic-secret" not in repr(row)


@pytest.mark.parametrize("path", ["//other.example/steal", "https://other.example/messages", "/v1/../admin"])
def test_untrusted_endpoint_fails_before_credentials(sender, path):
    dispatcher, context, calls = sender
    value = request(); value["path"] = path
    with pytest.raises(ValueError):
        dispatcher(value, timeout=1, context=context)
    assert calls == [] and Session.sent == []


@pytest.mark.parametrize("headers", [{"Authorization": "replacement"}, {"X-Api-Key": "replacement"}, {"x-case": "x\r\ny"}])
def test_case_cannot_override_authentication(sender, headers):
    dispatcher, context, calls = sender
    value = request(); value["headers"] = headers
    with pytest.raises(ValueError):
        dispatcher(value, timeout=1, context=context)
    assert calls == [] and Session.sent == []


def test_underfloor_request_is_not_rewritten_or_sent(sender):
    dispatcher, context, calls = sender
    value = request(); value["body"]["max_tokens"] = 255
    with pytest.raises(ValueError, match="allowance"):
        dispatcher(value, timeout=1, context=context)
    assert value["body"]["max_tokens"] == 255
    assert calls == [] and Session.sent == []


def test_public_fixture_has_no_credentials(sender):
    dispatcher, context, calls = sender
    row = dispatcher({"method": "GET", "url": "https://public.example.test/fixture", "public": True}, timeout=1, context=context)
    assert row["response_complete"] is True
    assert calls == []
    assert Session.sent[0][2]["headers"] == {}


def test_public_url_requires_exact_fixture_allowlist(sender):
    dispatcher, context, calls = sender
    with pytest.raises(ValueError):
        dispatcher({"method": "GET", "url": "https://public.example.test/other", "public": True}, timeout=1, context=context)
    assert calls == [] and Session.sent == []


def test_byte_limit_returns_incomplete_receipt(sender, monkeypatch):
    dispatcher, context, calls = sender
    monkeypatch.setattr(Response, "raw", b"x" * 100)
    value = request(); value["response_byte_limit"] = 8
    row = dispatcher(value, timeout=1, context=context)
    assert row["response_complete"] is False
    assert row["error_type"] == "ValueError"


def test_public_allowance_does_not_expand_authenticated_response_limit(sender, monkeypatch):
    dispatcher, context, calls = sender
    dispatcher.response_byte_limit = 8
    dispatcher.public_response_byte_limit = 32
    monkeypatch.setattr(Response, "raw", b"x" * 16)
    public = dispatcher({"method": "GET", "url": "https://public.example.test/fixture", "public": True}, timeout=1, context=context)
    assert public["response_complete"] is True and calls == []
    authenticated = dispatcher(request(), timeout=1, context=context)
    assert authenticated["response_complete"] is False


def test_dispatcher_rejects_different_frozen_provider(sender):
    dispatcher, context, calls = sender
    context.target["execution_target"]["provider_id"] = "another"
    with pytest.raises(ValueError, match="provider"):
        dispatcher(request(), timeout=1, context=context)
    assert calls == [] and Session.sent == []


@pytest.mark.parametrize("provider", ["anthropic_official", "inferenceai_awsb", "sanqiaoapi"])
def test_registered_fable_helpers_preserve_their_authentication(provider, monkeypatch):
    from lib.test_runner.adapters.fable import http_operation_auth
    modes = []
    class AuthCredential(Credential):
        def auth_headers(self, **kwargs):
            modes.append(kwargs["auth_mode"])
            return {}
    monkeypatch.setattr(module, "get_provider_interface", lambda *a: {
        "base_url": "https://api.example.test/v1", "auth": "anthropic"})
    context = SimpleNamespace(target={"execution_target": {"provider_id": provider}})
    dispatch = module.HttpDispatcher({}, provider, "claude_messages", session_factory=Session,
        credential_factory=lambda *args: AuthCredential(), operation_auth=http_operation_auth(provider))
    dispatch(request(), timeout=1, context=context)
    for method, path in (("POST", "/v1/files"), ("DELETE", "/v1/files/file_owned"), ("GET", "/v1/models/model")):
        dispatch({"method": method, "path": path}, timeout=1, context=context)
    assert modes == ["anthropic", *(["anthropic"] * 3 if provider == "anthropic_official" else ["bearer"] * 3)]
    # Prefix policy never bleeds into a similarly named resource or Messages.
    dispatch({"method": "GET", "path": "/v1/files-admin"}, timeout=1, context=context)
    assert modes[-1] == "anthropic"


def test_duplicate_json_is_not_a_complete_verified_response(sender, monkeypatch):
    dispatcher, context, _ = sender
    monkeypatch.setattr(Response, "raw", b'{"usage":1,"usage":2}')
    row = dispatcher(request(), timeout=1, context=context)
    assert row["response_complete"] is False
    assert row["error_type"] == "ValueError"


def test_protocol_raw_capture_is_explicit_and_response_headers_are_preserved(sender, monkeypatch):
    import base64
    dispatcher, context, _ = sender
    monkeypatch.setattr(Response, "headers", {"content-type": "application/json", "Retry-After": "2", "Set-Cookie": "private"})
    value = request(); value["capture_raw"] = True
    row = dispatcher(value, timeout=1, context=context)
    assert base64.b64decode(row["raw_base64"]) == Response.raw
    assert row["response_headers"]["retry-after"] == "2"
    assert "set-cookie" not in row["response_headers"]


def test_encoded_wire_capture_redacts_reflected_credentials(sender, monkeypatch):
    import base64
    import hashlib
    from lib.credential_security import ProviderCredential
    dispatcher, context, _ = sender
    key = "workflow-private-key-do-not-persist"
    credential = ProviderCredential.create(provider="fixture", secret=key, base_urls=["https://api.example.test/v1"])
    dispatcher._credential_factory = lambda *args: credential
    monkeypatch.setattr(Response, "raw", json.dumps({"error": {"message": "bad key " + key}}).encode())
    value = request(); value["capture_raw"] = True
    row = dispatcher(value, timeout=1, context=context)
    captured = base64.b64decode(row["raw_base64"])
    assert key.encode() not in captured and key not in repr(row)
    assert row["raw_redacted"] is True
    assert row["captured_bytes_sha256"] == hashlib.sha256(captured).hexdigest()
    assert row["response_bytes_sha256"] == hashlib.sha256(Response.raw).hexdigest()


@pytest.mark.parametrize("escaped_field", [False, True])
def test_json_capture_redacts_unicode_escaped_secrets_and_sensitive_field_names(sender, monkeypatch, escaped_field):
    import base64
    import hashlib
    from lib.credential_security import ProviderCredential, REDACTED

    dispatcher, context, _ = sender
    key = "escaped-json-fixture-key-never-a-real-credential"
    credential = ProviderCredential.create(provider="fixture", secret=key, base_urls=["https://api.example.test/v1"])
    dispatcher._credential_factory = lambda *args: credential
    escaped_key = "".join("\\u%04x" % ord(char) for char in key)
    field = r"api_\u006bey" if escaped_field else "api_key"
    raw = ('{ "model":"fixture", "echo":"' + escaped_key + '", "diagnostics":{"' + field +
           '":"server-side-fixture-token","retained":123}, "items":[1,2,3] }').encode()
    monkeypatch.setattr(Response, "raw", raw)
    value = request(); value["capture_raw"] = True
    row = dispatcher(value, timeout=1, context=context)
    captured = base64.b64decode(row["raw_base64"])
    decoded = json.loads(captured)
    assert decoded == {"model": "fixture", "echo": REDACTED,
                       "diagnostics": {"api_key": REDACTED, "retained": 123}, "items": [1, 2, 3]}
    assert row["response"] == decoded
    assert key not in json.dumps(decoded) and "server-side-fixture-token" not in captured.decode()
    assert row["raw_redacted"] is True
    assert row["response_bytes_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["captured_bytes_sha256"] == hashlib.sha256(captured).hexdigest()
    assert row["response_byte_length"] == len(raw) and row["captured_byte_length"] == len(captured)


def test_json_capture_without_redaction_preserves_exact_wire_bytes(sender, monkeypatch):
    import base64
    from lib.credential_security import ProviderCredential

    dispatcher, context, _ = sender
    credential = ProviderCredential.create(provider="fixture", secret="unused-raw-capture-fixture-key",
                                           base_urls=["https://api.example.test/v1"])
    dispatcher._credential_factory = lambda *args: credential
    raw = b' { "retained" : "\\u0394",\r\n "items" : [ 1, 2 ], "plain":true }\n'
    monkeypatch.setattr(Response, "raw", raw)
    value = request(); value["capture_raw"] = True
    row = dispatcher(value, timeout=1, context=context)
    assert base64.b64decode(row["raw_base64"]) == raw
    assert row["raw_redacted"] is False
    assert row["response_bytes_sha256"] == row["captured_bytes_sha256"]


@pytest.mark.parametrize("multiline", [False, True])
@pytest.mark.parametrize("capture_raw", [False, True])
def test_sse_json_redaction_protects_capture_and_events_without_losing_fields(sender, monkeypatch, multiline, capture_raw):
    import base64
    import hashlib
    from lib.credential_security import ProviderCredential, REDACTED

    dispatcher, context, _ = sender
    dispatcher.transport = "chat_completions"
    key = "escaped-sse-fixture-key-never-a-real-credential"
    credential = ProviderCredential.create(provider="fixture", secret=key, base_urls=["https://api.example.test/v1"])
    dispatcher._credential_factory = lambda *args: credential
    escaped_key = "".join("\\u%04x" % ord(char) for char in key)
    first = '{"delta":"' + escaped_key + '",'
    second = r'"diagnostics":{"authoriz\u0061tion":"synthetic-upstream-token","retained":9},"image":{"b64_json":"AAECAw=="}}'
    data = ("data: " + first + "\r\n: retained-mid-event-comment\r\ndata: " + second if multiline
            else "data: " + first + second)
    unchanged = 'event: message\r\ndata: { "delta" : "safe", "retained":true }\r\n\r\n'
    raw = ('id: fixture-1\r\nevent: message\r\nretry: 1000\r\n' + data + '\r\n\r\n'
           + unchanged + 'data: [DONE]\r\n\r\n').encode()
    monkeypatch.setattr(Response, "headers", {"content-type": "text/event-stream"})
    monkeypatch.setattr(Response, "raw", raw)
    value = request(); value["capture_raw"] = capture_raw
    row = dispatcher(value, timeout=1, context=context)
    captured = base64.b64decode(row["raw_base64"])
    assert captured.decode() == row["stream_events_text"]
    first_data = next(line[6:] for line in row["stream_events_text"].splitlines() if line.startswith("data: {"))
    decoded = json.loads(first_data)
    assert decoded == {"delta": REDACTED, "diagnostics": {"authorization": REDACTED, "retained": 9},
                       "image": {"b64_json": "AAECAw=="}}
    assert 'id: fixture-1\r\nevent: message\r\nretry: 1000\r\n' in row["stream_events_text"]
    assert unchanged in row["stream_events_text"] and 'data: [DONE]\r\n\r\n' in row["stream_events_text"]
    if multiline:
        assert ': retained-mid-event-comment\r\n' in row["stream_events_text"]
    assert "synthetic-upstream-token" not in row["stream_events_text"]
    assert row["raw_redacted"] is True
    assert row["response_bytes_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["captured_bytes_sha256"] == hashlib.sha256(captured).hexdigest()


def test_sse_capture_without_redaction_preserves_wire_and_decoded_events(sender, monkeypatch):
    import base64
    from lib.credential_security import ProviderCredential

    dispatcher, context, _ = sender
    dispatcher.transport = "chat_completions"
    credential = ProviderCredential.create(provider="fixture", secret="unused-sse-capture-fixture-key",
                                           base_urls=["https://api.example.test/v1"])
    dispatcher._credential_factory = lambda *args: credential
    raw = b': keepalive\n\nevent: delta\ndata: { "delta" : "\\u0394", "image":"AAECAw==" }\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(Response, "headers", {"content-type": "text/event-stream"})
    monkeypatch.setattr(Response, "raw", raw)
    row = dispatcher(request(), timeout=1, context=context)
    assert base64.b64decode(row["raw_base64"]) == raw
    assert row["stream_events_text"] == raw.decode()
    assert row["raw_redacted"] is False
