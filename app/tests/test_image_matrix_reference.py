from __future__ import annotations

import copy
import base64
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image
import pytest

from lib import image_matrix_reference as reference
from lib.image_validation import inspect_image_bytes
from lib.reference_specs import load_model_capability_profile
from scripts import image_param_test as cli
from scripts import run_gpt_image_25_responses_reference as responses


def test_resolution_charge_opt_ins_preserve_negative_boundary_probes():
    cases = reference.select_image_matrix_cases(
        "grok-imagine-image-2.0", "openai_images_generations",
        suite="full", include_2k=True, include_4k=False,
    )
    rejection = [case for case in cases if case.parameters.get("resolution") == "4k"]
    assert len(rejection) == 1 and rejection[0].expected_outcome == "rejection"
    without_negative = reference.select_image_matrix_cases(
        "grok-imagine-image-2.0", "openai_images_generations",
        suite="full", include_2k=True, include_negative=False,
    )
    assert all(case.expected_outcome != "rejection" for case in without_negative)


def _candidate(tmp_path, model="gpt-image-2", api_form="openai_images_generations"):
    source = "xai" if model.startswith("grok") else "openai"
    family = "grok-imagine" if source == "xai" else "gpt-image-2"
    route = source + "_official"
    cap = load_model_capability_profile("image", family, model, route_profile=route, api_form=api_form)
    manifest = reference.build_image_matrix_candidate(model=model, api_form=api_form, capability=cap)
    path = tmp_path / (model + "." + api_form + ".json")
    path.write_text(json.dumps(manifest))
    admitted = reference.load_image_matrix_candidate(str(path), model=model, family=family, route_profile=route,
                                                     api_form=api_form, endpoint=manifest["endpoint"], capability=cap)
    return path, manifest, admitted, cap


def _args(candidate, *extra):
    path, manifest, _, _ = candidate
    transport = {"openai_responses": "openai-responses-image", "openai_images_edits": "images-edits"}.get(manifest["api_form"], "images-generations")
    return ["--base-url", manifest["endpoint"], "--model", manifest["model"], "--family", manifest["family"],
            "--route-profile", manifest["route_profile"], "--transport", transport,
            "--reference-candidate", str(path), "--suite", "smoke", *extra]


def _image(width=1024, height=1024):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "blue").save(out, format="PNG")
    return inspect_image_bytes(out.getvalue(), visual_forensics=False)


def _responses_payload():
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(out, format="PNG")
    return {"object": "response", "id": "resp_offline_reference", "model": responses.MAINLINE_MODEL,
            "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "image_generation_call", "id": "ig_offline", "status": "completed",
                        "quality": "low", "size": "1024x1024", "result": base64.b64encode(out.getvalue()).decode()}],
            "usage": {"input_tokens": 100, "output_tokens": 35, "total_tokens": 135,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 10}}}


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {"content-type": "application/json", "x-request-id": "req_offline_reference"}
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size=65536):
        yield self.payload


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://gateway.example/v1/images/generations"),
    ("source_id", "xai"), ("case_definitions_sha256", "0" * 64),
    ("profile_id", "image/openai/gpt-image-2/unreviewed"),
])
def test_manifest_cannot_change_endpoint_identity_or_factory(tmp_path, field, value):
    path, manifest, _, cap = _candidate(tmp_path)
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        reference.load_image_matrix_candidate(str(path), model="gpt-image-2", family="gpt-image-2",
                                              route_profile="openai_official", api_form="openai_images_generations",
                                              endpoint="https://api.openai.com/v1/images/generations", capability=cap)


