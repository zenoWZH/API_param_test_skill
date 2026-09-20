from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from lib.config import load_config


MODEL = "deepseek-v4-flash"
CONTRACT_ID = "deepseek_chat"
PROFILE_ID = "text/deepseek/deepseek/deepseek-v4-flash-0731"
INTERFACE_ID = f"{PROFILE_ID}#openai-chat-default"
TEST_BINDING_ID = f"interface/{PROFILE_ID}/openai-chat-default"


class _FakeEventHook:
    @staticmethod
    def add_listener(listener: Any) -> Any:
        return listener


def _fake_locust_modules() -> dict[str, ModuleType]:
    locust = ModuleType("locust")
    locust.HttpUser = object  # type: ignore[attr-defined]
    locust.constant = lambda seconds: lambda _user: seconds  # type: ignore[attr-defined]
    locust.constant_throughput = lambda rate: lambda _user: rate  # type: ignore[attr-defined]
    locust.events = SimpleNamespace(  # type: ignore[attr-defined]
        test_start=_FakeEventHook(),
        quitting=_FakeEventHook(),
        init=_FakeEventHook(),
    )
    locust.task = lambda function: function  # type: ignore[attr-defined]
    exception = ModuleType("locust.exception")
    exception.StopUser = type("StopUser", (Exception,), {})  # type: ignore[attr-defined]
    return {"locust": locust, "locust.exception": exception}


class _FakeResponse:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        lines: list[str] | None = None,
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self._lines = lines or []
        self.status_code = status_code
        self.content = json.dumps(self._payload).encode("utf-8")
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"
        self.success_called = False
        self.failure_message: str | None = None

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def json(self) -> dict[str, Any]:
        return self._payload

    def iter_lines(self, decode_unicode: bool = False) -> list[str]:
        del decode_unicode
        return self._lines

    def success(self) -> None:
        self.success_called = True

    def failure(self, message: str) -> None:
        self.failure_message = message


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls.append((args, kwargs))
        return self.response


class _FakeRecorder:
    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.records: list[Any] = []

    def record_attempt(self, **entry: Any) -> None:
        self.attempts.append(entry)

    def record(self, record: Any) -> None:
        self.records.append(record)


def test_locust_import_keeps_the_provider_source_contract() -> None:
    config = copy.deepcopy(load_config())
    provider = config["providers"]["yibu"]
    provider["reference_source_id"] = "aliyun_maas"
    provider["models"]["default"] = "deepseek-v4-pro"
    module_name = "locustfile_aliyun_source_contract_test"
    module_path = Path(__file__).resolve().parents[1] / "locustfile.py"

    with TemporaryDirectory() as report_dir:
        environment = patch.dict(
            os.environ,
            {
                "LOADTEST_SKIP_DOTENV": "1",
                "LOADTEST_PROVIDER": "yibu",
                "LOADTEST_MODEL": "deepseek-v4-pro",
                "LOADTEST_WORKLOAD": "throughput_rpm",
                "LOADTEST_REQUEST_MODE": "fixed",
                "LOADTEST_REPORT_DIR": report_dir,
                "LOADTEST_TARGET_RPM": "1",
                "LOADTEST_USERS": "1",
                "LOADTEST_JOB_SPEC": "",
                "LLM_API_TEST_PROVIDERS_LOCAL": str(
                    Path(report_dir) / "providers.local.yaml"
                ),
            },
            clear=False,
        )
        with (
            environment,
            patch.dict(sys.modules, _fake_locust_modules()),
            patch("lib.config.load_config", return_value=config),
        ):
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)

    assert module.MODEL_PROFILE_DATABASE["source_id"] == "aliyun_maas"
    assert (
        module.REFERENCE_CONTRACT_ID
        == "aliyun_deepseek_v4_openai_compat"
    )
    assert module.MODEL_CAPABILITY["source_id"] == "aliyun_maas"
    assert module.MODEL_CAPABILITY["pressure_test_enabled"] is True
    built = module.build_request(
        module.CONFIG,
        "throughput_profiles",
        "standard_short",
    )
    assert built.metadata["reference_source"] == module.REFERENCE_CONTRACT_ID
    assert (
        built.metadata["capability_profile_id"]
        == "text/aliyun_maas/deepseek/deepseek-v4-pro"
    )


