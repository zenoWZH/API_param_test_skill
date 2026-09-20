"""Offline cache consumer tests against compiled shared source definitions.

Only test setup reads the public source author. Runtime must operate entirely
on an immutable MPDB snapshot and freshly frozen requests, without raw reports.
"""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from lib import anthropic_cache_reference as cache
from lib import model_profile_catalog as mpdb
from model_profile_db import Catalog, compile_catalog, compose_catalog


ROOT = Path(__file__).resolve().parents[2]
NONCES = {"positive": "1" * 32, "negative": "2" * 32}


@pytest.fixture(scope="module")
def author():
    spec = importlib.util.spec_from_file_location("shared_cache_suite_test_oracle",
        ROOT / "scripts/approved_anthropic_cache_fixed_suites.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shared(author):
    return author.load_manifest()


@pytest.fixture(scope="module")
def candidate_catalog(author, shared):
    # Exercise actual compiler/composer and App snapshot production in memory;
    # this also works before the separately approved catalog installation.
    data = ROOT / "packages/model-profile-db/model_profile_db/data"
    source = yaml.load((data / "catalog.yaml").read_text(), Loader=yaml.CSafeLoader)
    extension = yaml.load((data / "test_extensions.yaml").read_text(), Loader=yaml.CSafeLoader)
    binding = extension["test_bindings"]["parameter/" + author.CONTRACT]
    for model in author.MODELS:
        suite = shared["suites"][author.suite_id(model)]
        binding.setdefault("fixed_case_suites", {})[suite["suite_id"]] = copy.deepcopy(suite)
        interface = source["profiles"][author.identity(model)["profile_id"]]["interfaces"]["anthropic-messages-default"]
        interface.setdefault("source_conflicts", {})[author.MANIFEST_KEY] = author.manifest_entry(suite)
    return Catalog(compose_catalog(compile_catalog(source), extension))


@pytest.fixture(scope="module")
def snapshots(author, candidate_catalog):
    catalog = candidate_catalog
    result = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mpdb, "get_model_profile_catalog", lambda: catalog)
        for model in author.MODELS:
            exact = author.identity(model)
            result[model] = mpdb.database_snapshot({**mpdb.catalog_metadata(),
                "source_id": "anthropic", "suite_family_id": "claude", "canonical_family_id": "claude",
                "profile_id": exact["profile_id"], "interface_id": exact["interface_id"], "catalog_resolved": True,
                "profile": catalog.get_profile(exact["profile_id"]), "interface": catalog.get_interface(exact["interface_id"]),
                "binding_source": "catalog_official_reference", "execution_target": {
                    "provider_id": "anthropic_official", "request_model_id": model,
                    "route_profile": "vendor_direct", "api_form": cache.API_FORM, "transport": cache.TRANSPORT}})
    return result


@pytest.fixture
def snapshot(snapshots):
    return copy.deepcopy(snapshots[cache.MODELS[0]])


def _plan(snapshot):
    # The original regression corpus deliberately exercises frozen schema v1.
    from lib import anthropic_cache_legacy_20260908 as legacy
    frozen = legacy.build_cache_plan(snapshot, suite_id=legacy.suite_id(snapshot["model_slug"]),
        create_runtime_nonce=True, nonce_factory=Mock(side_effect=list(NONCES.values())))
    return cache.build_cache_plan(snapshot, suite_id=cache.suite_id(snapshot["model_slug"]),
        frozen_plan=frozen.frozen_payload)


@pytest.fixture
def plan(snapshot):
    return _plan(snapshot)


def _redigest(value, key="snapshot_digest"):
    value[key] = cache.digest({k: v for k, v in value.items() if k != key})


