"""Endpoint request IDs cannot be inferred from bare canonical profile names."""
import pytest

from lib.model_profile_catalog import resolve_runtime_profile_binding
from lib.reference_specs import load_model_capability_profile


MANTLE_MODELS = (
    ("anthropic.claude-haiku-4-5", "claude-haiku-4-5-20251001"),
    ("anthropic.claude-opus-4-7", "claude-opus-4-7"),
    ("anthropic.claude-opus-4-8", "claude-opus-4-8"),
    ("anthropic.claude-opus-5", "claude-opus-5"),
    ("anthropic.claude-sonnet-5", "claude-sonnet-5"),
)


def config_for(runtime_model, *, reference_model=None, profile_model=None):
    models = {
        "default": runtime_model,
        "candidates": [runtime_model],
        "families": {runtime_model: "claude"},
        "transports": {runtime_model: "claude_messages"},
        "routes": {runtime_model: {"aws_bedrock": {"api_forms": {"anthropic_messages": {}}}}},
        "default_routes": {runtime_model: "aws_bedrock"},
        "default_api_forms": {runtime_model: {"aws_bedrock": "anthropic_messages"}},
    }
    if reference_model is not None:
        models["reference_model_ids"] = {runtime_model: reference_model}
    if profile_model is not None:
        profile = "text/aws_bedrock/claude/" + profile_model
        models["reference_bindings"] = {runtime_model: {
            "profile_id": profile,
            "interfaces": {"aws_bedrock": {"anthropic_messages": profile + "#bedrock-mantle-messages"}},
        }}
    return {"active_provider": "identity_fixture", "providers": {"identity_fixture": {
        "name": "identity_fixture", "reference_source_id": "aws_bedrock", "models": models,
    }}}


def resolve(config, model):
    return resolve_runtime_profile_binding(config, "identity_fixture", model, "claude", "aws_bedrock", "anthropic_messages")


@pytest.mark.parametrize("native,canonical", MANTLE_MODELS)
@pytest.mark.parametrize("canonical_reference", [False, True])
def test_approved_native_request_ids_preserve_their_exact_mantle_interface(native, canonical, canonical_reference):
    config = config_for(native, reference_model=canonical if canonical_reference else None)
    result = resolve(config, native)
    assert result["catalog_resolved"]
    assert result["interface_id"] == f"text/aws_bedrock/claude/{canonical}#bedrock-mantle-messages"
    assert result["interface"]["request_model_ids"] == [native]


@pytest.mark.parametrize("native,canonical", MANTLE_MODELS)
def test_unmapped_bare_canonical_name_stays_unresolved(native, canonical):
    result = resolve(config_for(canonical), canonical)
    assert result["catalog_resolved"] is False
    assert result["reference_unresolved"] is True
    assert result["interface_id"] is None


@pytest.mark.parametrize("native,canonical", MANTLE_MODELS)
@pytest.mark.parametrize("explicit_interface", [False, True])
def test_explicit_gateway_mapping_to_native_reference_id_is_valid(native, canonical, explicit_interface):
    alias = "gateway-" + canonical
    config = config_for(alias, reference_model=native, profile_model=canonical if explicit_interface else None)
    result = resolve(config, alias)
    assert result["catalog_resolved"]
    assert result["reference_model_id"] == native
    assert result["reference_model_id_inferred"] is False
    assert result["interface_id"] == f"text/aws_bedrock/claude/{canonical}#bedrock-mantle-messages"


@pytest.mark.parametrize("native,canonical", MANTLE_MODELS)
def test_explicit_interface_alone_does_not_supply_a_native_request_id(native, canonical):
    with pytest.raises(ValueError, match="interface.request_model_ids"):
        resolve(config_for(canonical, profile_model=canonical), canonical)


@pytest.mark.parametrize("runtime", ["gateway-haiku", "anthropic.claude-haiku-4-5"])
def test_runtime_haiku_reference_id_cannot_be_used_for_mantle_even_with_explicit_interface(runtime):
    config = config_for(runtime, reference_model="anthropic.claude-haiku-4-5-20251001-v1:0", profile_model="claude-haiku-4-5-20251001")
    with pytest.raises(ValueError, match="interface.request_model_ids"):
        resolve(config, runtime)


def test_runtime_haiku_id_does_not_resolve_implicitly_to_mantle():
    runtime = "anthropic.claude-haiku-4-5-20251001-v1:0"
    result = resolve(config_for(runtime), runtime)
    assert result["catalog_resolved"] is False
    assert result["reference_unresolved"] is True


@pytest.mark.parametrize("native,canonical", MANTLE_MODELS)
def test_canonical_reference_metadata_lookup_does_not_infer_a_runtime_request(native, canonical):
    cap = load_model_capability_profile(
        "text", "claude", canonical, api_form="anthropic_messages", route_profile="aws_bedrock",
        reference_source="claude_aws_bedrock_mantle_messages",
    )
    assert cap["profile_id"] == "text/aws_bedrock/claude/" + canonical
    assert cap["interface_id"].endswith("#bedrock-mantle-messages")
    # The separate actual-runtime lookup still rejects the same bare name.
    assert resolve(config_for(canonical), canonical)["catalog_resolved"] is False
