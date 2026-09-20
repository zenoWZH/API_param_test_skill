"""A closed source reference must not alter a runnable sibling selection."""
import copy

import pytest

from lib import model_profile_catalog as catalog_module


def select(contract_id=None):
    return catalog_module._resolve_exact_profile_interface(source_id="zhipu", modality="text",
        family="glm", model="glm-5.3", api_form="openai_chat_completions", contract_id=contract_id)[1]


def test_closed_coding_sibling_does_not_shadow_existing_default():
    assert select()["interface_id"].endswith("#openai-chat-default")
    assert select("zhipu_glm_5_3_openai_compat")["interface_id"].endswith("#openai-chat-default")


def test_explicit_closed_or_unknown_contract_never_falls_back_to_another_interface():
    with pytest.raises(ValueError, match="interface.enabled|interface.executable"):
        select("zai_coding_glm_5_3_chat")
    with pytest.raises(ValueError, match="found=0"):
        select("unregistered_contract")


def test_two_runnable_interfaces_remain_ambiguous(monkeypatch):
    real = catalog_module.get_model_profile_catalog()
    class Catalog:
        def list_profiles(self, **kwargs): return real.list_profiles(**kwargs)
        def get_profile(self, key): return real.get_profile(key)
        def get_interface(self, key):
            value = copy.deepcopy(real.get_interface(key))
            if key.endswith("#zai-coding-chat"):
                value.update(enabled=True, executable=True)
            return value
    monkeypatch.setattr(catalog_module, "get_model_profile_catalog", lambda: Catalog())
    with pytest.raises(ValueError, match="found=2"):
        select()
    assert select("zhipu_glm_5_3_openai_compat")["interface_id"].endswith("#openai-chat-default")
