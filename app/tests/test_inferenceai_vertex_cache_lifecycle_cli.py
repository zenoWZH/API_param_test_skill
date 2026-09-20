import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lib.credential_security import ProviderCredential
from scripts import run_inferenceai_vertex_cache_lifecycle as cli


def config():
    return {"providers": {cli.PROVIDER: {
        "base_url": "https://model.service-inference.ai/v1", "default_transport": "chat_completions",
        "models": {"default": cli.MODEL, "candidates": [cli.MODEL]},
        "api_interfaces": {
            "chat_completions": {"path": "/chat/completions", "auth": "bearer"},
            "gemini_generate_content": {"base_url": "https://model.service-inference.ai/v1beta",
                "path": "/models/{model}:generateContent", "auth": "bearer"},
        },
    }}}


def credential(secret="offline-gateway-account"):
    return ProviderCredential.create(provider=cli.PROVIDER, secret=secret,
                                     base_urls=["https://model.service-inference.ai"])


def prepare(tmp_path):
    key = credential()
    batch, package = cli.prepare_package(config=config(), output_dir=tmp_path / "sample",
                                         credential_factory=lambda *_: key)
    return batch, package, key


class Sender:
    def __init__(self, package, *, reject_create=False, delete_status=200):
        self.package, self.reject_create, self.delete_status = package, reject_create, delete_status
        self.requests = []

    def __call__(self, request, *, context, **_):
        self.requests.append(copy.deepcopy(request))
        if request["method"] == "DELETE":
            return self.receipt({} if self.delete_status == 200 else {"error": {"message": "temporary delete failure"}}, self.delete_status)
        if "cachedContent" in request.get("body", {}):
            assert request["body"]["cachedContent"] == "cachedContents/owned-cli-cache"
            assert self.package["nonce"] not in json.dumps(request["body"])
            return self.receipt({"modelVersion": cli.MODEL, "responseId": "offline-response",
                "candidates": [{"index": 0, "finishReason": "STOP", "content": {
                    "role": "model", "parts": [{"text": self.package["nonce"]}]}}],
                "usageMetadata": {"promptTokenCount": 8200, "cachedContentTokenCount": 8192,
                                  "candidatesTokenCount": 12, "totalTokenCount": 8212}})
        if self.reject_create:
            return self.receipt({"error": {"type": "proxy_error",
                "message": "cachedContents is only supported for Gemini-format providers"}}, 400)
        now = datetime.now(timezone.utc)
        return self.receipt({"name": "cachedContents/owned-cli-cache", "model": "models/" + cli.MODEL,
                             "createTime": now.isoformat(), "updateTime": now.isoformat(),
                             "expireTime": (now + timedelta(seconds=cli.TTL_SECONDS)).isoformat(),
                             "usageMetadata": {"totalTokenCount": 8192}})

    @staticmethod
    def receipt(payload, status=200):
        return {"http_status": status, "response_complete": True, "response": payload}


def test_prepared_sample_executes_shared_lifecycle_once_and_retains_evidence(tmp_path):
    batch, package, key = prepare(tmp_path)
    plan = package["execution_plan"]
    assert plan["run_count"] == 1
    assert plan["limits"]["max_requests"] == 2 and plan["limits"]["cleanup_max_requests"] == 1
    assert "offline-gateway-account" not in json.dumps(package)
    sender = Sender(package)
    result = cli.execute_package(batch, package["package_sha256"], config=config(),
                                 credential_factory=lambda *_: key, dispatcher=sender)
    assert result["pass"] and not result["google_direct"] and not result["full_parameter_certification"]
    assert [r["method"] for r in sender.requests] == ["POST", "POST", "DELETE"]
    for request in (sender.requests[0], sender.requests[-1]):
        assert parse_qs(urlsplit(request["path"]).query) == {"model": [cli.MODEL]}
    run = result["workflow_result"]["runs"][0]
    assert run["business_request_count"] == 2 and run["cleanup_request_count"] == 1
    assert run["cleanup"]["resources"][0]["status"] == "deleted"
    retained = json.loads((batch / ".report-retention.json").read_text())
    assert {"result.json", "request_package.json", "dispatch_started.json"} <= {r["path"] for r in retained["owned_files"]}
    with pytest.raises(FileExistsError):
        cli.execute_package(batch, package["package_sha256"], config=config(),
                            credential_factory=lambda *_: key, dispatcher=sender)
    assert len(sender.requests) == 3


