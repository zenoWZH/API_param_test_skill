"""Real configured clients retain exact origins despite optional /v1 bases."""
import pytest

from lib import client as client_module
from lib.client import DeepSeekClient
from lib.config import load_config
from lib.credential_security import ProviderCredential
from lib.deepseek_beta_runner import _official_client_origin_matches


@pytest.fixture
def configuration(monkeypatch):
    monkeypatch.setenv("LOADTEST_SKIP_DOTENV", "1")
    monkeypatch.setenv("LLM_API_TEST_PROVIDERS_LOCAL", "/tmp/approved-no-private.yaml")
    monkeypatch.setattr(client_module, "get_api_key", lambda *args: "offline-configured-client-key")
    return load_config()


@pytest.mark.parametrize("provider,origin", [
    ("anthropic_official", "https://api.anthropic.com"), ("deepseek_official", "https://api.deepseek.com")])
@pytest.mark.parametrize("suffix", ["", "/v1"])
def test_exact_two_base_representations_from_real_config(configuration, provider, origin, suffix):
    configuration["providers"][provider]["base_url"] = origin + suffix
    client = DeepSeekClient.from_config(configuration, provider)
    assert client.base_url == origin + suffix
    assert _official_client_origin_matches(client, provider)
    assert client._credential.allowed_origins == frozenset({origin})
    client.session.close()


@pytest.mark.parametrize("base", ["http://api.anthropic.com/v1", "https://api.anthropic.com.evil.invalid/v1",
    "https://api.anthropic.com/v2", "https://api.anthropic.com/v1/other", "https://other.invalid"])
def test_other_bases_are_not_normalized_into_official_authority(configuration, base):
    client = DeepSeekClient.from_config(configuration, "anthropic_official")
    client.base_url = base
    assert not _official_client_origin_matches(client, "anthropic_official")
    client.session.close()


@pytest.mark.parametrize("provider,origins", [
    ("gateway", ["https://api.anthropic.com"]),
    ("anthropic_official", ["https://other.invalid"]),
    ("anthropic_official", ["https://api.anthropic.com", "https://other.invalid"]),
])
def test_credential_provider_and_origin_binding_cannot_be_borrowed(configuration, provider, origins):
    client = DeepSeekClient.from_config(configuration, "anthropic_official")
    client._credential = ProviderCredential.create(provider=provider, secret="offline-another-key", base_urls=origins)
    assert not _official_client_origin_matches(client, "anthropic_official")
    client.session.close()
