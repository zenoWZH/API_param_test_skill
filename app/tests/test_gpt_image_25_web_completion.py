"""Exercise the actual image CLI output through Web completion and UI gates."""
from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest
from PIL import Image
import yaml

from lib import gpt_image_25_responses as matrix
from lib.gpt_image_25_web_audit import POLICY_NAME
from lib.job_spec import classify_parameter_result
from scripts import image_param_test, run_gpt_image_25_responses_reference as runner, web_console

ROOT = Path(__file__).resolve().parents[1]


def response_payload(*, size=(1024, 1024)):
    output = io.BytesIO()
    Image.new("RGB", size, (0, 80, 220)).save(output, format="PNG")
    return {"object": "response", "id": "resp_web_fixture", "model": matrix.MAINLINE_MODEL,
            "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "image_generation_call", "id": "ig_web_fixture", "status": "completed",
                        "quality": "low", "size": "1024x1024", "result": base64.b64encode(output.getvalue()).decode()}],
            "usage": {"input_tokens": 100, "output_tokens": 35, "total_tokens": 135,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 10}},
            "tool_usage": {"image_gen": {"input_tokens": 12, "output_tokens": 196, "total_tokens": 208,
                           "input_tokens_details": {"text_tokens": 12, "image_tokens": 0},
                           "output_tokens_details": {"text_tokens": 0, "image_tokens": 196}}}}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"content-type": "application/json", "x-request-id": "req_web_fixture"}
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size=65536):
        yield self.raw