def test_locust_import_rejects_a_changed_sweep_snapshot_digest() -> None:
    config = copy.deepcopy(load_config())
    config["active_provider"] = "yibu"
    config["providers"]["yibu"]["models"]["default"] = MODEL
    module_name = "locustfile_changed_sweep_snapshot_test"
    module_path = Path(__file__).resolve().parents[1] / "locustfile.py"

    with TemporaryDirectory() as report_dir, patch.dict(
        os.environ,
        {
            "LOADTEST_SKIP_DOTENV": "1",
            "LOADTEST_PROVIDER": "yibu",
            "LOADTEST_MODEL": MODEL,
            "LOADTEST_WORKLOAD": "throughput_rpm",
            "LOADTEST_REQUEST_MODE": "fixed",
            "LOADTEST_REPORT_DIR": report_dir,
            "LOADTEST_TARGET_RPM": "1",
            "LOADTEST_USERS": "1",
            "LOADTEST_JOB_SPEC": "",
            "LOADTEST_EXPECTED_MPDB_SNAPSHOT_DIGEST": "0" * 64,
            "LLM_API_TEST_PROVIDERS_LOCAL": str(
                Path(report_dir) / "providers.local.yaml"
            ),
        },
        clear=False,
    ), patch.dict(sys.modules, _fake_locust_modules()), patch(
        "lib.config.load_config", return_value=config
    ):
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            try:
                spec.loader.exec_module(module)
            except RuntimeError as exc:
                assert "snapshot digest conflicts" in str(exc)
            else:
                raise AssertionError("Locust accepted a changed sweep snapshot")
        finally:
            sys.modules.pop(module_name, None)


class LocustOutcomeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._report_dir = TemporaryDirectory()
        cls._prior_locustfile = sys.modules.pop("locustfile", None)
        cls._locust_modules = patch.dict(sys.modules, _fake_locust_modules())
        cls._locust_modules.start()
        cls._environment = patch.dict(
            os.environ,
            {
                "LOADTEST_SKIP_DOTENV": "1",
                "LOADTEST_PROVIDER": "yibu",
                "LOADTEST_MODEL": MODEL,
                "LOADTEST_WORKLOAD": "throughput_rpm",
                "LOADTEST_REQUEST_MODE": "fixed",
                "LOADTEST_REPORT_DIR": cls._report_dir.name,
                "LOADTEST_TARGET_RPM": "1",
                "LOADTEST_TARGET_TPM": "0",
                "LOADTEST_USERS": "1",
                "LOADTEST_WARMUP_SEC": "0",
                "LOADTEST_MEASURE_DURATION_SEC": "0",
                "LOADTEST_JOB_SPEC": "",
                "LOADTEST_HTTP_EXTRA_HEADERS": "",
                "LLM_API_TEST_PROVIDERS_LOCAL": os.path.join(
                    cls._report_dir.name, "providers.local.yaml"
                ),
            },
            clear=False,
        )
        cls._environment.start()
        cls.loadtest = importlib.import_module("locustfile")

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("locustfile", None)
        if cls._prior_locustfile is not None:
            sys.modules["locustfile"] = cls._prior_locustfile
        cls._environment.stop()
        cls._locust_modules.stop()
        cls._report_dir.cleanup()

    def _invoke_claude(
        self,
        response: _FakeResponse,
        *,
        group: str,
        stream: bool,
    ) -> tuple[dict[str, Any] | None, _FakeRecorder]:
        recorder = _FakeRecorder()
        user = object.__new__(self.loadtest.DeepSeekLoadUser)
        user.client = _FakeClient(response)
        user.api_key = "test-key"
        user.timeout_sec = 1
        body = {
            "model": "claude-haiku-4-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        }
        name = f"chat:{group}:refusal"
        with (
            patch.object(self.loadtest, "RECORDER", recorder),
            patch.object(
                self.loadtest,
                "METRICS",
                {"failure_finish_reasons": ["refusal"]},
            ),
            patch.object(self.loadtest, "TARGET_TOKEN_RATE_LIMITER", None),
            patch.object(self.loadtest, "ADAPTIVE_CONTROLLER", None),
            patch.object(self.loadtest, "_admit_request", return_value=None),
            patch.object(self.loadtest, "_is_warmup_timestamp", return_value=False),
            patch.object(
                self.loadtest, "_transport_target", return_value="https://test/messages"
            ),
            patch.object(self.loadtest, "_transport_headers", return_value={}),
        ):
            payload = user._post_chat(
                name,
                group,
                "refusal",
                body,
                validate=False,
                transport="claude_messages",
            )
        return payload, recorder

    def test_throughput_2xx_uses_exact_mpdb_contract_and_records_success(self) -> None:
        if hasattr(self.loadtest, "MODEL_PROFILE_DATABASE"):
            database = self.loadtest.MODEL_PROFILE_DATABASE
        else:
            database = self.loadtest.MODEL_DATABASE_BINDING
        interface = database["interface"]
        test_binding = next(
            binding
            for binding in interface["test_bindings"]
            if binding["test_binding_id"] == TEST_BINDING_ID
        )
        contract = interface["contracts"][CONTRACT_ID]

        self.assertEqual(self.loadtest.SELECTED_MODEL, MODEL)
        self.assertEqual(self.loadtest.WORKLOAD, "throughput_rpm")
        self.assertEqual(self.loadtest.REQUEST_MODE, "fixed")
        self.assertEqual(database["profile_id"], PROFILE_ID)
        self.assertEqual(database["interface_id"], INTERFACE_ID)
        self.assertEqual(interface["default_contract_id"], CONTRACT_ID)
        self.assertEqual(test_binding["test_binding_id"], TEST_BINDING_ID)
        self.assertTrue(test_binding["pressure_test_enabled"])

        group = "throughput_profiles"
        profile = "standard_short"
        built = self.loadtest.build_request(self.loadtest.CONFIG, group, profile)
        self.loadtest._apply_request_mode(
            built.body,
            str(built.metadata.get("transport") or "chat_completions"),
        )
        self.assertEqual(built.metadata["capability_profile_id"], PROFILE_ID)
        self.assertEqual(built.metadata["requested_model"], MODEL)
        for parameter in built.body:
            self.assertIn(parameter, contract["parameter_capabilities"])

        response = _FakeResponse(
            payload={
                "id": "chatcmpl-pressure-smoke",
                "model": MODEL,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 1,
                    "total_tokens": 13,
                },
            }
        )
        recorder = _FakeRecorder()
        client = _FakeClient(response)
        user = object.__new__(self.loadtest.DeepSeekLoadUser)
        user.client = client
        user.api_key = "test-key"
        user.timeout_sec = 1
        with (
            patch.object(self.loadtest, "RECORDER", recorder),
            patch.object(self.loadtest, "TARGET_TOKEN_RATE_LIMITER", None),
            patch.object(self.loadtest, "ADAPTIVE_CONTROLLER", None),
            patch.object(self.loadtest, "_admit_request", return_value=None),
            patch.object(self.loadtest, "_is_warmup_timestamp", return_value=False),
            patch.object(
                self.loadtest,
                "_transport_target",
                return_value="https://test/chat/completions",
            ),
            patch.object(self.loadtest, "_transport_headers", return_value={}),
        ):
            payload = user._post_chat(
                f"chat:{group}:{profile}",
                group,
                profile,
                built.body,
                transport="chat_completions",
            )

        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["json"], built.body)
        self.assertTrue(response.success_called)
        self.assertIsNone(response.failure_message)
        self.assertEqual(len(recorder.attempts), 1)
        self.assertEqual(len(recorder.records), 1)
        record = recorder.records[0]
        self.assertTrue(record.success)
        self.assertEqual(record.group, group)
        self.assertEqual(record.profile, profile)
        self.assertEqual(record.extra["requested_model"], MODEL)
        self.assertEqual(record.extra["response_model"], MODEL)
        if hasattr(self.loadtest, "MODEL_DATABASE_BINDING"):
            self.assertEqual(record.extra["profile_id"], PROFILE_ID)
            self.assertEqual(record.extra["interface_id"], INTERFACE_ID)
            self.assertEqual(record.extra["test_binding_id"], TEST_BINDING_ID)

    @staticmethod
    def _json_refusal_payload(response_id: str) -> dict[str, Any]:
        return {
            "id": response_id,
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "I cannot help with that."}],
            "stop_reason": "refusal",
            "stop_details": {
                "type": "refusal",
                "category": "safety",
                "explanation": "The request was declined by policy.",
            },
            "usage": {"input_tokens": 5, "output_tokens": 6},
        }

    def test_json_refusal_is_failure_only_for_throughput_and_is_recorded(self) -> None:
        throughput_response = _FakeResponse(
            payload=self._json_refusal_payload("msg-throughput")
        )
        payload, recorder = self._invoke_claude(
            throughput_response,
            group="throughput_profiles",
            stream=False,
        )

        self.assertEqual(payload["id"], "msg-throughput")
        self.assertFalse(throughput_response.success_called)
        self.assertEqual(
            throughput_response.failure_message, "finish_reason:refusal"
        )
        self.assertEqual(len(recorder.attempts), 1)
        self.assertEqual(len(recorder.records), 1)
        record = recorder.records[0]
        self.assertEqual(record.status_code, 200)
        self.assertFalse(record.success)
        self.assertEqual(record.failure_classification, "finish_reason:refusal")
        self.assertEqual(record.extra["response_id"], "msg-throughput")
        self.assertEqual(record.extra["stop_details_type"], "refusal")
        self.assertEqual(record.extra["stop_details_category"], "safety")
        self.assertEqual(
            record.extra["stop_details_explanation"],
            "The request was declined by policy.",
        )

        compatibility_response = _FakeResponse(
            payload=self._json_refusal_payload("msg-compatibility")
        )
        _payload, compatibility_recorder = self._invoke_claude(
            compatibility_response,
            group="compatibility_profiles",
            stream=False,
        )

        self.assertTrue(compatibility_response.success_called)
        self.assertIsNone(compatibility_response.failure_message)
        compatibility_record = compatibility_recorder.records[0]
        self.assertTrue(compatibility_record.success)
        self.assertIsNone(compatibility_record.failure_classification)
        self.assertEqual(compatibility_record.finish_reason, "refusal")

    def test_stream_refusal_persists_stop_details_and_response_id(self) -> None:
        details = {
            "type": "refusal",
            "category": "safety",
            "explanation": "Streaming refusal explanation.",
        }
        lines = [
            "event: message_start",
            "data: "
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-stream",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [],
                        "usage": {"input_tokens": 7, "output_tokens": 0},
                    },
                }
            ),
            "event: message_delta",
            "data: "
            + json.dumps(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "refusal",
                        "stop_details": details,
                    },
                    "usage": {"output_tokens": 3},
                }
            ),
            "event: message_stop",
            'data: {"type": "message_stop"}',
        ]
        response = _FakeResponse(lines=lines)

        payload, recorder = self._invoke_claude(
            response,
            group="throughput_profiles",
            stream=True,
        )

        self.assertEqual(payload["id"], "msg-stream")
        self.assertEqual(payload["stop_reason"], "refusal")
        self.assertEqual(payload["stop_details"], details)
        self.assertFalse(response.success_called)
        self.assertEqual(response.failure_message, "finish_reason:refusal")
        self.assertEqual(len(recorder.attempts), 1)
        self.assertEqual(len(recorder.records), 1)
        record = recorder.records[0]
        self.assertEqual(record.status_code, 200)
        self.assertFalse(record.success)
        self.assertEqual(record.extra["response_id"], "msg-stream")
        self.assertEqual(
            record.extra["response_model"], "claude-haiku-4-5-20251001"
        )
        self.assertEqual(record.extra["stop_details_type"], "refusal")
        self.assertEqual(record.extra["stop_details_category"], "safety")
        self.assertEqual(
            record.extra["stop_details_explanation"],
            "Streaming refusal explanation.",
        )


if __name__ == "__main__":
    unittest.main()
