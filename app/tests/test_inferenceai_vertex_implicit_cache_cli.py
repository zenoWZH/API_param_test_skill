import copy
import json

import pytest

from lib.credential_security import ProviderCredential
from scripts import run_inferenceai_vertex_implicit_cache as cli


def config():
    return {"providers": {cli.PROVIDER: {"base_url": "https://model.service-inference.ai/v1",
        "models": {"default": cli.MODEL, "candidates": [cli.MODEL]},
        "api_interfaces": {"gemini_generate_content": {"base_url": "https://model.service-inference.ai/v1beta",
            "path": "/models/{model}:generateContent", "auth": "bearer"}}}}}


def key(secret="offline-implicit-account"):
    return ProviderCredential.create(provider=cli.PROVIDER, secret=secret,
                                     base_urls=["https://model.service-inference.ai"])


def prepare(tmp_path):
    credential = key()
    batch, package = cli.prepare_package(config=config(), output_dir=tmp_path / "implicit",
                                         credential_factory=lambda *_: credential)
    return batch, package, credential


class Sender:
    def __init__(self, *, reads=(0, 9000, 0), first_status=200, final_text="42"):
        self.reads, self.first_status, self.final_text = reads, first_status, final_text
        self.requests = []

    def __call__(self, request, *, context, **_):
        self.requests.append(copy.deepcopy(request))
        index = len(self.requests) - 1
        assert request["method"] == "POST" and request["path"].endswith(":generateContent")
        assert "cachedContent" not in request["body"]
        if index == 0 and self.first_status != 200:
            return {"http_status": self.first_status, "response_complete": True,
                    "response": {"error": {"message": "upstream unavailable"}}}
        count = 10301 if index == 2 else 10300
        usage = {"promptTokenCount": count, "candidatesTokenCount": 2, "totalTokenCount": count + 2}
        if self.reads[index] is not None:
            usage["cachedContentTokenCount"] = self.reads[index]
        payload = {"modelVersion": cli.MODEL, "responseId": f"offline-{index}",
            "candidates": [{"index": 0, "finishReason": "STOP", "content": {"role": "model",
                "parts": [{"text": self.final_text if index == 2 else "42"}]}}], "usageMetadata": usage}
        return {"http_status": 200, "response_complete": True, "response": payload}


def execute(tmp_path, sender):
    batch, package, credential = prepare(tmp_path)
    result = cli.execute_package(batch, package["package_sha256"], config=config(),
        credential_factory=lambda *_: credential, dispatcher=sender)
    return batch, package, credential, result


def test_implicit_cli_runs_complete_trio_without_resource_api_and_cannot_replay(tmp_path):
    sender = Sender()
    batch, package, credential, result = execute(tmp_path, sender)
    assert result["pass"] and result["cache_summary"]["cache_outcome"] == "verified"
    assert result["cache_mode"] == "implicit_prefix" and result["state_resources"] is False
    assert len(sender.requests) == 3
    assert sender.requests[0]["body"] == sender.requests[1]["body"]
    assert sender.requests[0]["body"] != sender.requests[2]["body"]
    run = result["workflow_result"]["runs"][0]
    assert run["business_request_count"] == 3 and run["cleanup_request_count"] == 0
    ledger = json.loads(open(run["ledger_path"]).read())
    assert ledger["creations"] == ledger["resources"] == []
    assert "offline-implicit-account" not in json.dumps(package)
    with pytest.raises(FileExistsError):
        cli.execute_package(batch, package["package_sha256"], config=config(),
            credential_factory=lambda *_: credential, dispatcher=sender)
    assert len(sender.requests) == 3


@pytest.mark.parametrize("reads,outcome", [((None, None, None), "unknown"), ((0, 0, 0), "expectation_failed")])
def test_missing_or_unhit_cache_remains_distinct_from_supported_verified(tmp_path, reads, outcome):
    sender = Sender(reads=reads)
    _, _, _, result = execute(tmp_path, sender)
    assert not result["pass"] and result["cache_summary"]["cache_outcome"] == outcome
    assert len(sender.requests) == 3
    if outcome == "unknown":
        assert all(row["observation"]["telemetry"]["cached_input_tokens"] is None
                   for row in result["cache_summary"]["observations"])


def test_http_failure_blocks_remaining_controls_and_no_resource_cleanup(tmp_path):
    sender = Sender(first_status=503)
    _, _, _, result = execute(tmp_path, sender)
    assert not result["pass"] and len(sender.requests) == 1
    summary = result["cache_summary"]
    assert len(summary["not_sent_case_ids"]) == 2 and summary["cache_outcome"] == "unknown"
    assert result["workflow_result"]["runs"][0]["cleanup_request_count"] == 0


def test_final_semantic_error_cannot_be_hidden_by_legacy_cache_metrics(tmp_path):
    _, _, _, result = execute(tmp_path, Sender(final_text="43"))
    summary = result["cache_summary"]
    assert summary["acceptance"]["status"] == "pass"
    assert not result["pass"] and not summary["semantic_controls_verified"]
    assert summary["cache_outcome"] != "verified"


def test_wrong_route_before_credential_lookup_and_changed_account_before_dispatch(tmp_path):
    cfg = config()
    cfg["providers"][cli.PROVIDER]["api_interfaces"]["gemini_generate_content"]["base_url"] = "https://other.invalid/v1beta"
    with pytest.raises(ValueError):
        cli.prepare_package(config=cfg, output_dir=tmp_path / "bad",
            credential_factory=lambda *_: pytest.fail("Bad preflight reached credential lookup"))
    batch, package, _ = prepare(tmp_path)
    sender = Sender()
    with pytest.raises(ValueError, match="account changed"):
        cli.execute_package(batch, package["package_sha256"], config=config(),
            credential_factory=lambda *_: key("another-offline-account"), dispatcher=sender)
    assert sender.requests == [] and not (batch / "dispatch_started.json").exists()


def test_cli_has_no_resource_recovery_or_mutable_model_execution_switch():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--execute", "/unused", "--package-sha256", "0" * 64, "--cleanup-only", "not-a-resource-run"])
    assert exc.value.code == 2