def _native(plan, *, read=0, write=None, total=None, text=None):
    suite = plan.snapshot["parameter_test_binding"]["fixed_case_suites"][plan.suite_id]
    minimum = suite["minimum_prefix_tokens"]
    write = minimum if write is None else write
    total = minimum + 100 if total is None else total
    return {"id": "msg_offline_cache", "type": "message", "role": "assistant", "model": plan.model,
        "stop_reason": "end_turn", "stop_sequence": None,
        "content": [{"type": "text", "text": text if text is not None else
            "The library is open for eight hours." if plan.model == "claude-opus-5" else "CACHE_PROBE_OK"}],
        "usage": {"input_tokens": total - read - write, "output_tokens": 9,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write, "inference_geo": "us"}}


def _record(plan, index, payload):
    request = plan.requests[index]
    raw = cache.canonical_bytes(payload)
    return {"case_id": request.case_id, "request_body_sha256": request.body_sha256,
        "request_kind": request.kind, "request_endpoint": request.endpoint, "fixed_plan_digest": plan.plan_digest,
        "status_code": 200, "response_complete": True, "client_entered": True, "response_raw": raw,
        "response_sha256_before_redaction": hashlib.sha256(raw).hexdigest()}


def _records(plan):
    minimum = plan.snapshot["parameter_test_binding"]["fixed_case_suites"][plan.suite_id]["minimum_prefix_tokens"]
    if plan.schema_version == 2:
        return [_record(plan, 0, _native(plan)), _record(plan, 1, _native(plan, read=minimum, write=0)),
            _record(plan, 2, _native(plan))]
    return [_record(plan, 0, {"input_tokens": minimum + 64}), _record(plan, 1, {"input_tokens": minimum + 65}),
        _record(plan, 2, _native(plan)), _record(plan, 3, _native(plan, read=minimum, write=0)),
        _record(plan, 4, _native(plan))]


# Public fixture helpers for transport/CLI/Web integration tests in this App.
records_for = _records


def _change_response(records, index, change):
    payload = json.loads(records[index]["response_raw"])
    change(payload)
    raw = cache.canonical_bytes(payload)
    records[index].update(response_raw=raw, response_sha256_before_redaction=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("model", cache.MODELS)
def test_all_nine_compiled_suites_match_shared_renderer_exactly(snapshots, shared, author, model):
    snapshot = snapshots[model]
    factory = Mock(side_effect=list(NONCES.values()))
    plan = cache.build_cache_plan(snapshot, suite_id=cache.suite_id(model), create_runtime_nonce=True, nonce_factory=factory)
    assert [c.args for c in factory.call_args_list] == [(16,), (16,)]
    oracle = author.render_suite_cases(shared["suites"][plan.suite_id], NONCES)[2:]
    assert len(oracle) == len(plan.requests) == plan.request_cap == 3
    for request, expected in zip(plan.requests, oracle):
        assert request.body == expected["body"]
        assert request.body_bytes == author.canonical_bytes(expected["body"])
        assert request.body_sha256 == expected["body_sha256"]
        assert (request.case_id, request.kind, request.endpoint) == (
            expected["case_id"], expected["kind"], expected["endpoint"])
        assert request.expectation == {"kind": "cache_hit_expectation", "expected_hit": request.label == "repeat"}
    assert plan.requests[0].body_bytes == plan.requests[1].body_bytes
    assert plan.requests[0].body["system"] != plan.requests[2].body["system"]
    assert all(r.body["max_tokens"] == 2048 and r.body["stream"] is False for r in plan.requests)
    assert cache.evaluate_cache_plan(plan, _records(plan))["cache_effect_verified"] is True


def test_description_is_zero_randomness_and_cannot_supply_a_wire(snapshot, monkeypatch):
    random = Mock(side_effect=AssertionError("preview must not create a nonce"))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    description = cache.build_cache_plan(snapshot, suite_id=cache.suite_id(snapshot["model_slug"]))
    assert type(description) is cache.CacheSuiteDescription
    for request in description.requests:
        for field in ("body", "body_bytes", "body_sha256"):
            with pytest.raises(ValueError):
                getattr(request, field)
    with pytest.raises(ValueError):
        cache.next_cache_request(description, [])
    random.assert_not_called()


def test_real_creation_calls_token_hex_twice_but_recovery_and_evaluation_never_do(snapshot, monkeypatch):
    random = Mock(side_effect=list(NONCES.values()))
    monkeypatch.setattr(cache.secrets, "token_hex", random)
    plan = cache.build_cache_plan(snapshot, suite_id=cache.suite_id(snapshot["model_slug"]), create_runtime_nonce=True)
    assert [c.args for c in random.call_args_list] == [(16,), (16,)]
    random.side_effect = AssertionError("frozen recovery must not create a nonce")
    restored = cache.build_cache_plan(snapshot, suite_id=plan.suite_id, frozen_plan=plan.frozen_payload)
    assert restored.frozen_payload == plan.frozen_payload
    assert [r.body_bytes for r in restored.requests] == [r.body_bytes for r in plan.requests]
    assert cache.evaluate_cache_plan(restored, _records(restored))["pass"] is True
    assert random.call_count == 2


def test_runtime_does_not_read_catalog_public_references_or_reports(snapshot, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", Mock(side_effect=AssertionError("runtime filesystem read")))
        patch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("runtime filesystem read")))
        patch.setattr(mpdb, "get_model_profile_catalog", Mock(side_effect=AssertionError("runtime catalog reload")))
        plan = _plan(snapshot)
        assert cache.evaluate_cache_plan(plan, _records(plan))["pass"] is True