@pytest.fixture
def job_environment(tmp_path, monkeypatch):
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("LLM_API_TEST_DISABLE_AUTH", "1")
    monkeypatch.setenv("LLM_API_TEST_SKIP_HISTORY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-web-image-fixture")
    monkeypatch.setenv("IMAGE_TEST_API_KEY", "offline-web-image-fixture")
    monkeypatch.setattr(web_console, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(image_param_test, "load_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(web_console, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(web_console.JobManager, "_start_locked", lambda *args: None)
    monkeypatch.setattr(web_console, "image_provider_has_api_key", lambda *args: True)
    monkeypatch.setattr(runner, "REPORT_ROOT", tmp_path / "source")
    # No requests outside the fake Session below, including inventory probes.
    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: pytest.fail("unexpected HTTP"))
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(runner.requests, "Session", lambda: session)
    return web_console.JobManager(), session


def create_job(manager, model, *, rejection=False):
    return manager.create({"type": "image_param_test", "provider": "openai_official", "model": model,
        "image_plan": {"suite": "resolution", "api_form": "openai_responses", "route_profile": "vendor_direct",
                       "cases": ["gpt_image_25_responses_reject_edit_without_image" if rejection
                                 else "gpt_image_25_responses_baseline_generate"]}})


def frontend_result(job):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the actual frontend gate check")
    source = (ROOT / "scripts/static/web_console.js").read_text()
    declarations = list(re.finditer(r"^function ([A-Za-z0-9_]+)\(", source, flags=re.M))
    functions = {m.group(1): source[m.start():declarations[i + 1].start() if i + 1 < len(declarations) else len(source)]
                 for i, m in enumerate(declarations)}
    selected = ["isResponsesImageAudit", "responsesImageExchangePass", "responsesImageGateInfo", "tokenExchangeValidationPass",
                "caseOverallPass", "tokenValidationGateInfo", "tokenValidationGateLabel", "tokenExchangeCountLabel",
                "imageTokenAuditDetail", "imageTestMetrics", "auditDimension", "auditStatusLabel"]
    program = '\n'.join(functions[name] for name in selected)
    program += '\nfunction tokenAuditSources() { return []; }\nfunction esc(x) { return String(x ?? ""); }\nfunction fmtNum(x) { return x == null ? "n/a" : String(x); }\n'
    program += '\nconst job=JSON.parse(require("fs").readFileSync(0,"utf8")); const audit=job.image_summary.token_audit_summary; process.stdout.write(JSON.stringify({gate:tokenValidationGateInfo(job,audit),label:tokenValidationGateLabel(job,audit),cases:job.image_results.map(caseOverallPass),details:job.image_results.map(imageTokenAuditDetail),metrics:imageTestMetrics(job)}));'
    result = subprocess.run([node, "-e", program], input=json.dumps(job), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("model", matrix.MODELS)
@pytest.mark.parametrize("scenario", ["success", "rejection", "missing_image_usage", "wrong_arithmetic", "wrong_dimensions", "wrong_response_id", "wrong_image_id_list", "wrong_image_id_object", "incomplete", "malformed_output", "oversized_mainline_output"])
def test_cli_web_and_frontend_agree_on_real_dedicated_results(job_environment, monkeypatch, model, scenario, capsys):
    manager, session = job_environment
    good = scenario in {"success", "rejection"}
    job = create_job(manager, model, rejection=scenario == "rejection")
    assert job.job_spec["result_contract"]["validation_policy"] == POLICY_NAME
    payload = response_payload(size=(512, 512) if scenario == "wrong_dimensions" else (1024, 1024))
    status = 200
    if scenario == "rejection":
        payload, status = {"error": {"type": "invalid_request_error", "param": "action", "message": "edit requires an input image"}}, 400
    elif scenario == "missing_image_usage":
        payload.pop("tool_usage")
    elif scenario == "wrong_arithmetic":
        payload["usage"]["total_tokens"] += 1
    elif scenario == "wrong_response_id":
        payload["id"] = "invalid"
    elif scenario == "wrong_image_id_list":
        payload["output"][0]["id"] = ["ig_invalid"]
    elif scenario == "wrong_image_id_object":
        payload["output"][0]["id"] = {"id": "ig_invalid"}
    elif scenario == "incomplete":
        payload["status"] = "incomplete"
    elif scenario == "malformed_output":
        payload["output"] = None
    elif scenario == "oversized_mainline_output":
        payload["usage"]["output_tokens"] = matrix.MAX_OUTPUT_TOKENS + 1
        payload["usage"]["total_tokens"] = payload["usage"]["input_tokens"] + payload["usage"]["output_tokens"]
    session.request.return_value = FakeResponse(payload, status)
    monkeypatch.setenv("LOADTEST_JOB_SPEC", str(job.report_dir / "job_spec.json"))
    returncode = image_param_test.main(job.command[2:])
    capsys.readouterr()
    assert session.request.call_count == 1
    assert session.request.call_args.args == ("POST", matrix.ENDPOINT)
    assert returncode == (0 if good else 1)
    summary = json.loads((job.report_dir / "summary.json").read_text())
    rows = json.loads((job.report_dir / "case_results.json").read_text())
    validation = classify_parameter_result(job.job_spec, summary)
    assert validation["pass"] is good, validation["reasons"]
    assert web_console._result_gated_completion(job, returncode) == ((0, "completed") if good else (1, "failed"))
    assert len(rows[0]["token_audit"]["exchanges"]) == 1
    public = {"type": "image_param_test", "status": "completed" if good else "failed", "returncode": returncode,
              "image_summary": web_console._public_result_view(summary, validation), "image_results": rows}
    ui = frontend_result(public)
    assert ui["gate"]["pass"] is good
    assert ui["cases"] == [good]
    if scenario == "success":
        assert "Main model" in ui["details"][0]
        assert "aggregate estimate" in ui["details"][0]
        assert "unverified" in ui["details"][0]
        # The UI cannot promote a summary without current backend policy/identity proof.
        public["image_summary"].pop("result_validation")
        assert frontend_result(public)["gate"]["pass"] is None
    elif scenario == "rejection":
        assert summary["token_audit_summary"]["required_exchange_count"] == 0
        assert summary["token_audit_summary"]["expected_rejection_count"] == 1
        assert ui["label"] == "N/A (expected rejection)"
        assert "No generation usage required" in ui["details"][0]


def test_outbound_mutation_cannot_manufacture_a_web_pass(job_environment, monkeypatch, capsys):
    from lib.test_runner.adapters import image
    manager, session = job_environment
    job = create_job(manager, matrix.MODELS[0])
    session.request.return_value = FakeResponse(response_payload())
    original = image.make_image_dispatcher
    def changed_dispatcher(*args, **kwargs):
        dispatcher = original(*args, **kwargs)
        def changed_request(request, **options):
            request = copy.deepcopy(request)
            request["body"]["input"] = "a different prompt than the frozen case"
            return dispatcher(request, **options)
        return changed_request
    monkeypatch.setattr(image, "make_image_dispatcher", changed_dispatcher)
    monkeypatch.setenv("LOADTEST_JOB_SPEC", str(job.report_dir / "job_spec.json"))
    assert image_param_test.main(job.command[2:]) == 1
    capsys.readouterr()
    rows = json.loads((job.report_dir / "case_results.json").read_text())
    assert rows[0]["request_input_integrity"]["status"] == "fail"
    assert web_console._result_gated_completion(job, 0) == (1, "failed")
