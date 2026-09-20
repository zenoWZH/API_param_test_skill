from __future__ import annotations
import pytest
from lib.reference_specs import load_model_capability_profile

@pytest.mark.parametrize(("form", "contract"), [
    ("anthropic_messages", "claude_fable_native_messages"),
    ("openai_chat_completions", "claude_fable_openai_compat"),
])
def test_enabled_opus5_fable_suite_resolves_its_exact_model_policy(form, contract):
    capability = load_model_capability_profile(
        "text", "claude_fable", "claude-opus-5", api_form=form,
        reference_source=contract,
    )
    assert capability["source_id"] == "anthropic"
    assert capability["test_binding_id"].endswith("/suite/claude_fable")
    assert capability["reference_contract_id"] == contract
    assert capability["parameter_test_enabled"] is True
    with pytest.raises(ValueError):
        load_model_capability_profile(
            "text", "claude", "claude-opus-5", api_form=form,
            reference_source=contract,
        )