def test_payload_and_snapshot_views_cannot_mutate_the_frozen_job(plan, snapshot):
    original = copy.deepcopy(snapshot)
    plan.frozen_payload["requests"][2]["body"]["max_tokens"] = 1
    plan.snapshot["source_id"] = "foreign"
    request = plan.requests[2]
    request.body["max_tokens"] = 1
    request.expectation["semantic_requirement"] = "changed"
    assert plan.requests[2].body["max_tokens"] == 2048
    assert plan.requests[2].expectation != request.expectation
    assert snapshot == original
    with pytest.raises(FrozenInstanceError):
        plan.endpoint = "https://example.invalid"


@pytest.mark.parametrize("values", [
    ["1" * 32, "1" * 32], ["0" * 32, "2" * 32], ["1" * 32, "0" * 32],
    ["a" * 31, "2" * 32], ["A" * 32, "2" * 32], ["g" * 32, "2" * 32], [None, "2" * 32],
])
def test_creation_rejects_nonfresh_or_malformed_nonces(snapshot, values):
    with pytest.raises(ValueError):
        cache.build_cache_plan(snapshot, suite_id=cache.suite_id(snapshot["model_slug"]),
            create_runtime_nonce=True, nonce_factory=Mock(side_effect=values))


def test_historical_nonce_hash_is_rejected_by_renderer(shared):
    suite = copy.deepcopy(shared["suites"][cache.SUITE_IDS[0]])
    suite["nonce_policy"]["historical_nonce_sha256"].append(hashlib.sha256(NONCES["positive"].encode()).hexdigest())
    with pytest.raises(ValueError):
        cache._render(suite, NONCES)


@pytest.mark.parametrize("mutation", ["body", "cap", "stream", "endpoint", "kind", "expectation", "order", "missing",
    "duplicate", "nonce", "same_nonce", "placeholder", "retention", "request_cap", "snapshot_digest", "schema_bool", "extra"])
