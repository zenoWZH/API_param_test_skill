import copy

import pytest

from lib.config import transport_for_api_form
from lib.reference_specs import _app_transport_capability_view, load_model_capability_profile


def test_beta_catalog_can_be_queried_without_claiming_a_generic_app_transport():
    cap = load_model_capability_profile(
        "text", "deepseek", "deepseek-v4-flash-0731", api_form="deepseek_beta_chat_prefix",
        route_profile="vendor_direct", reference_source="deepseek_v4_flash_0731_chat_prefix_beta",
    )
    assert cap["catalog_execution_flags"]["executable"] is True
    assert cap["catalog_execution_flags"]["parameter_test_enabled"] is True
    assert cap["enabled"] is True and cap["executable"] is False
    assert cap["parameter_test_enabled"] is False and cap["pressure_test_enabled"] is False
    assert cap["app_transport_available"] is False
    assert transport_for_api_form("deepseek_beta_chat_prefix") == "deepseek-beta-chat-prefix"


@pytest.mark.parametrize("form", ["openai_chat_completions", "openai_responses", "openai_fim_completions_beta", "openai_images_generations", "gemini_generate_content"])
def test_other_forms_keep_their_own_execution_flags(form):
    source = {"api_form": form, "enabled": True, "executable": True,
              "parameter_test_enabled": True, "pressure_test_enabled": True}
    before = copy.deepcopy(source)
    assert _app_transport_capability_view(source) == before
    assert source == before and "app_transport_available" not in source
