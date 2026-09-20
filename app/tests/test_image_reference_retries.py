from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib.image_validation import ImageTestCase
from scripts import image_param_test as runner


@pytest.mark.parametrize("header_name", ["Retry-After", "retry-after"])
def test_rate_limit_response_preserves_retry_after_through_dispatch(tmp_path, monkeypatch, header_name):
    sleeps = []
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    responses = iter([
        SimpleNamespace(status_code=429, headers={header_name: "60", "Set-Cookie": "private"},
                        text="", json=lambda: {"error": {"message": "rate limited"}}),
        SimpleNamespace(status_code=400, headers={}, text="",
                        json=lambda: {"error": {"message": "invalid parameter"}}),
    ])
    session = SimpleNamespace(post=lambda *args, **kwargs: next(responses))
    case = ImageTestCase(name="baseline", parameters={}, expected_outcome="observation")
    results, budget = runner._execute_image_cases(
        [case],
        lambda selected: runner.run_case(
            session, "https://image-provider.example/v1/images/generations", "gpt-image-2",
            "draw", selected, timeout=3, images_dir=tmp_path, visual_forensics=False,
        ),
        report_dir=tmp_path, reference_observation=True,
        max_generation_requests=2, transient_retries=1,
    )
    attempts = json.loads((tmp_path / "attempt_results.json").read_text())
    assert [attempt["status_code"] for attempt in attempts] == [429, 400]
    assert attempts[0]["response_headers"] == {"retry-after": "60"}
    assert attempts[0]["retry_delay_seconds"] == 60
    assert sleeps == [60]
    assert budget["generation_request_count"] == 2
    assert results[0]["status_code"] == 400