def test_even_rehashed_frozen_plan_tampering_is_rejected(plan, mutation):
    value = plan.frozen_payload
    request = value["requests"][2]
    if mutation == "body": request["body"]["messages"][0]["content"] = "changed"
    elif mutation == "cap": request["body"]["max_tokens"] = 1
    elif mutation == "stream": request["body"]["stream"] = True
    elif mutation == "endpoint": request["endpoint"] = cache.COUNT_ENDPOINT
    elif mutation == "kind": request["kind"] = "count"
    elif mutation == "expectation": request["expectation"] = {"supported": True}
    elif mutation == "order": value["requests"][0], value["requests"][1] = value["requests"][1], value["requests"][0]
    elif mutation == "missing": value["requests"].pop()
    elif mutation == "duplicate": value["requests"].append(copy.deepcopy(request))
    elif mutation == "nonce": value["nonces"]["positive"] = "3" * 32
    elif mutation == "same_nonce": value["nonces"]["positive"] = value["nonces"]["negative"]
    elif mutation == "placeholder": value["nonces"]["negative"] = "0" * 32
    elif mutation == "retention": value["report_retention"] = "P1Y"
    elif mutation == "request_cap": value["request_cap"] = 6
    elif mutation == "snapshot_digest": value["snapshot_digest"] = "0" * 64
    elif mutation == "schema_bool": value["schema_version"] = True
    elif mutation == "extra": value["retry"] = 1
    request["body_sha256"] = cache.digest(request["body"])
    _redigest(value, "plan_digest")
    with pytest.raises(ValueError):
        cache.build_cache_plan(plan.snapshot, suite_id=plan.suite_id, frozen_plan=value)


@pytest.mark.parametrize("mutation", ["source", "family", "canonical", "owner", "request_model", "provider", "route", "form",
    "transport", "version", "path", "contract_source", "policy_source", "parameter_source", "embedded_policy",
    "suite_body", "suite_endpoint", "suite_kind", "manifest", "manifest_scope", "generic_matrix", "override", "excluded",
    "unsupported", "thinking", "cap", "schema_bool", "digest"])
def test_rehashed_snapshot_cannot_cross_source_manifest_or_binding_scope(snapshot, mutation):
    p, i, c, policy, parameter = [snapshot[k] for k in ("profile", "interface", "reference_contract", "test_binding", "parameter_test_binding")]
    suite = parameter["fixed_case_suites"][cache.suite_id(snapshot["model_slug"])]
    manifest = i["source_conflicts"]["fixed_cache_suite_manifest_20260908"]
    if mutation == "source": snapshot["source_id"] = "aws_bedrock"
    elif mutation == "family": p["family_id"] = "foreign"
    elif mutation == "canonical": p["canonical_model_id"] = "claude/claude-opus-5"
    elif mutation == "owner": p["interface_ids"] = []
    elif mutation == "request_model": snapshot["execution_target"]["request_model_id"] = "claude-haiku-4-5"
    elif mutation == "provider": snapshot["execution_target"]["provider_id"] = "proxy"
    elif mutation == "route": snapshot["execution_target"]["route_profile"] = "dynamic_aggregator"
    elif mutation == "form": snapshot["execution_target"]["api_form"] = "openai_chat_completions"
    elif mutation == "transport": snapshot["execution_target"]["transport"] = "chat_completions"
    elif mutation == "version": i["default_api_version"] = "2026-01-01"
    elif mutation == "path": i["api_versions"] = {cache.API_VERSION: {"path_template": "/messages"}}
    elif mutation == "contract_source": c["source_ids"] = ["aws_bedrock"]
    elif mutation == "policy_source": policy["source_id"] = "aws_bedrock"
    elif mutation == "parameter_source": parameter["source_id"] = "aws_bedrock"
    elif mutation == "embedded_policy": i["test_bindings"][0]["label"] = "changed"
    elif mutation == "suite_body": suite["request_template"]["thinking"]["type"] = "enabled"
    elif mutation == "suite_endpoint": suite["case_definitions"][0]["endpoint_constraint"]["url"] = cache.ENDPOINT
    elif mutation == "suite_kind": suite["case_definitions"][0]["kind"] = "generation"
    elif mutation == "manifest": manifest["suite_artifact"]["sha256"] = "0" * 64
    elif mutation == "manifest_scope": manifest["request_model_id"] = "claude-opus-5"
    elif mutation == "generic_matrix": parameter["test_cases"].append(suite["case_definitions"][0]["case_id"])
    elif mutation == "override": parameter["expectations"] = {suite["case_definitions"][0]["case_id"]: "unsupported"}
    elif mutation == "excluded": parameter["excluded_test_profiles"] = [suite["case_definitions"][0]["case_id"]]
    elif mutation == "unsupported": i.setdefault("parameter_capabilities", {})["cache_control"] = {"state": "unsupported"}
    elif mutation == "thinking": i.setdefault("parameter_constraints", {})["thinking.type"] = {"allowed_values": ["enabled"]}
    elif mutation == "cap": i.setdefault("parameter_constraints", {})["max_tokens"] = {"inclusive_maximum": 1024}
    elif mutation == "schema_bool": snapshot["snapshot_schema_version"] = True
    _redigest(snapshot)
    if mutation == "digest": snapshot["snapshot_digest"] = "0" * 64
    with pytest.raises(ValueError):
        _plan(snapshot)