def test_explicit_creation_rejection_blocks_use_without_fake_cleanup(tmp_path):
    batch, package, key = prepare(tmp_path)
    sender = Sender(package, reject_create=True)
    result = cli.execute_package(batch, package["package_sha256"], config=config(),
                                 credential_factory=lambda *_: key, dispatcher=sender)
    assert not result["pass"] and len(sender.requests) == 1
    run = result["workflow_result"]["runs"][0]
    assert run["cleanup_request_count"] == 0
    assert run["cleanup"]["resources"] == [] and run["cleanup"]["unknown_creations"] == []
    assert "blocked" in [step["status"] for step in run["steps"].values()]


def test_bad_route_and_mutated_package_fail_before_credential_lookup(tmp_path):
    cfg = config()
    cfg["providers"][cli.PROVIDER]["api_interfaces"]["gemini_generate_content"]["base_url"] = "https://unrelated.invalid/v1beta"
    never = lambda *_: pytest.fail("Invalid preflight looked up a credential")
    with pytest.raises(ValueError):
        cli.prepare_package(config=cfg, output_dir=tmp_path / "bad", credential_factory=never)
    batch, package, _key = prepare(tmp_path)
    changed = json.loads((batch / "request_package.json").read_text())
    changed["model"] = "unselected-model"
    (batch / "request_package.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="checksum"):
        cli.execute_package(batch, package["package_sha256"], config=config(), credential_factory=never)
    assert not (batch / "dispatch_started.json").exists()


def test_cleanup_forks_immutable_results_and_second_recovery_sends_nothing(tmp_path):
    batch, package, key = prepare(tmp_path)
    sender = Sender(package, delete_status=500)
    result = cli.execute_package(batch, package["package_sha256"], config=config(),
                                 credential_factory=lambda *_: key, dispatcher=sender)
    assert not result["pass"]
    run = result["workflow_result"]["runs"][0]
    original_ledger = Path(run["ledger_path"])
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original_ledger, batch / "result.json")}
    deletion = Sender(package)
    recovered = cli.execute_package(batch, package["package_sha256"], config=config(),
        credential_factory=lambda *_: key, dispatcher=deletion, cleanup_run_id=run["run_id"])
    assert recovered["pass"] and recovered["proof_scope"] == "cleanup_only"
    assert [r["method"] for r in deletion.requests] == ["DELETE"]
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    repeated = Sender(package)
    twice = cli.execute_package(batch, package["package_sha256"], config=config(),
        credential_factory=lambda *_: key, dispatcher=repeated, cleanup_run_id=run["run_id"])
    assert twice["pass"] and repeated.requests == []


def test_changed_credential_account_never_dispatches_or_claims_business(tmp_path):
    batch, package, _ = prepare(tmp_path)
    sender = Sender(package)
    with pytest.raises(ValueError, match="account changed"):
        cli.execute_package(batch, package["package_sha256"], config=config(),
            credential_factory=lambda *_: credential("different-offline-account"), dispatcher=sender)
    assert sender.requests == [] and not (batch / "dispatch_started.json").exists()


def test_cleanup_can_delete_original_cache_after_model_is_removed_from_new_selection(tmp_path, monkeypatch):
    batch, package, key = prepare(tmp_path)
    failed = cli.execute_package(batch, package["package_sha256"], config=config(),
        credential_factory=lambda *_: key, dispatcher=Sender(package, delete_status=500))
    run_id = failed["workflow_result"]["runs"][0]["run_id"]
    changed = config()
    changed["providers"][cli.PROVIDER]["models"] = {"candidates": []}
    sent = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def iter_content(self, *_args, **_kwargs): yield b"{}"

    class Session:
        trust_env = False
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def mount(self, *_): pass
        def request(self, method, url, **_):
            sent.append((method, url))
            return Response()

    original_factory = cli.make_gateway_dispatcher
    monkeypatch.setattr(cli, "make_gateway_dispatcher", lambda cfg, plan, **kwargs:
        original_factory(cfg, plan, session_factory=Session, **kwargs))
    recovered = cli.execute_package(batch, package["package_sha256"], config=changed,
        credential_factory=lambda *_: key, cleanup_run_id=run_id)
    assert recovered["pass"]
    assert sent == [("DELETE", "https://model.service-inference.ai/v1beta/cachedContents/owned-cli-cache?model=" + cli.MODEL)]


def test_frozen_cli_rejects_changed_model_and_output_directory():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--execute", "/unused", "--package-sha256", "0" * 64, "--model", cli.MODEL])
    assert exc.value.code == 2