def test_default_disabled_policy_requires_explicit_candidate(tmp_path, monkeypatch, capsys):
    candidate = _candidate(tmp_path)
    cap = copy.deepcopy(candidate[3])
    cap.update(parameter_test_enabled=False, test_policy_parameter_test_enabled=False)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setattr(cli, "load_model_capability_profile", lambda *a, **kw: cap)
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("dry run must not open a session"))
    assert cli.main(_args(candidate, "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["model_capability_profile"]["parameter_test_enabled"] is False
    args = _args(candidate, "--dry-run")
    index = args.index("--reference-candidate")
    del args[index:index + 2]
    with pytest.raises(SystemExit):
        cli.main(args)


def test_cli_preserves_reviewed_quality_prompt_and_case_subset(tmp_path, monkeypatch, capsys):
    candidate = _candidate(tmp_path)
    chosen = reference.image_matrix_cases("gpt-image-2", "openai_images_generations")[1]
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setattr(cli, "apply_capability_expectations", lambda *a, **kw: pytest.fail("candidate cannot inherit ordinary expectations"))
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("dry run must not open a session"))
    assert cli.main(_args(candidate, "--suite", "full", "--case", chosen.name, "--quality", "high",
                          "--prompt", "unreviewed replacement prompt", "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"] == [chosen.public()]
    assert plan["cases"][0]["parameters"]["quality"] == "medium"
    assert plan["prompt_sha256"] == hashlib.sha256(chosen.metadata["prompt"].encode()).hexdigest()


def test_observation_requires_pixels_and_parameter_attributed_rejection():
    case = reference.image_matrix_cases("gpt-image-2", "openai_images_generations")[0]
    wrong = reference.evaluate_matrix_observation(case, status_code=200, images=[_image(32, 32)], usage={}, latency_ms=1, error=None)
    assert wrong["diagnostic_pass"] is False
    assert wrong["dimension_validation_pass"] is False
    for error, expected in (({"message": "Invalid size"}, True), ({"message": "billing quota rejected request"}, False)):
        row = reference.evaluate_matrix_observation(case, status_code=400, images=[], usage={}, latency_ms=1, error=error)
        assert row["diagnostic_pass"] is expected
        assert row["pass"] is False
    assert reference.evaluate_matrix_observation(case, status_code=403, images=[], usage={}, latency_ms=1,
                                                 error={"message": "size access denied"})["diagnostic_pass"] is False


@pytest.mark.parametrize("cost,expected", [(12, True), (None, True), (-1, False), (True, False)])
def test_grok_parameter_observation_does_not_invent_token_counts(cost, expected):
    case = reference.image_matrix_cases("grok-imagine-image", "openai_images_generations")[0]
    payload = {"usage": {"cost_in_usd_ticks": cost}} if cost is not None else {}
    row = reference.evaluate_matrix_observation(case, status_code=200, images=[_image()], usage=payload.get("usage", {}),
                                                latency_ms=1, error=None)
    row.update(token_validation_pass=False, model_identity_audit={"status": "unverifiable"})
    reference.finalize_matrix_observation(case, row, payload=payload, protocol_failures=[])
    assert row["diagnostic_pass"] is expected
    assert row["token_validation_pass"] is False
    assert row["returned_image_model_status"] == "unreported"
    assert row["token_accuracy_pass"] is None
    assert row["pass"] is row["compatibility_pass"] is row["certified_route_contract_pass"] is False


def test_generic_candidate_execution_uses_existing_sender_and_persists_manifest(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path, "grok-imagine-image")
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setenv("IMAGE_TEST_API_KEY", "offline-not-a-real-key")
    monkeypatch.setattr(cli, "_list_models", lambda *a: {"status_code": 200, "model_ids": ["grok-imagine-image"]})
    called = []
    def sender(*args, **kwargs):
        case = args[4]
        called.append(case)
        return {"case": case.name, "status": "observed_acceptance", "diagnostic_pass": True, "pass": False,
                "overall_pass": False, "compatibility_pass": False, "status_code": 200, "metadata": case.metadata,
                "actual_images": [], "failures": [], "token_validation_pass": False}
    monkeypatch.setattr(cli, "run_case", sender)
    out = tmp_path / "report"
    assert cli.main(_args(candidate, "--output-dir", str(out), "--max-generation-requests", "1")) == 0
    assert len(called) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert summary["diagnostic_pass"] is True
    assert summary["pass_count"] == 0
    assert summary["pass"] is summary["compatibility_pass"] is False
    assert (out / "reference_candidate.json").read_bytes() == candidate[0].read_bytes()


def test_responses_new_package_keeps_legacy_schema_and_fails_tampering(tmp_path):
    candidate = _candidate(tmp_path, "gpt-image-2.5-sunburst", "openai_responses")
    legacy = responses.build_package()
    assert legacy["schema_version"] == 1 and len(legacy["cases"]) == 96 and legacy["request_cap"] == 108
    cases = reference.image_matrix_cases(candidate[1]["model"], "openai_responses")
    package = responses.build_matrix_package(candidate[1]["model"], [case.name for case in cases[:2]], candidate[2], max_generation_requests=1)
    responses.validate_package(package)
    assert package["schema_version"] == 2 and package["request_cap"] == 1
    altered = copy.deepcopy(package)
    altered["cases"][0]["parameters"]["model"] = "gpt-image-2.5-flare"
    with pytest.raises(ValueError):
        responses.validate_package(altered)
    assert responses.build_package() == legacy


def test_responses_candidate_dry_run_routes_existing_cli_without_credentials(tmp_path, monkeypatch, capsys):
    candidate = _candidate(tmp_path, "gpt-image-2.5-sunburst", "openai_responses")
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setattr(responses, "designated_credential", lambda: pytest.fail("dry run must not load key"))
    monkeypatch.setattr(responses.requests, "Session", lambda: pytest.fail("dry run must not create session"))
    assert cli.main(_args(candidate, "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_mode"] == "reference_observation" and plan["certified"] is False
    assert len(plan["cases"]) == 1
    assert plan["cases"][0]["parameters"]["tools"][0]["model"] == candidate[1]["model"]
    with pytest.raises(ValueError, match="retry"):
        cli.main(_args(candidate, "--transient-retries", "1", "--dry-run"))


def test_grok_actual_request_evidence_keeps_resolution_and_aspect():
    body = {"model": "grok-imagine-image", "resolution": "2k", "aspect_ratio": "16:9", "quality": "low"}
    assert cli._effective_request_parameters(body, "images-generations") == body


def test_gpt_usage_anomaly_does_not_become_parameter_rejection():
    case = reference.image_matrix_cases("gpt-image-1.5", "openai_images_generations")[0]
    row = reference.evaluate_matrix_observation(case, status_code=200, images=[_image()], usage={}, latency_ms=1, error=None)
    row.update(token_validation_pass=False, token_validation_failures=["unexplained_reported_text_output"],
               model_identity_audit={"status": "unverifiable"})
    reference.finalize_matrix_observation(case, row, payload={}, protocol_failures=[])
    assert row["diagnostic_pass"] is True
    assert row["status"] == "observed_acceptance"
    assert row["usage_validation_pass"] is False
    assert row["token_validation_failures"] == ["unexplained_reported_text_output"]


def test_grok_run_case_retains_cost_and_full_wire_digest(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(image, format="PNG")
    payload = {"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}],
               "usage": {"cost_in_usd_ticks": 200_000_000}}
    session = MagicMock()
    session.post.return_value = SimpleNamespace(status_code=200, headers={"x-request-id": "req_offline_cost"},
                                                json=lambda: payload, text=json.dumps(payload))
    case = reference.image_matrix_cases("grok-imagine-image", "openai_images_generations")[0]
    images = tmp_path / "images"
    images.mkdir()
    row = cli.run_case(session, "https://api.x.ai/v1/images/generations", "grok-imagine-image", "ignored",
                       case, timeout=30, images_dir=images, visual_forensics=False, config={})
    request = session.post.call_args.kwargs["json"]
    assert request == case.request_body("grok-imagine-image", case.metadata["prompt"])
    assert row["request_sha256"] == reference.canonical_digest(request)
    assert row["response_sha256"] == reference.canonical_digest(payload)
    assert row["diagnostic_pass"] is True
    assert row["cost_usage_audit"]["pass"] is True
    assert row["token_validation_pass"] is False
    assert row["effective_request_parameters"]["resolution"] == "1k"


def test_responses_candidate_uses_existing_execute_and_immutable_report_evidence(tmp_path, monkeypatch, capsys):
    candidate = _candidate(tmp_path, "gpt-image-2.5-sunburst", "openai_responses")
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setattr(responses, "REPORT_ROOT", tmp_path / "batches")
    monkeypatch.setenv("IMAGE_TEST_API_KEY", "offline-test-reference-secret")
    fake = MagicMock()
    fake.__enter__.return_value = fake
    payload = _responses_payload()
    payload["usage"]["total_tokens"] = 999  # Preserve an independent accounting anomaly.
    fake.request.return_value = _FakeResponse(payload)
    monkeypatch.setattr(responses.requests, "Session", lambda: fake)
    out = tmp_path / "response-report"
    assert cli.main(_args(candidate, "--output-dir", str(out))) == 0
    summary = json.loads((out / "summary.json").read_text())
    rows = json.loads((out / "case_results.json").read_text())
    plan = json.loads((out / "plan.json").read_text())
    for field in ("model", "api_form", "source_id", "endpoint", "route_profile", "execution_mode", "profile_id", "interface_id"):
        assert plan[field] == summary[field] == candidate[1][field]
    assert rows[0]["api_form"] == "openai_responses"
    assert len(rows) == fake.request.call_count == 1
    assert summary["diagnostic_pass"] is True and summary["compatibility_pass"] is False
    assert rows[0]["token_validation_pass"] is False
    assert rows[0]["request_sha256"] == reference.canonical_digest(json.loads(fake.request.call_args.kwargs["data"]))
    assert rows[0]["response_sha256"] == reference.canonical_digest(payload)
    assert rows[0]["request_started_at"]
    assert (out / rows[0]["artifacts"][0]).is_file()
    assert (out / "reference_candidate.json").read_bytes() == candidate[0].read_bytes()
    from scripts.workflow_test import _owned_ledger
    report = json.loads((out / "workflow_report.json").read_text())
    assert report["plan_digest"] == plan["execution_plan"]["plan_digest"]
    before = fake.request.call_count
    for run in report["runs"]:
        raw = _owned_ledger(Path(run["ledger_path"]), {"execution_plan": plan["execution_plan"]}, run["run_id"])
        assert json.loads(raw)["run_id"] == run["run_id"]
    assert fake.request.call_count == before


def test_responses_candidate_budget_stops_at_exact_outbound_count(tmp_path, monkeypatch):
    from lib.credential_security import ProviderCredential

    candidate = _candidate(tmp_path, "gpt-image-2.5-sunburst", "openai_responses")
    cases = reference.image_matrix_cases(candidate[1]["model"], "openai_responses")[:2]
    package = responses.build_matrix_package(candidate[1]["model"], [case.name for case in cases], candidate[2], max_generation_requests=1)
    monkeypatch.setattr(responses, "REPORT_ROOT", tmp_path / "batches")
    batch = responses.prepare(package=package)
    fake = MagicMock()
    fake.__enter__.return_value = fake
    fake.post.return_value = _FakeResponse(_responses_payload())
    monkeypatch.setattr(responses.requests, "Session", lambda: fake)
    credential = ProviderCredential.create(provider="offline", secret="offline-reference-key", base_urls=[responses.ENDPOINT])
    summary = responses.execute(package, batch, credential=credential)
    assert fake.post.call_count == 1
    assert summary["recorded_cases"] == 1 and summary["planned_cases"] == 2
    assert summary["budget_exhausted"] is True and summary["diagnostic_pass"] is False


@pytest.mark.parametrize("api_form", ["openai_images_generations", "openai_images_edits"])
def test_gpt25_candidate_selects_exact_factory_for_images_and_edits(tmp_path, monkeypatch, capsys, api_form):
    candidate = _candidate(tmp_path, "gpt-image-2.5-flare", api_form)
    chosen = reference.image_matrix_cases(candidate[1]["model"], api_form)[1]
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("dry run must not create session"))
    assert cli.main(_args(candidate, "--suite", "full", "--case", chosen.name, "--quality", "max", "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"] == [chosen.public()]
    assert plan["reference_candidate"]["case_definitions_sha256"] == candidate[1]["case_definitions_sha256"]


def test_candidate_manifest_mutation_after_admission_stops_before_http(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_job_spec", lambda _: None)
    monkeypatch.setenv("IMAGE_TEST_API_KEY", "offline-not-real-key")
    original_load = cli.load_image_matrix_candidate
    def mutated(*args, **kwargs):
        admitted = original_load(*args, **kwargs)
        candidate[0].write_text(candidate[0].read_text() + "\n")
        return admitted
    monkeypatch.setattr(cli, "load_image_matrix_candidate", mutated)
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("changed manifest must not create session"))
    with pytest.raises(ValueError, match="changed after admission"):
        cli.main(_args(candidate, "--output-dir", str(tmp_path / "report")))


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "grok-imagine-image"])
def test_positive_candidate_attributed_rejection_remains_observation(tmp_path, model):
    case = reference.image_matrix_cases(model, "openai_images_generations")[0]
    field = "size" if model.startswith("gpt") else "resolution"
    payload = {"error": {"param": field, "message": field + " is not supported"}}
    session = MagicMock()
    session.post.return_value = SimpleNamespace(status_code=400, headers={}, json=lambda: payload, text=json.dumps(payload))
    row = cli.run_case(session, reference.matrix_identity(model, "openai_images_generations")["endpoint"], model, "ignored", case,
                       timeout=30, images_dir=tmp_path, visual_forensics=False, config={})
    assert row["status"] == "observed_parameter_rejection"
    assert row["diagnostic_pass"] is True
    assert row["documented_outcome_match"] is False
    assert row["pass"] is row["compatibility_pass"] is False


def test_candidate_responses_auto_records_non_grid_pixels_without_geometry_certification(tmp_path):
    candidate = _candidate(tmp_path, "gpt-image-2.5-flare", "openai_responses")
    cases = reference.image_matrix_cases(candidate[1]["model"], "openai_responses")
    selected = next(case for case in cases if case.parameters["tools"][0]["size"] == "auto"
                    and case.parameters["tools"][0]["quality"] == "low")
    package = responses.build_matrix_package(candidate[1]["model"], [selected.name], candidate[2])
    payload = _responses_payload()
    img = io.BytesIO()
    Image.new("RGB", (1312, 1199), "blue").save(img, format="PNG")
    payload["output"][0].update(size="1312x1199", result=base64.b64encode(img.getvalue()).decode())
    verdict = responses._evaluate_package_case(package, package["cases"][0], 200, payload, tmp_path / "auto-artifacts")
    assert verdict["diagnostic_pass"] is True
    assert verdict["auto_output_geometry_certified"] is False
    assert verdict["auto_output_size_validation"] == "completed_decoded_image"
    assert verdict["artifacts"][0]["width"] == 1312
    assert verdict["artifacts"][0]["height"] == 1199
    assert verdict["pass"] is verdict["certified_route_contract_pass"] is False


def test_candidate_edits_wire_parameters_and_fixture_digest_are_pinned(tmp_path):
    from lib.gpt_image_25 import edit_fixtures, fixture_manifest
    case = reference.image_matrix_cases("gpt-image-2.5-flare", "openai_images_edits")[0]
    img = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(img, format="PNG")
    payload = {"data": [{"b64_json": base64.b64encode(img.getvalue()).decode()}]}
    session = MagicMock()
    session.post.return_value = SimpleNamespace(status_code=200, headers={}, json=lambda: payload, text=json.dumps(payload))
    row = cli.run_case(session, case.metadata["endpoint"], "gpt-image-2.5-flare", "ignored", case,
                       timeout=30, images_dir=tmp_path, visual_forensics=False, config={}, transport="images-edits")
    body = case.request_body("gpt-image-2.5-flare", case.metadata["prompt"])
    assert session.post.call_args.kwargs["data"] == {key: str(value) for key, value in body.items()}
    assert session.post.call_args.kwargs["files"] == edit_fixtures(case)
    assert row["input_fixtures"] == fixture_manifest(case)
    assert row["request_sha256"] == reference.canonical_digest({**body, "image": fixture_manifest(case)})
    assert row["diagnostic_pass"] is True


@pytest.mark.parametrize("dimensions,accepted", [((1312, 1199), True), ((1254, 1254), True), ((32, 32), False), ((4096, 2048), False)])
def test_candidate_auto_geometry_uses_project_sanity_without_grid(dimensions, accepted):
    case = next(case for case in reference.image_matrix_cases("gpt-image-2", "openai_images_generations")
                if case.parameters["size"] == "auto")
    row = reference.evaluate_matrix_observation(case, status_code=200, images=[_image(*dimensions)],
                                                usage={}, latency_ms=1, error=None)
    assert row["diagnostic_pass"] is accepted
    assert row["geometry_check"]["scope"] == "decoded_auto_output_with_project_sanity_only"
    assert row["auto_output_geometry_certified"] is False