@pytest.mark.parametrize("record_name", ["profile", "interface", "reference_contract", "test_binding", "parameter_test_binding"])
@pytest.mark.parametrize("gate,value", [("enabled", False), ("executable", False), ("runner_enabled", False),
    ("parameter_test_enabled", False), ("enabled", 1), ("disabled_reason", "revoked")])
def test_every_execution_owner_can_close_the_suite(snapshot, record_name, gate, value):
    snapshot[record_name][gate] = value
    if record_name == "test_binding":
        for embedded in snapshot["interface"]["test_bindings"]:
            if embedded["test_binding_id"] == snapshot[record_name]["test_binding_id"]:
                embedded[gate] = value
    _redigest(snapshot)
    with pytest.raises(ValueError):
        _plan(snapshot)


@pytest.mark.parametrize("options", [
    {"endpoint": cache.COUNT_ENDPOINT}, {"endpoint": cache.ENDPOINT + "?x=1"},
    {"endpoint": "https://api.anthropic.com.evil.invalid/v1/messages"}, {"endpoint": "http://api.anthropic.com/v1/messages"},
    {"runs": True}, {"runs": 1.0}, {"runs": 0}, {"runs": 2}, {"job_type": "cache_suite"},
    {"job_type": "quick_load"}, {"create_runtime_nonce": 1}, {"suite_id": "unknown"},
])
def test_only_one_explicit_parameter_job_is_available(snapshot, options):
    with pytest.raises(ValueError):
        cache.build_cache_plan(snapshot, **{"suite_id": cache.suite_id(snapshot["model_slug"]), **options})


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("count", [None, True, 3.0, -1, 4096 + 63, 16385])
def test_both_estimates_must_qualify_before_any_generation(plan, index, count):
    records = _records(plan)[:index + 1]
    _change_response(records, index, lambda p: p.update(input_tokens=count))
    summary = cache.evaluate_cache_plan(plan, records)
    assert not summary["pass"] and not summary["case_results"][index]["pass"]
    assert cache.next_cache_request(plan, records) is None


@pytest.mark.parametrize("count", [4160, 16384])
def test_count_qualification_includes_both_documented_boundaries(plan, count):
    records = [_record(plan, index, {"input_tokens": count}) for index in range(2)]
    assert cache.next_cache_request(plan, records).label == "cold"
    result = cache.evaluate_cache_plan(plan, records)
    assert all(row["qualifies_for_generation"] for row in result["case_results"])
    assert not result["token_exact_proof"] and not result["cache_effect_verified"]


