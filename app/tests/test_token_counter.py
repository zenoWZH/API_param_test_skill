from __future__ import annotations

import sys
from types import SimpleNamespace

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
