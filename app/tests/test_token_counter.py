from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from lib import token_counter as token_counter_module
from lib.token_counter import count_semantic_tokens


def test_missing_counter_is_explicitly_unavailable() -> None:
    result = count_semantic_tokens(
        {}, provider=None, model="m", input_text="input", output_text="output"
    )

    assert result["input"]["evidence_level"] == "unavailable"
    assert result["output"]["tokens"] is None


def test_configured_tiktoken_counter_declares_exact_dimensions(monkeypatch) -> None:
    encoding = SimpleNamespace(encode=lambda text: text.split())
    monkeypatch.setitem(
        sys.modules,
        "tiktoken",
        SimpleNamespace(get_encoding=lambda _name: encoding),
    )
    config = {
        "test_cases": {
            "token_accuracy": {
                "counters": {
                    "model-a": {
                        "kind": "tiktoken",
                        "encoding": "test",
                        "exact_dimensions": ["input", "output"],
                    }
                }
            }
        }
    }

    result = count_semantic_tokens(
        config,
        provider=None,
        model="model-a",
        input_text="one two",
        output_text="three four five",
    )

    assert result["input"] == {
        "tokens": 2,
        "evidence_level": "exact",
        "note": None,
    }
    assert result["output"]["tokens"] == 3
    assert result["output"]["evidence_level"] == "exact"


@pytest.mark.parametrize("exact_transports", [["openai_responses"], ["*"]])
def test_exact_dimensions_require_matching_transport_declaration(
    monkeypatch, exact_transports
) -> None:
    monkeypatch.setattr(
        token_counter_module,
        "_load_counter",
        lambda _spec, _model: (lambda text: len(text.split()), "fake"),
    )
    config = {
        "test_cases": {
            "token_accuracy": {
                "counters": {
                    "model-a": {
                        "kind": "fake",
                        "exact_dimensions": ["input", "output"],
                        "exact_transports": exact_transports,
                    }
                }
            }
        }
    }

    result = count_semantic_tokens(
        config,
        provider=None,
        model="model-a",
        input_text="one two",
        output_text="three",
        transport="openai_responses",
    )

    assert result["input"]["evidence_level"] == "exact"
    assert result["output"]["evidence_level"] == "exact"


@pytest.mark.parametrize("exact_transports", [None, [], ["openai_chat"]])
def test_transport_without_exact_declaration_downgrades_to_estimate(
    monkeypatch, exact_transports
) -> None:
    monkeypatch.setattr(
        token_counter_module,
        "_load_counter",
        lambda _spec, _model: (lambda text: len(text), "fake"),
    )
    spec = {
        "kind": "fake",
        "exact_dimensions": ["input", "output"],
    }
    if exact_transports is not None:
        spec["exact_transports"] = exact_transports
    config = {
        "test_cases": {
            "token_accuracy": {"counters": {"model-a": spec}}
        }
    }

    result = count_semantic_tokens(
        config,
        provider=None,
        model="model-a",
        input_text="in",
        output_text="out",
        transport="openai_responses",
    )

    assert result["input"]["evidence_level"] == "estimate"
    assert "exactness is not declared" in result["input"]["note"]
    assert "openai_responses" in result["input"]["note"]


def test_dimension_counter_exception_is_isolated(monkeypatch) -> None:
    def counter(text: str) -> int:
        if text == "bad input":
            raise RuntimeError("dimension failed")
        return 7

    monkeypatch.setattr(
        token_counter_module,
        "_load_counter",
        lambda _spec, _model: (counter, "fake"),
    )
    config = {
        "test_cases": {
            "token_accuracy": {
                "counters": {"model-a": {"kind": "fake"}}
            }
        }
    }

    result = count_semantic_tokens(
        config,
        provider=None,
        model="model-a",
        input_text="bad input",
        output_text="good output",
    )

    assert result["input"]["evidence_level"] == "unavailable"
    assert "RuntimeError: dimension failed" in result["input"]["note"]
    assert result["output"]["tokens"] == 7


@pytest.mark.parametrize("invalid", [True, 1.5, "3", -1])
def test_invalid_counter_return_is_unavailable(monkeypatch, invalid) -> None:
    monkeypatch.setattr(
        token_counter_module,
        "_load_counter",
        lambda _spec, _model: (lambda _text: invalid, "fake"),
    )
    config = {
        "test_cases": {
            "token_accuracy": {
                "counters": {"model-a": {"kind": "fake"}}
            }
        }
    }

    result = count_semantic_tokens(
        config,
        provider=None,
        model="model-a",
        input_text="input",
        output_text="output",
    )

    assert result["input"]["tokens"] is None
    assert result["input"]["evidence_level"] == "unavailable"
    assert "expected a non-negative int" in result["input"]["note"]
    assert result["output"]["evidence_level"] == "unavailable"