def test_sequential_selection_never_skips_the_second_count_and_ends_at_five(plan):
    records = _records(plan)
    for length in range(5):
        assert cache.next_cache_request(plan, records[:length]).case_id == plan.requests[length].case_id
        assert not cache.evaluate_cache_plan(plan, records[:length])["cache_effect_verified"]
    assert cache.next_cache_request(plan, records) is None
    summary = cache.evaluate_cache_plan(plan, records)
    assert summary["pass"] and summary["count_precision"] == "official_estimate"
    assert summary["official_input_estimates"] == [4160, 4161]
    assert not any(summary[k] for k in ("token_exact_proof", "full_parameter_matrix_verified", "physical_region_verified",
        "provider_ttl_expiry_verified", "historical_pass_reused"))
    with pytest.raises(ValueError):
        cache.evaluate_cache_plan(plan, records + [records[-1]])


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("value", [None, True, -1, 1.0])
def test_native_counters_cannot_be_missing_coerced_or_replaced_with_zero(plan, field, value):
    records = _records(plan)
    def change(p):
        if value is None: p["usage"].pop(field)
        else: p["usage"][field] = value
    _change_response(records, 2, change)
    summary = cache.evaluate_cache_plan(plan, records)
    assert not summary["pass"] and not summary["case_results"][2]["response_valid"]
    assert cache.next_cache_request(plan, records[:3]) is None


@pytest.mark.parametrize("mutation", ["cold_read", "repeat_zero", "negative_read", "cold_write_below_min", "repeat_read_exceeds_write",
    "different_full_input", "different_geo", "one_missing_geo", "invalid_geo", "bad_creation_split", "missing_creation_split_counter"])
def test_cache_effect_needs_all_controls_native_accounting_and_same_reported_geo(plan, mutation):
    records = _records(plan)
    if mutation == "cold_read": index, updates = 2, {"cache_read_input_tokens": 1, "input_tokens": 99}
    elif mutation == "repeat_zero": index, updates = 3, {"cache_read_input_tokens": 0, "input_tokens": 4196}
    elif mutation == "negative_read": index, updates = 4, {"cache_read_input_tokens": 1, "input_tokens": 99}
    elif mutation == "cold_write_below_min": index, updates = 2, {"cache_creation_input_tokens": 4095, "input_tokens": 101}
    elif mutation == "repeat_read_exceeds_write": index, updates = 3, {"cache_read_input_tokens": 4097, "input_tokens": 99}
    elif mutation == "different_full_input": index, updates = 3, {"input_tokens": 101}
    elif mutation == "different_geo": index, updates = 4, {"inference_geo": "eu"}
    elif mutation == "one_missing_geo": index, updates = 4, {"inference_geo": None}
    elif mutation == "invalid_geo": index, updates = 4, {"inference_geo": {"region": "us"}}
    elif mutation == "bad_creation_split": index, updates = 2, {"cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 0}}
    elif mutation == "missing_creation_split_counter": index, updates = 2, {"cache_creation": {"ephemeral_5m_input_tokens": 4096}}
    _change_response(records, index, lambda p: p["usage"].update(updates))
    assert cache.evaluate_cache_plan(plan, records)["cache_effect_verified"] is False


def test_absent_geo_and_consistent_native_creation_split_do_not_invent_physical_region(plan):
    records = _records(plan)
    for index in range(2, 5):
        def change(p):
            p["usage"].pop("inference_geo")
            p["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": p["usage"]["cache_creation_input_tokens"],
                "ephemeral_1h_input_tokens": 0}
        _change_response(records, index, change)
    summary = cache.evaluate_cache_plan(plan, records)
    assert summary["cache_effect_verified"] and summary["reported_inference_geo_consistent"]
    assert summary["physical_region_verified"] is False


@pytest.mark.parametrize("mutation", ["wrong_model", "empty_id", "max_tokens_stop", "tool_stop", "wrong_role", "wrong_type",
    "refusal", "error", "refusal_block", "thinking_block", "tool_block", "empty_text", "empty_content", "no_usage", "zero_output",
    "large_output", "incomplete", "not_entered", "interrupted", "failure", "http400", "raw_hash", "duplicate_json", "broken_json"])
def test_native_refusal_tools_thinking_partial_or_untrusted_bytes_never_pass(plan, mutation):
    records = _records(plan)
    def change(p):
        if mutation == "wrong_model": p["model"] = "claude-opus-5"
        elif mutation == "empty_id": p["id"] = ""
        elif mutation == "max_tokens_stop": p["stop_reason"] = "max_tokens"
        elif mutation == "tool_stop": p["stop_reason"] = "tool_use"
        elif mutation == "wrong_role": p["role"] = "user"
        elif mutation == "wrong_type": p["type"] = "response"
        elif mutation in ("refusal", "error"): p[mutation] = "refused"
        elif mutation.endswith("_block"): p["content"].append({"type": {"refusal_block": "refusal", "thinking_block": "thinking", "tool_block": "tool_use"}[mutation], "text": "hidden"})
        elif mutation == "empty_text": p["content"][0]["text"] = " "
        elif mutation == "empty_content": p["content"] = []
        elif mutation == "no_usage": p.pop("usage")
        elif mutation == "zero_output": p["usage"]["output_tokens"] = 0
        elif mutation == "large_output": p["usage"]["output_tokens"] = 2049
    _change_response(records, 2, change)
    record = records[2]
    changes = {"incomplete": {"response_complete": False}, "not_entered": {"client_entered": False},
        "interrupted": {"interrupted": True}, "failure": {"failure_type": "read_error"}, "http400": {"status_code": 400},
        "raw_hash": {"response_sha256_before_redaction": "0" * 64}}
    record.update(changes.get(mutation, {}))
    if mutation in ("duplicate_json", "broken_json"):
        raw = b'{"input_tokens":1,"input_tokens":2}' if mutation == "duplicate_json" else b'{"type":"message"'
        record.update(response_raw=raw, response_sha256_before_redaction=hashlib.sha256(raw).hexdigest())
    summary = cache.evaluate_cache_plan(plan, records)
    assert not summary["pass"] and not summary["case_results"][2]["response_valid"]
    assert cache.next_cache_request(plan, records[:3]) is None


@pytest.mark.parametrize("text,expected", [("Eight hours.", True), ("8 hours", True), ("Seven hours.", False),
    ("CACHE_PROBE_OK", False), ("18 hours", False), ("80 hours", False),
    ("It is open for seven hours, not eight.", False), ("It is not open for 8 hours.", False)])
def test_opus5_requires_the_selected_library_answer(snapshots, text, expected):
    plan = _plan(snapshots["claude-opus-5"])
    records = _records(plan)
    _change_response(records, 2, lambda p: p["content"][0].update(text=text))
    assert cache.evaluate_cache_plan(plan, records)["cache_effect_verified"] is expected


@pytest.mark.parametrize("field,value", [("case_id", "foreign"), ("request_body_sha256", "0" * 64),
    ("request_kind", "count"), ("request_endpoint", cache.COUNT_ENDPOINT), ("fixed_plan_digest", "0" * 64)])
def test_old_or_foreign_records_cannot_attach_to_a_new_plan(plan, field, value):
    records = _records(plan)
    records[2][field] = value
    with pytest.raises(ValueError):
        cache.evaluate_cache_plan(plan, records)


def test_historical_pass_flags_cannot_make_an_empty_or_failed_new_run_pass(plan):
    empty = cache.evaluate_cache_plan(plan, [])
    assert empty["requests_recorded"] == 0 and not empty["pass"] and not empty["historical_pass_reused"]
    records = _records(plan)
    _change_response(records, 3, lambda p: p["usage"].update(cache_read_input_tokens=0, input_tokens=4196))
    for row in records:
        row.update(pass_=True, historical_pass=True, cache_effect_verified=True)
        row["pass"] = True
    assert cache.evaluate_cache_plan(plan, records)["pass"] is False
