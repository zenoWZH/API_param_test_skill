from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only before deps install
    raise RuntimeError("PyYAML is required. Install with: pip install -r requirements.txt") from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
SKILL_DATA_DIR_ENV = "LLM_API_TEST_DATA_DIR"


def skill_data_dir() -> Path:
    return Path(
        os.getenv(SKILL_DATA_DIR_ENV) or (Path.home() / ".config" / "llm-api-test")
    ).expanduser()


def _env_path(env_name: str, fallback: Path) -> Path:
    value = os.getenv(env_name)
    return Path(value).expanduser() if value else fallback


LOCAL_PROVIDERS_PATH = _env_path(
    "LLM_API_TEST_PROVIDERS_LOCAL", PROJECT_ROOT / "providers.local.yaml"
)
DEFAULT_TIMEOUT_SEC = 300
LEGACY_API_KEY_ENV_NAMES = ("YIBU_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
SELECTED_API_KEY_ENV = "LOADTEST_SELECTED_API_KEY"
SELECTED_API_KEY_PROVIDER_ENV = "LOADTEST_SELECTED_API_KEY_PROVIDER"
SKIP_DOTENV_ENV = "LOADTEST_SKIP_DOTENV"
PROVIDER_ALIASES = {
    "serveice_gemini": "inferenceai",
    "service_gemini": "inferenceai",
}
SUPPORTED_TRANSPORTS = {
    "chat_completions",
    "claude_messages",
    "gemini_generate_content",
    "openai_responses",
}
SUPPORTED_BACKENDS = {
    "openai_compatible",
    "anthropic",
    "gemini_ai_studio",
    "proxy_unknown",
}
SUPPORTED_AUTH_MODES = {"bearer", "anthropic", "google_api_key"}
SUPPORTED_MODEL_FAMILIES = {
    "deepseek",
    "glm",
    "gpt",
    "kimi",
    "minimax",
    "grok",
    "qwen",
    "gemini",
    "claude",
    "claude_fable",
}
SUPPORTED_API_FORMS = {
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_generate_content",
    "openai_images_generations",
    "gemini_interactions",
}
TEXT_API_FORM_BY_TRANSPORT = {
    "chat_completions": "openai_chat_completions",
    "openai_responses": "openai_responses",
    "claude_messages": "anthropic_messages",
    "gemini_generate_content": "gemini_generate_content",
}
TEXT_TRANSPORT_BY_API_FORM = {
    api_form: transport for transport, api_form in TEXT_API_FORM_BY_TRANSPORT.items()
}
IMAGE_API_FORM_BY_TRANSPORT = {
    "images-generations": "openai_images_generations",
    "chat-completions": "openai_chat_completions",
    "gemini-interactions": "gemini_interactions",
}
IMAGE_TRANSPORT_BY_API_FORM = {
    api_form: transport for transport, api_form in IMAGE_API_FORM_BY_TRANSPORT.items()
}
SUPPORTED_IMAGE_FAMILIES = {"gpt-image-2", "banana", "grok-imagine"}
SUPPORTED_IMAGE_TRANSPORTS = {
    "images-generations",
    "chat-completions",
    "gemini-interactions",
}
IMAGE_TRANSPORT_INTERFACES = {
    "images-generations": "images_generations",
    "chat-completions": "chat_completions",
    "gemini-interactions": "gemini_interactions",
}
IMAGE_TRANSPORT_AUTH_MODES = {
    "images-generations": {"bearer"},
    "chat-completions": {"bearer"},
    "gemini-interactions": {"bearer", "google_api_key"},
}
DEFAULT_INTERFACE_PATHS = {
    "chat_completions": "/chat/completions",
    "claude_messages": "/messages",
    "gemini_generate_content": "/models/{model}:generateContent",
    "openai_responses": "/responses",
    "images_generations": "/images/generations",
    "gemini_interactions": "/v1beta/interactions",
}


def load_dotenv(path: str | Path | None = None) -> None:
    if os.getenv(SKIP_DOTENV_ENV) == "1":
        return
    env_override = os.getenv("LLM_API_TEST_DOTENV")
    env_path = (
        Path(path)
        if path
        else (Path(env_override).expanduser() if env_override else PROJECT_ROOT / ".env")
    )
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv()
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config = _without_inline_api_keys(_read_yaml(config_path))

    local_path = LOCAL_PROVIDERS_PATH if path is None else config_path.with_name("providers.local.yaml")
    if local_path.exists():
        config = deep_merge(config, _without_inline_api_keys(_read_yaml(local_path)))

    api = config.setdefault("api", {})
    if "base_url" in api:
        api["base_url"] = str(api["base_url"]).rstrip("/")
    _normalize_provider_config(config, prune_unknown_models=True)
    validate_provider_config(config)
    _apply_runtime_overrides(config)
    return config


def default_reports_root() -> Path:
    return _env_path("LLM_API_TEST_REPORTS_DIR", skill_data_dir() / "reports")


def get_timeout_sec(config: dict[str, Any] | None = None) -> int:
    cfg = config if config is not None else load_config()
    timeout = (cfg.get("api") or {}).get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    try:
        return max(int(timeout), 1)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SEC


def _apply_runtime_overrides(config: dict[str, Any]) -> None:
    env_timeout = os.getenv("LOADTEST_TIMEOUT_SEC")
    if not env_timeout:
        return
    try:
        config.setdefault("api", {})["timeout_sec"] = max(int(env_timeout), 1)
    except (TypeError, ValueError):
        return


def get_api_key(config: dict[str, Any] | None = None, provider: str | None = None) -> str:
    cfg = config if config is not None else load_config()
    provider_cfg = get_provider_config(cfg, provider)
    provider_name = str(provider_cfg.get("name") or provider or get_active_provider_name(cfg))

    selected_key = os.getenv(SELECTED_API_KEY_ENV)
    selected_provider = os.getenv(SELECTED_API_KEY_PROVIDER_ENV)
    if selected_key and normalize_provider_name(str(selected_provider or "")) == provider_name:
        return selected_key

    for name in _api_key_env_names(provider_cfg):
        value = os.getenv(name)
        if value:
            return value
    inline_key = _local_inline_api_key(provider_name)
    if inline_key:
        return inline_key
    raise RuntimeError(
        f"Missing API key for provider {provider_name!r}. Configure api_key_env in .env."
    )


def get_active_provider_name(config: dict[str, Any]) -> str:
    return normalize_provider_name(str(os.getenv("LOADTEST_PROVIDER") or config.get("active_provider") or "yibu"))


def normalize_provider_name(provider: str) -> str:
    return PROVIDER_ALIASES.get(provider, provider)


def get_provider_config(config: dict[str, Any], provider: str | None = None) -> dict[str, Any]:
    provider_name = normalize_provider_name(provider) if provider else get_active_provider_name(config)
    providers = config.get("providers") or {}
    if provider_name not in providers:
        raise KeyError(f"Provider {provider_name!r} not found in config.providers")
    provider_cfg = dict(providers[provider_name] or {})
    provider_cfg["name"] = provider_name
    if "base_url" in provider_cfg:
        provider_cfg["base_url"] = str(provider_cfg["base_url"]).rstrip("/")
    return provider_cfg


def get_selected_model(config: dict[str, Any], provider: str | None = None) -> str:
    env_model = os.getenv("LOADTEST_MODEL")
    if env_model:
        return env_model
    provider_cfg = get_provider_config(config, provider)
    models_cfg = provider_cfg.get("models") or {}
    if models_cfg.get("default"):
        return str(models_cfg["default"])
    if config.get("models", {}).get("default"):
        return str(config["models"]["default"])
    candidates = models_cfg.get("candidates") or []
    if candidates:
        return str(candidates[0])
    raise ValueError("No model configured. Set providers.<name>.models.default or LOADTEST_MODEL.")


def get_model_transport(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
    route_profile: str | None = None,
    api_form: str | None = None,
) -> str:
    """Resolve the internal adapter from the selected public API form."""
    provider_cfg = get_provider_config(config, provider)
    selected_model = model or get_selected_model(config, provider_cfg["name"])
    if api_form:
        selected_api_form = get_model_api_form(
            config,
            selected_model,
            provider_cfg["name"],
            route_profile=route_profile,
            api_form=api_form,
        )
        return transport_for_api_form(selected_api_form)
    model_transports = (provider_cfg.get("models") or {}).get("transports") or {}
    legacy_transport = str(model_transports.get(selected_model) or "")
    selected_api_form = get_model_api_form(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=route_profile,
        preferred_transport=legacy_transport or None,
    )
    transport = transport_for_api_form(selected_api_form)
    if transport not in SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Provider {provider_cfg['name']!r} model {selected_model!r} has invalid or missing transport {transport!r}."
        )
    return transport


def api_form_for_transport(transport: str, *, modality: str = "text") -> str:
    mapping = (
        TEXT_API_FORM_BY_TRANSPORT
        if str(modality).casefold() == "text"
        else IMAGE_API_FORM_BY_TRANSPORT
    )
    api_form = mapping.get(str(transport))
    if not api_form:
        raise ValueError(
            f"Unsupported {modality} transport for API-form resolution: {transport!r}."
        )
    return api_form


def transport_for_api_form(api_form: str) -> str:
    transport = TEXT_TRANSPORT_BY_API_FORM.get(str(api_form))
    if not transport:
        raise ValueError(f"Unsupported text API form: {api_form!r}.")
    return transport


def get_model_route_profiles(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return every configured route profile for one provider/model."""
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    models_cfg = provider_cfg.get("models") or {}
    routes = models_cfg.get("routes") or {}
    selected_routes = routes.get(selected_model)
    if not isinstance(selected_routes, dict) or not selected_routes:
        fallback_transport = str(
            (models_cfg.get("transports") or {}).get(selected_model)
            or provider_cfg.get("default_transport")
            or ""
        )
        selected_routes = _normalize_model_routes(
            None,
            legacy_api_forms=(models_cfg.get("api_forms") or {}).get(selected_model),
            fallback_route=str(
                provider_cfg.get("route_profile")
                or _infer_legacy_route_profile(
                    str(provider_cfg.get("name") or provider or ""),
                    str(provider_cfg.get("backend") or ""),
                )
            ),
            fallback_transport=fallback_transport,
        )
        legacy_dynamic = selected_routes.pop("dynamic_aggregator", None)
        if isinstance(legacy_dynamic, dict):
            family = normalize_model_family(
                selected_model,
                ((models_cfg.get("families") or {}).get(selected_model)),
            )
            for form_name, form_cfg in (
                legacy_dynamic.get("api_forms") or {}
            ).items():
                contract_route = _contract_route_for_legacy_form(
                    provider_name=str(provider_cfg.get("name") or provider or ""),
                    model_family=family,
                    api_form=str(form_name),
                    form_cfg=form_cfg if isinstance(form_cfg, dict) else {},
                    fallback_route=str(
                        provider_cfg.get("route_profile")
                        or _infer_legacy_route_profile(
                            str(provider_cfg.get("name") or provider or ""),
                            str(provider_cfg.get("backend") or ""),
                        )
                    ),
                )
                target = selected_routes.setdefault(
                    contract_route, {"api_forms": {}}
                )
                target["api_forms"][str(form_name)] = deep_merge(
                    target["api_forms"].get(str(form_name)) or {},
                    form_cfg if isinstance(form_cfg, dict) else {},
                )
    if not isinstance(selected_routes, dict) or not selected_routes:
        raise ValueError(
            f"Provider {provider_cfg['name']!r} model {selected_model!r} has no route profile."
        )
    return {
        str(route): dict(settings)
        for route, settings in selected_routes.items()
        if isinstance(settings, dict)
    }


def get_model_route_profile(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
    *,
    route_profile: str | None = None,
) -> str:
    """Resolve the route before any API-form selection."""
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    available = get_model_route_profiles(
        config, selected_model, provider_cfg["name"]
    )
    requested = str(
        route_profile or os.getenv("LOADTEST_ROUTE_PROFILE") or ""
    ).strip()
    if requested:
        if requested not in available:
            raise ValueError(
                f"Provider {provider_cfg['name']!r} model {selected_model!r} does not "
                f"expose route profile {requested!r}; available={sorted(available)}."
            )
        return requested
    models_cfg = provider_cfg.get("models") or {}
    configured_default = str(
        (models_cfg.get("default_routes") or {}).get(selected_model) or ""
    ).strip()
    if configured_default:
        if configured_default not in available:
            raise ValueError(
                f"Provider {provider_cfg['name']!r} model {selected_model!r} default "
                f"route profile {configured_default!r} is not enabled."
            )
        return configured_default
    if len(available) == 1:
        return next(iter(available))
    raise ValueError(
        f"Provider {provider_cfg['name']!r} model {selected_model!r} exposes multiple "
        f"route profiles {sorted(available)} but models.default_routes has no selection."
    )


def get_model_api_forms(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
    *,
    route_profile: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return API forms exposed by the selected provider/model route."""
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    selected_route = get_model_route_profile(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=route_profile,
    )
    route_cfg = get_model_route_profiles(
        config, selected_model, provider_cfg["name"]
    )[selected_route]
    forms = route_cfg.get("api_forms") or {}
    if not isinstance(forms, dict) or not forms:
        raise ValueError(
            f"Provider {provider_cfg['name']!r} model {selected_model!r} route "
            f"{selected_route!r} has no API form."
        )
    return {
        str(form): dict(settings)
        for form, settings in forms.items()
        if isinstance(settings, dict)
    }


def get_model_api_form(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
    *,
    route_profile: str | None = None,
    api_form: str | None = None,
    preferred_transport: str | None = None,
) -> str:
    """Resolve one API form without conflating it with the model family."""
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    selected_route = get_model_route_profile(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=route_profile,
    )
    available = get_model_api_forms(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=selected_route,
    )
    requested = str(api_form or os.getenv("LOADTEST_API_FORM") or "").strip()
    if requested:
        if requested not in available:
            raise ValueError(
                f"Provider {provider_cfg['name']!r} model {selected_model!r} does not "
                f"expose API form {requested!r} on route {selected_route!r}; "
                f"available={sorted(available)}."
            )
        return requested
    models_cfg = provider_cfg.get("models") or {}
    default_api_forms = models_cfg.get("default_api_forms") or {}
    model_defaults = default_api_forms.get(selected_model) or {}
    configured_default = str(
        (model_defaults.get(selected_route) or "")
        if isinstance(model_defaults, dict)
        else (model_defaults or "")
    ).strip()
    if configured_default:
        if configured_default not in available:
            raise ValueError(
                f"Provider {provider_cfg['name']!r} model {selected_model!r} default "
                f"API form {configured_default!r} is not enabled on route "
                f"{selected_route!r}."
            )
        return configured_default
    transport = str(preferred_transport or provider_cfg.get("default_transport") or "")
    if transport in TEXT_API_FORM_BY_TRANSPORT:
        transport_form = api_form_for_transport(transport)
        if transport_form in available:
            return transport_form
    if len(available) == 1:
        return next(iter(available))
    raise ValueError(
        f"Provider {provider_cfg['name']!r} model {selected_model!r} exposes multiple "
        f"API forms {sorted(available)} on route {selected_route!r} but "
        "models.default_api_forms has no selection."
    )


def resolve_threshold_config(
    config: dict[str, Any],
    stage: str,
    provider: str | None = None,
    model: str | None = None,
    job_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve Global → Provider → Model → Job threshold overrides."""
    provider_cfg = get_provider_config(config, provider)
    selected_model = model or get_selected_model(config, provider_cfg["name"])
    global_thresholds = (config.get("thresholds") or {}).get(stage) or {}
    provider_thresholds = (provider_cfg.get("thresholds") or {}).get(stage) or {}
    model_thresholds = (
        ((provider_cfg.get("models") or {}).get("thresholds") or {})
        .get(selected_model, {})
        .get(stage, {})
    )
    result = deep_merge({}, global_thresholds)
    result = deep_merge(result, provider_thresholds)
    result = deep_merge(result, model_thresholds)
    return deep_merge(result, job_thresholds or {})


def get_provider_interface(
    config: dict[str, Any],
    transport: str,
    provider: str | None = None,
) -> dict[str, Any]:
    provider_cfg = get_provider_config(config, provider)
    interfaces = provider_cfg.get("api_interfaces") or {}
    raw = interfaces.get(transport)
    if not isinstance(raw, dict):
        raise ValueError(
            f"Provider {provider_cfg['name']!r} does not configure api_interfaces.{transport}."
        )
    interface = dict(raw)
    interface["base_url"] = str(
        interface.get("base_url") or provider_cfg.get("base_url") or ""
    ).rstrip("/")
    interface.setdefault("path", DEFAULT_INTERFACE_PATHS[transport])
    return interface


def infer_model_family(model: str) -> str:
    """Infer a vendor model family without treating an API standard as a family."""
    lowered = str(model).strip().casefold()
    leaf = lowered.rsplit("/", 1)[-1]
    if leaf.startswith("deepseek"):
        return "deepseek"
    if leaf.startswith("glm"):
        return "glm"
    if leaf.startswith("qwen"):
        return "qwen"
    if leaf.startswith("gemini") or lowered.startswith("models/gemini"):
        return "gemini"
    if "fable" in leaf:
        return "claude_fable"
    if leaf.startswith("claude"):
        return "claude"
    if leaf.startswith("grok"):
        return "grok"
    if leaf.startswith("kimi") or lowered.startswith("moonshotai/"):
        return "kimi"
    if leaf.startswith("minimax") or lowered.startswith("minimax/"):
        return "minimax"
    if leaf.startswith("gpt") or lowered.startswith("openai/"):
        return "gpt"
    return "unknown"


def normalize_model_family(model: str, family: str | None) -> str:
    declared = str(family or "").strip()
    if not declared or declared == "openai":
        return infer_model_family(model)
    return declared


def get_model_family(config: dict[str, Any], model: str | None = None, provider: str | None = None) -> str:
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    families = (provider_cfg.get("models") or {}).get("families") or {}
    if selected_model in families:
        return normalize_model_family(selected_model, str(families[selected_model]))
    return infer_model_family(selected_model)


def get_model_reference_source(
    config: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
    *,
    route_profile: str | None = None,
    api_form: str | None = None,
) -> str | None:
    """Return an explicit provider/model parameter-reference override."""
    selected_model = model or get_selected_model(config, provider)
    provider_cfg = get_provider_config(config, provider)
    selected_route = get_model_route_profile(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=route_profile,
    )
    selected_form = get_model_api_form(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=selected_route,
        api_form=api_form,
    )
    form_cfg = get_model_api_forms(
        config,
        selected_model,
        provider_cfg["name"],
        route_profile=selected_route,
    )[selected_form]
    form_source = form_cfg.get("reference_source")
    if isinstance(form_source, str) and form_source.strip():
        return form_source.strip()
    reference_sources = (
        (provider_cfg.get("models") or {}).get("reference_sources") or {}
    )
    source = reference_sources.get(selected_model)
    if isinstance(source, dict):
        route_source = source.get(selected_route)
        source = (
            route_source.get(selected_form)
            if isinstance(route_source, dict)
            else source.get(selected_form)
        )
    return str(source).strip() if isinstance(source, str) and source.strip() else None


def provider_has_api_key(config: dict[str, Any], provider: str | None = None) -> bool:
    provider_cfg = get_provider_config(config, provider)
    provider_name = str(provider_cfg["name"])
    selected_key = os.getenv(SELECTED_API_KEY_ENV)
    selected_provider = os.getenv(SELECTED_API_KEY_PROVIDER_ENV)
    if selected_key and normalize_provider_name(str(selected_provider or "")) == provider_name:
        return True
    return any(bool(os.getenv(name)) for name in _api_key_env_names(provider_cfg)) or bool(
        _local_inline_api_key(provider_name)
    )


def get_image_provider_config(
    config: dict[str, Any], provider: str | None = None
) -> dict[str, Any]:
    provider_cfg = get_provider_config(config, provider)
    image_cfg = provider_cfg.get("image")
    if not isinstance(image_cfg, dict) or image_cfg.get("enabled") is not True:
        raise ValueError(
            f"Provider {provider_cfg['name']!r} does not enable image generation."
        )
    result = dict(image_cfg)
    result["provider"] = provider_cfg["name"]
    result["provider_label"] = provider_cfg.get("label") or provider_cfg["name"]
    return result


def _image_contract_route(
    family: str,
    api_form: str,
    provider_name: str,
    backend: str,
) -> str:
    if family == "banana":
        return (
            "google_ai_studio"
            if api_form == "gemini_interactions"
            and _infer_legacy_route_profile(provider_name, backend)
            == "google_ai_studio"
            else "provider_compat"
        )
    route = _infer_legacy_route_profile(provider_name, backend)
    return route if route != "dynamic_aggregator" else "dynamic_aggregator"


def get_image_model_config(
    config: dict[str, Any],
    provider: str,
    model: str | None = None,
    *,
    route_profile: str | None = None,
    api_form: str | None = None,
) -> dict[str, Any]:
    provider_cfg = get_provider_config(config, provider)
    image_cfg = get_image_provider_config(config, provider)
    selected = str(model or image_cfg.get("default") or "")
    for raw in image_cfg.get("models") or []:
        if not isinstance(raw, dict) or str(raw.get("id") or "") != selected:
            continue
        result = dict(raw)
        result["id"] = selected
        family = str(result.get("family") or "")
        routes = result.get("routes")
        normalized_routes: dict[str, dict[str, Any]] = {}
        if isinstance(routes, dict) and routes:
            for route_name, raw_route_cfg in routes.items():
                if not isinstance(raw_route_cfg, dict):
                    raise ValueError(
                        f"Image model {selected!r} route {route_name!r} must be an object."
                    )
                forms = raw_route_cfg.get("api_forms") or {}
                if not isinstance(forms, dict) or not forms:
                    raise ValueError(
                        f"Image model {selected!r} route {route_name!r} has no API forms."
                    )
                normalized_routes[str(route_name)] = {
                    **raw_route_cfg,
                    "api_forms": {
                        str(form): dict(settings or {})
                        for form, settings in forms.items()
                    },
                }
        else:
            default_transport = str(result.get("transport") or "")
            allowed = result.get("allowed_transports")
            allowed_transports = (
                [str(item) for item in allowed]
                if isinstance(allowed, list) and allowed
                else [default_transport]
            )
            for transport in allowed_transports:
                form = api_form_for_transport(transport, modality="image")
                route = _image_contract_route(
                    family,
                    form,
                    str(provider_cfg.get("name") or provider),
                    str(provider_cfg.get("backend") or ""),
                )
                normalized_routes.setdefault(route, {"api_forms": {}})[
                    "api_forms"
                ][form] = {"transport": transport}
        configured_default_route = str(
            result.get("default_route_profile") or ""
        ).strip()
        requested_route = str(route_profile or "").strip()
        explicit_form = str(api_form or "").strip()
        if not requested_route:
            requested_route = configured_default_route
        default_transport = str(result.get("transport") or "")
        default_form = (
            api_form_for_transport(default_transport, modality="image")
            if default_transport
            else ""
        )
        if not requested_route and len(normalized_routes) == 1:
            requested_route = next(iter(normalized_routes))
        if not requested_route:
            raise ValueError(
                f"Image model {selected!r} exposes multiple route profiles "
                f"{sorted(normalized_routes)} but default_route_profile has no "
                "selection."
            )
        if requested_route not in normalized_routes:
            raise ValueError(
                f"Image model {selected!r} does not expose route profile "
                f"{requested_route!r}; available={sorted(normalized_routes)}."
            )
        route_forms = normalized_routes[requested_route]["api_forms"]
        requested_form = explicit_form
        defaults = result.get("default_api_forms") or {}
        if not requested_form and isinstance(defaults, dict):
            requested_form = str(defaults.get(requested_route) or "").strip()
        if not requested_form and default_form in route_forms:
            requested_form = default_form
        if not requested_form and len(route_forms) == 1:
            requested_form = next(iter(route_forms))
        if requested_form not in route_forms:
            raise ValueError(
                f"Image API form {requested_form!r} is not allowed for model "
                f"{selected!r} on route {requested_route!r}; "
                f"available={sorted(route_forms)}."
            )
        form_cfg = route_forms[requested_form]
        transport = str(
            form_cfg.get("transport")
            or IMAGE_TRANSPORT_BY_API_FORM.get(requested_form)
            or ""
        )
        result["routes"] = normalized_routes
        result["default_route_profile"] = (
            configured_default_route or requested_route
        )
        result["route_profile"] = requested_route
        result["api_form"] = requested_form
        result["transport"] = transport
        result["api_forms"] = list(route_forms)
        result["allowed_transports"] = [
            str(
                settings.get("transport")
                or IMAGE_TRANSPORT_BY_API_FORM.get(form)
                or ""
            )
            for form, settings in route_forms.items()
        ]
        return result
    raise ValueError(
        f"Image model {selected!r} is not configured for provider {provider!r}."
    )


def get_image_endpoint(
    config: dict[str, Any],
    provider: str,
    transport: str,
) -> str:
    interface_name = IMAGE_TRANSPORT_INTERFACES.get(str(transport))
    if not interface_name:
        raise ValueError(f"Unsupported image transport: {transport!r}.")
    interface = get_provider_interface(config, interface_name, provider)
    auth = str(interface.get("auth") or "")
    allowed_auth = IMAGE_TRANSPORT_AUTH_MODES[str(transport)]
    if auth not in allowed_auth:
        requirement = (
            "bearer auth"
            if allowed_auth == {"bearer"}
            else f"one of {sorted(allowed_auth)} auth"
        )
        raise ValueError(
            f"Image interface {interface_name!r} for provider {provider!r} must use "
            f"{requirement}."
        )
    base_url = str(interface.get("base_url") or "").rstrip("/")
    path = str(interface.get("path") or DEFAULT_INTERFACE_PATHS[interface_name])
    endpoint = base_url + path
    _validate_image_endpoint(endpoint)
    return endpoint


def get_image_auth_mode(
    config: dict[str, Any],
    provider: str,
    transport: str,
) -> str:
    interface_name = IMAGE_TRANSPORT_INTERFACES.get(str(transport))
    if not interface_name:
        raise ValueError(f"Unsupported image transport: {transport!r}.")
    interface = get_provider_interface(config, interface_name, provider)
    auth = str(interface.get("auth") or "")
    allowed_auth = IMAGE_TRANSPORT_AUTH_MODES[str(transport)]
    if auth not in allowed_auth:
        requirement = (
            "bearer auth"
            if allowed_auth == {"bearer"}
            else f"one of {sorted(allowed_auth)} auth"
        )
        raise ValueError(
            f"Image interface {interface_name!r} for provider {provider!r} must use "
            f"{requirement}."
        )
    return auth


def image_provider_has_api_key(
    config: dict[str, Any], provider: str | None = None
) -> bool:
    get_image_provider_config(config, provider)
    return provider_has_api_key(config, provider)


def list_image_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for name in (config.get("providers") or {}):
        try:
            provider_cfg = get_provider_config(config, name)
            image_cfg = get_image_provider_config(config, name)
        except (KeyError, ValueError):
            continue
        models = []
        for raw in image_cfg.get("models") or []:
            if not isinstance(raw, dict):
                continue
            model = get_image_model_config(config, name, str(raw.get("id") or ""))
            routes: dict[str, Any] = {}
            for route, route_cfg in (model.get("routes") or {}).items():
                forms: dict[str, Any] = {}
                for form, form_cfg in (route_cfg.get("api_forms") or {}).items():
                    transport = str(
                        (form_cfg or {}).get("transport")
                        or IMAGE_TRANSPORT_BY_API_FORM.get(str(form))
                        or ""
                    )
                    forms[str(form)] = {
                        "api_form": str(form),
                        "transport": transport,
                    }
                routes[str(route)] = {
                    "route_profile": str(route),
                    "default_api_form": str(
                        (model.get("default_api_forms") or {}).get(route)
                        or (
                            model.get("api_form")
                            if route == model.get("route_profile")
                            else ""
                        )
                        or (next(iter(forms)) if len(forms) == 1 else "")
                    ),
                    "api_forms": forms,
                }
            models.append(
                {
                    "id": model["id"],
                    "family": model["family"],
                    "default_route_profile": model["route_profile"],
                    "routes": routes,
                    "api_form": model["api_form"],
                    "api_forms": model["api_forms"],
                    "transport": model["transport"],
                    "allowed_transports": model["allowed_transports"],
                }
            )
        providers.append(
            {
                "name": name,
                "label": provider_cfg.get("label") or name,
                "default_model": image_cfg.get("default"),
                "models": models,
                "has_key": provider_has_api_key(config, name),
            }
        )
    return providers


def list_public_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for name, raw_cfg in (config.get("providers") or {}).items():
        cfg = dict(raw_cfg or {})
        models = cfg.get("models") or {}
        providers.append(
            {
                "name": name,
                "label": cfg.get("label") or name,
                "base_url": cfg.get("base_url"),
                "backend": cfg.get("backend"),
                "default_transport": cfg.get("default_transport"),
                "api_interfaces": cfg.get("api_interfaces") or {},
                "models": models,
                "has_key": provider_has_api_key(config, name),
                "api_key_env": cfg.get("api_key_env"),
            }
        )
    return providers


def resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_model_api_forms(
    raw: Any,
    *,
    fallback_transport: str = "",
) -> dict[str, dict[str, Any]]:
    """Normalize legacy scalar/list API declarations to form -> route settings."""
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(raw, str) and raw.strip():
        normalized[raw.strip()] = {}
    elif isinstance(raw, list):
        for item in raw:
            form = str(item or "").strip()
            if form:
                normalized[form] = {}
    elif isinstance(raw, dict):
        # Transitional shape: {default: ..., enabled: {form: {...}}}.
        enabled = raw.get("enabled") if "enabled" in raw else raw
        if isinstance(enabled, list):
            for item in enabled:
                form = str(item or "").strip()
                if form:
                    normalized[form] = {}
        elif isinstance(enabled, dict):
            for form, settings in enabled.items():
                if form in {"default", "enabled"}:
                    continue
                form_key = str(form or "").strip()
                if not form_key:
                    continue
                if settings is None:
                    normalized[form_key] = {}
                elif isinstance(settings, dict):
                    normalized[form_key] = dict(settings)
                else:
                    raise ValueError(
                        f"API form {form_key!r} settings must be an object."
                    )
    elif raw is not None:
        raise ValueError("Model API forms must be a string, list, or object.")
    if not normalized and fallback_transport in TEXT_API_FORM_BY_TRANSPORT:
        normalized[api_form_for_transport(fallback_transport)] = {}
    return normalized


def _normalize_model_routes(
    raw_routes: Any,
    *,
    legacy_api_forms: Any = None,
    fallback_route: str = "",
    fallback_transport: str = "",
) -> dict[str, dict[str, Any]]:
    """Normalize provider model routing to route -> API forms."""
    normalized: dict[str, dict[str, Any]] = {}
    if raw_routes is not None and not isinstance(raw_routes, dict):
        raise ValueError("Model routes must be an object.")
    for route, settings in (raw_routes or {}).items():
        route_key = str(route or "").strip()
        if not route_key:
            raise ValueError("Model route names must be non-empty strings.")
        if settings is None:
            route_cfg: dict[str, Any] = {}
        elif isinstance(settings, dict):
            route_cfg = dict(settings)
        else:
            raise ValueError(f"Route profile {route_key!r} settings must be an object.")
        route_cfg["api_forms"] = _normalize_model_api_forms(
            route_cfg.get("api_forms"),
            fallback_transport="",
        )
        normalized[route_key] = route_cfg

    # A transport-only fallback is a legacy declaration, not an additional
    # route.  Re-normalizing an already route-first provider must therefore be
    # idempotent; otherwise validation used to append a second guessed route.
    legacy_forms = _normalize_model_api_forms(
        legacy_api_forms,
        fallback_transport=fallback_transport if not normalized else "",
    )
    for form, raw_form_cfg in legacy_forms.items():
        form_cfg = dict(raw_form_cfg)
        route = str(form_cfg.pop("route_profile", None) or fallback_route).strip()
        if not route:
            raise ValueError(
                f"Legacy API form {form!r} cannot be migrated without a route profile."
            )
        route_cfg = normalized.setdefault(route, {"api_forms": {}})
        route_forms = route_cfg.setdefault("api_forms", {})
        existing = route_forms.get(form)
        if existing is not None and not isinstance(existing, dict):
            raise ValueError(
                f"Route profile {route!r} API form {form!r} settings must be an object."
            )
        route_forms[form] = deep_merge(existing or {}, form_cfg)
    return normalized


def _contract_route_for_legacy_form(
    *,
    provider_name: str,
    model_family: str,
    api_form: str,
    form_cfg: dict[str, Any],
    fallback_route: str = "",
) -> str:
    """Resolve a legacy form without inventing an upstream supplier.

    An explicit Reference Source is authoritative.  Otherwise the provider's
    declared/inferred legacy route is retained, including
    ``dynamic_aggregator``.  The model family and wire form are deliberately
    insufficient evidence for declaring a vendor-direct route.
    """
    source = str(form_cfg.get("reference_source") or "")
    if source.startswith("aliyun_") or source == "qwen_openai_compat":
        return "aliyun_maas"
    if "dynamic" in source and "aggregator" in source:
        return "dynamic_aggregator"
    if source.startswith("gemini_dynamic_") or source.startswith(
        ("gpt_dynamic_", "gpt5_dynamic_", "gpt56_dynamic_", "grok_dynamic_")
    ):
        return "dynamic_aggregator"
    if source.endswith("_aws_bedrock_messages"):
        return "aws_bedrock"
    if source.endswith("_google_vertex_messages"):
        return "google_vertex"
    if source.endswith("_openrouter"):
        return "openrouter"
    if source == "gemini_vertex_generate_content":
        return "google_vertex"
    if source in {"gemini_openai_compat", "gemini_native_generate_content"}:
        return "google_ai_studio"
    if source in {
        "claude_cloud_adapter_messages",
        "claude_fable_cloud_adapter_messages",
    }:
        return "cloud_adapter"
    if source in {"claude_openai_compat", "claude_fable_openai_compat"}:
        return "vendor_compat"
    if source in {
        "openai_chat_base",
        "kimi_openai_compat",
        "minimax_openai_compat",
        "kimi_k3_openai_compat",
        "openai_gpt5_chat",
        "openai_gpt56_chat",
        "openai_responses",
        "openai_gpt56_responses",
        "grok_responses",
        "grok_chat_completions",
        "glm_openai_compat",
        "claude_native_messages",
        "claude_fable_native_messages",
        "deepseek_chat",
    }:
        return "vendor_direct"
    declared = str(fallback_route or "").strip()
    if declared:
        return declared
    return _infer_legacy_route_profile(provider_name, "")


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _without_inline_api_keys(config: dict[str, Any]) -> dict[str, Any]:
    sanitized = deep_merge({}, config)
    for provider in (sanitized.get("providers") or {}).values():
        if isinstance(provider, dict):
            provider.pop("api_key", None)
    return sanitized


def _local_inline_api_key(provider: str) -> str | None:
    if not LOCAL_PROVIDERS_PATH.exists():
        return None
    local = _read_yaml(LOCAL_PROVIDERS_PATH)
    provider_cfg = (local.get("providers") or {}).get(provider) or {}
    value = provider_cfg.get("api_key") if isinstance(provider_cfg, dict) else None
    return str(value) if value else None


def _normalize_provider_config(
    config: dict[str, Any],
    *,
    prune_unknown_models: bool = False,
) -> None:
    providers = config.setdefault("providers", {})
    legacy_api = config.setdefault("api", {})
    legacy_models = config.setdefault("models", {})

    if "yibu" not in providers:
        providers["yibu"] = {
            "label": "YibuAPI",
            "base_url": legacy_api.get("base_url", "https://yibuapi.com/v1"),
            "backend": "openai_compatible",
            "default_transport": "chat_completions",
            "api_interfaces": {
                "chat_completions": {
                    "path": "/chat/completions",
                    "auth": "bearer",
                }
            },
            "api_key_env": "YIBU_API_KEY",
            "models": {
                "default": legacy_models.get("default", "deepseek-v4-pro"),
                "candidates": legacy_models.get("candidates", ["deepseek-v4-flash", "deepseek-v4-pro"]),
                "families": {
                    "deepseek-v4-flash": "deepseek",
                    "deepseek-v4-pro": "deepseek",
                },
            },
        }

    legacy_api.setdefault("timeout_sec", DEFAULT_TIMEOUT_SEC)

    for name, provider_cfg in providers.items():
        provider_cfg.setdefault("label", name)
        provider_cfg.setdefault(
            "route_profile",
            _infer_legacy_route_profile(name, str(provider_cfg.get("backend") or "")),
        )
        if "base_url" in provider_cfg:
            provider_cfg["base_url"] = str(provider_cfg["base_url"]).rstrip("/")
        provider_cfg.pop("timeout_sec", None)
        provider_cfg.setdefault("models", {})
        provider_cfg["models"].setdefault("candidates", [])
        provider_cfg["models"].setdefault("families", {})
        provider_cfg["models"].setdefault("transports", {})
        provider_cfg["models"].setdefault("routes", {})
        provider_cfg["models"].setdefault("default_routes", {})
        provider_cfg["models"].setdefault("default_api_forms", {})
        provider_cfg["models"].setdefault("reference_sources", {})
        candidates = [
            str(item) for item in provider_cfg["models"].get("candidates") or []
        ]
        families = provider_cfg["models"]["families"]
        transports = provider_cfg["models"]["transports"]
        routes = provider_cfg["models"]["routes"]
        legacy_api_forms = provider_cfg["models"].get("api_forms") or {}
        default_routes = provider_cfg["models"]["default_routes"]
        default_api_forms = provider_cfg["models"]["default_api_forms"]
        legacy_reference_sources = provider_cfg["models"]["reference_sources"]
        # A local provider overlay may replace candidates while inheriting the
        # public provider block. Drop inherited per-model API declarations that
        # are no longer candidates in the effective provider.
        if prune_unknown_models:
            for configured_model in list(routes):
                if str(configured_model) not in candidates:
                    routes.pop(configured_model, None)
                    default_routes.pop(configured_model, None)
                    default_api_forms.pop(configured_model, None)
        for model in candidates:
            families[model] = normalize_model_family(model, families.get(model))
            transport = str(
                transports.get(model)
                or provider_cfg.get("default_transport")
                or ""
            )
            raw_legacy_forms = legacy_api_forms.get(model)
            normalized_routes = _normalize_model_routes(
                routes.get(model),
                legacy_api_forms=raw_legacy_forms,
                fallback_route=str(provider_cfg.get("route_profile") or ""),
                fallback_transport=transport,
            )
            legacy_source = legacy_reference_sources.get(model)
            for route_name, route_cfg in normalized_routes.items():
                route_source = (
                    legacy_source.get(route_name)
                    if isinstance(legacy_source, dict)
                    else None
                )
                for form_name, form_cfg in (
                    route_cfg.get("api_forms") or {}
                ).items():
                    selected_source = (
                        route_source.get(form_name)
                        if isinstance(route_source, dict)
                        else legacy_source.get(form_name)
                        if isinstance(legacy_source, dict)
                        else legacy_source
                    )
                    if isinstance(selected_source, str) and selected_source.strip():
                        form_cfg.setdefault(
                            "reference_source", selected_source.strip()
                        )
            legacy_dynamic = normalized_routes.pop("dynamic_aggregator", None)
            if isinstance(legacy_dynamic, dict):
                for form_name, form_cfg in (
                    legacy_dynamic.get("api_forms") or {}
                ).items():
                    contract_route = _contract_route_for_legacy_form(
                        provider_name=str(name),
                        model_family=str(families[model]),
                        api_form=str(form_name),
                        form_cfg=form_cfg if isinstance(form_cfg, dict) else {},
                        fallback_route=str(provider_cfg.get("route_profile") or ""),
                    )
                    target_route = normalized_routes.setdefault(
                        contract_route, {"api_forms": {}}
                    )
                    target_forms = target_route.setdefault("api_forms", {})
                    target_forms[str(form_name)] = deep_merge(
                        target_forms.get(str(form_name)) or {},
                        form_cfg if isinstance(form_cfg, dict) else {},
                    )
            routes[model] = normalized_routes
            selected_default_route = str(default_routes.get(model) or "").strip()
            legacy_provider_route = str(provider_cfg.get("route_profile") or "").strip()
            if not selected_default_route and legacy_provider_route in normalized_routes:
                selected_default_route = legacy_provider_route
            legacy_default_form = default_api_forms.get(model)
            if isinstance(legacy_default_form, str) and legacy_default_form.strip():
                routes_for_default_form = [
                    route_name
                    for route_name, route_cfg in normalized_routes.items()
                    if legacy_default_form.strip() in (route_cfg.get("api_forms") or {})
                ]
                if not selected_default_route and len(routes_for_default_form) == 1:
                    selected_default_route = routes_for_default_form[0]
            if not selected_default_route and len(normalized_routes) == 1:
                selected_default_route = next(iter(normalized_routes))
            if selected_default_route:
                default_routes[model] = selected_default_route

            raw_defaults = default_api_forms.get(model)
            normalized_defaults: dict[str, str] = {}
            if isinstance(raw_defaults, str) and raw_defaults.strip():
                if not selected_default_route:
                    raise ValueError(
                        f"Provider {name!r} model {model!r} legacy default API form "
                        "cannot be migrated without a default route."
                    )
                normalized_defaults[selected_default_route] = raw_defaults.strip()
            elif isinstance(raw_defaults, dict):
                normalized_defaults = {
                    str(route): str(form).strip()
                    for route, form in raw_defaults.items()
                    if str(route).strip() and str(form).strip()
                }
            elif raw_defaults is not None:
                raise ValueError(
                    f"Provider {name!r} model {model!r} default API forms must be "
                    "a route-to-form object."
                )
            if isinstance(raw_legacy_forms, dict):
                inline_default = str(raw_legacy_forms.get("default") or "").strip()
                if inline_default and selected_default_route:
                    normalized_defaults.setdefault(selected_default_route, inline_default)

            for route_name, route_cfg in normalized_routes.items():
                route_forms = route_cfg.get("api_forms") or {}
                inline_route_default = str(
                    route_cfg.pop("default_api_form", None) or ""
                ).strip()
                if inline_route_default:
                    normalized_defaults.setdefault(route_name, inline_route_default)
                if route_name not in normalized_defaults and len(route_forms) == 1:
                    normalized_defaults[route_name] = next(iter(route_forms))
                legacy_source = legacy_reference_sources.get(model)
                if isinstance(legacy_source, dict):
                    route_source = legacy_source.get(route_name)
                else:
                    route_source = None
                for form_name, form_cfg in route_forms.items():
                    selected_source = (
                        route_source.get(form_name)
                        if isinstance(route_source, dict)
                        else legacy_source.get(form_name)
                        if isinstance(legacy_source, dict)
                        else legacy_source
                    )
                    if isinstance(selected_source, str) and selected_source.strip():
                        form_cfg.setdefault("reference_source", selected_source.strip())
            default_api_forms[model] = normalized_defaults
        provider_cfg["models"].pop("api_forms", None)
        provider_cfg["models"].pop("reference_sources", None)
        provider_cfg.pop("route_profile", None)
        interfaces = provider_cfg.get("api_interfaces") or {}
        for interface in interfaces.values():
            if isinstance(interface, dict) and "base_url" in interface:
                interface["base_url"] = str(interface["base_url"]).rstrip("/")

    active_provider = get_active_provider_name(config)
    if active_provider in providers:
        provider_cfg = providers[active_provider]
        legacy_api["base_url"] = str(provider_cfg.get("base_url", legacy_api.get("base_url", ""))).rstrip("/")
        provider_models = provider_cfg.get("models") or {}
        if provider_models.get("candidates"):
            legacy_models["candidates"] = provider_models["candidates"]
        legacy_models["default"] = get_selected_model(config, active_provider)


def _infer_legacy_route_profile(provider_name: str, backend: str) -> str:
    name = str(provider_name).casefold()
    if name == "aliyun_maas":
        return "aliyun_maas"
    if "openrouter" in name:
        return "openrouter"
    if "bedrock" in name:
        return "aws_bedrock"
    if "vertex" in name:
        return "google_vertex"
    if backend == "gemini_ai_studio" and name == "gemini":
        return "google_ai_studio"
    if "official" in name or name in {"moonshot_official_k3", "xai_official"}:
        return "vendor_direct"
    return "dynamic_aggregator"


def validate_provider_config(config: dict[str, Any]) -> None:
    """Fail early when provider routing would otherwise be guessed at runtime."""
    for name, raw_provider in (config.get("providers") or {}).items():
        raw_models = (raw_provider or {}).get("models") or {}
        raw_candidates = {
            str(item) for item in raw_models.get("candidates") or []
        }
        raw_families = raw_models.get("families") or {}
        missing_families = sorted(raw_candidates - set(raw_families))
        if missing_families:
            raise ValueError(
                f"providers.{name}.models.families is missing models: "
                f"{missing_families}."
            )
    # Normalize a fully detached copy.  A shallow nested copy caused this
    # validation pass to append guessed routes to the live configuration.
    normalized = copy.deepcopy(config)
    _normalize_provider_config(normalized)
    providers = normalized.get("providers") or {}
    if not providers:
        raise ValueError("config.providers must contain at least one provider.")
    for name, raw in providers.items():
        provider = raw or {}
        backend = str(provider.get("backend") or "")
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"providers.{name}.backend must be one of {sorted(SUPPORTED_BACKENDS)}."
            )
        default_transport = str(provider.get("default_transport") or "")
        if default_transport not in SUPPORTED_TRANSPORTS:
            raise ValueError(
                f"providers.{name}.default_transport must be one of {sorted(SUPPORTED_TRANSPORTS)}."
            )
        interfaces = provider.get("api_interfaces")
        if not isinstance(interfaces, dict) or not interfaces:
            raise ValueError(f"providers.{name}.api_interfaces must be a non-empty object.")
        models = provider.get("models") or {}
        candidates = [str(item) for item in models.get("candidates") or []]
        default_model = models.get("default")
        if default_model and str(default_model) not in candidates:
            raise ValueError(
                f"providers.{name}.models.default must be included in models.candidates."
            )
        families = models.get("families") or {}
        missing_families = sorted(set(candidates) - set(families))
        if missing_families:
            raise ValueError(
                f"providers.{name}.models.families is missing models: {missing_families}."
            )
        invalid_families = sorted(
            {
                str(family)
                for family in families.values()
                if str(family) not in SUPPORTED_MODEL_FAMILIES
            }
        )
        if invalid_families:
            raise ValueError(
                f"providers.{name}.models.families contains unsupported families: {invalid_families}."
            )
        model_transports = models.get("transports") or {}
        unknown_models = sorted(set(model_transports) - set(candidates))
        if unknown_models:
            raise ValueError(
                f"providers.{name}.models.transports contains unknown models: {unknown_models}."
            )
        model_routes = models.get("routes") or {}
        unknown_route_models = sorted(set(model_routes) - set(candidates))
        if unknown_route_models:
            raise ValueError(
                f"providers.{name}.models.routes contains unknown models: "
                f"{unknown_route_models}."
            )
        default_routes = models.get("default_routes") or {}
        unknown_default_route_models = sorted(set(default_routes) - set(candidates))
        if unknown_default_route_models:
            raise ValueError(
                f"providers.{name}.models.default_routes contains unknown models: "
                f"{unknown_default_route_models}."
            )
        default_api_forms = models.get("default_api_forms") or {}
        unknown_default_models = sorted(set(default_api_forms) - set(candidates))
        if unknown_default_models:
            raise ValueError(
                f"providers.{name}.models.default_api_forms contains unknown models: "
                f"{unknown_default_models}."
            )
        required_transports = {default_transport}
        for model in candidates:
            routes = model_routes.get(model)
            if not isinstance(routes, dict) or not routes:
                raise ValueError(
                    f"providers.{name} model {model!r} must expose at least one route profile."
                )
            default_route = str(default_routes.get(model) or "").strip()
            if not default_route:
                raise ValueError(
                    f"providers.{name} model {model!r} must define a default route profile."
                )
            if default_route not in routes:
                raise ValueError(
                    f"providers.{name} model {model!r} default route profile "
                    f"{default_route!r} is not enabled."
                )
            model_defaults = default_api_forms.get(model)
            if not isinstance(model_defaults, dict):
                raise ValueError(
                    f"providers.{name} model {model!r} default API forms must be "
                    "a route-to-form object."
                )
            for route_profile, route_cfg in routes.items():
                if not str(route_profile).strip() or not isinstance(route_cfg, dict):
                    raise ValueError(
                        f"providers.{name} model {model!r} route {route_profile!r} "
                        "settings must be an object with a non-empty route name."
                    )
                forms = route_cfg.get("api_forms") or {}
                if not isinstance(forms, dict) or not forms:
                    raise ValueError(
                        f"providers.{name} model {model!r} route {route_profile!r} "
                        "must expose at least one API form."
                    )
                default_form = str(model_defaults.get(route_profile) or "").strip()
                if not default_form:
                    raise ValueError(
                        f"providers.{name} model {model!r} route {route_profile!r} "
                        "must define a default API form."
                    )
                if default_form not in forms:
                    raise ValueError(
                        f"providers.{name} model {model!r} route {route_profile!r} "
                        f"default API form {default_form!r} is not enabled."
                    )
                for api_form, form_cfg in forms.items():
                    if str(api_form) not in SUPPORTED_API_FORMS:
                        raise ValueError(
                            f"providers.{name} model {model!r} route {route_profile!r} "
                            f"contains unsupported API form {api_form!r}."
                        )
                    if not isinstance(form_cfg, dict):
                        raise ValueError(
                            f"providers.{name} model {model!r} route {route_profile!r} "
                            f"API form {api_form!r} settings must be an object."
                        )
                    reference_source = form_cfg.get("reference_source")
                    if reference_source is not None and not str(reference_source).strip():
                        raise ValueError(
                            f"providers.{name} model {model!r} route {route_profile!r} "
                            f"API form {api_form!r} reference_source must be non-empty."
                        )
                    required_transports.add(transport_for_api_form(str(api_form)))
            legacy_transport = str(model_transports.get(model) or "")
            if legacy_transport:
                legacy_form = api_form_for_transport(legacy_transport)
                default_form = str(model_defaults.get(default_route) or "")
                if legacy_form != default_form:
                    raise ValueError(
                        f"providers.{name} model {model!r} legacy transport "
                        f"{legacy_transport!r} conflicts with default API form "
                        f"{default_form!r}."
                    )
        for transport in required_transports:
            if transport not in SUPPORTED_TRANSPORTS:
                raise ValueError(
                    f"providers.{name} contains unsupported model transport {transport!r}."
                )
            interface = interfaces.get(transport)
            if not isinstance(interface, dict):
                raise ValueError(
                    f"providers.{name}.api_interfaces.{transport} is required by configured models."
                )
            base_url = str(interface.get("base_url") or provider.get("base_url") or "")
            if not base_url:
                raise ValueError(
                    f"providers.{name}.api_interfaces.{transport} requires base_url or provider base_url."
                )
            auth = str(interface.get("auth") or "")
            if auth not in SUPPORTED_AUTH_MODES:
                raise ValueError(
                    f"providers.{name}.api_interfaces.{transport}.auth must be one of {sorted(SUPPORTED_AUTH_MODES)}."
                )
            path = str(interface.get("path") or DEFAULT_INTERFACE_PATHS[transport])
            if not path.startswith("/"):
                raise ValueError(
                    f"providers.{name}.api_interfaces.{transport}.path must start with '/'."
                )
        _validate_image_capability(name, provider)


def _validate_image_capability(name: str, provider: dict[str, Any]) -> None:
    image_cfg = provider.get("image")
    if image_cfg is None:
        return
    if not isinstance(image_cfg, dict):
        raise ValueError(f"providers.{name}.image must be an object.")
    enabled = image_cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"providers.{name}.image.enabled must be a boolean.")
    if not enabled:
        return
    models = image_cfg.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError(f"providers.{name}.image.models must be a non-empty list.")
    seen: set[str] = set()
    required_interfaces: set[str] = set()
    for index, raw in enumerate(models):
        prefix = f"providers.{name}.image.models[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be an object.")
        model_id = str(raw.get("id") or "")
        if not model_id:
            raise ValueError(f"{prefix}.id is required.")
        if model_id in seen:
            raise ValueError(f"providers.{name}.image contains duplicate model {model_id!r}.")
        seen.add(model_id)
        family = str(raw.get("family") or "")
        if family not in SUPPORTED_IMAGE_FAMILIES:
            raise ValueError(
                f"{prefix}.family must be one of {sorted(SUPPORTED_IMAGE_FAMILIES)}."
            )
        raw_routes = raw.get("routes")
        if isinstance(raw_routes, dict) and raw_routes:
            default_route = str(raw.get("default_route_profile") or "")
            if default_route not in raw_routes:
                raise ValueError(
                    f"{prefix}.default_route_profile must name an enabled route."
                )
            default_forms = raw.get("default_api_forms") or {}
            if not isinstance(default_forms, dict):
                raise ValueError(
                    f"{prefix}.default_api_forms must be a route-to-form object."
                )
            allowed = []
            for route, route_cfg in raw_routes.items():
                if not isinstance(route_cfg, dict):
                    raise ValueError(f"{prefix}.routes.{route} must be an object.")
                forms = route_cfg.get("api_forms") or {}
                if not isinstance(forms, dict) or not forms:
                    raise ValueError(
                        f"{prefix}.routes.{route}.api_forms must be non-empty."
                    )
                default_form = str(default_forms.get(route) or "")
                if default_form not in forms:
                    raise ValueError(
                        f"{prefix}.default_api_forms.{route} must name an enabled form."
                    )
                for api_form, form_cfg in forms.items():
                    if api_form not in SUPPORTED_API_FORMS:
                        raise ValueError(
                            f"{prefix}.routes.{route} contains unsupported API form "
                            f"{api_form!r}."
                        )
                    if not isinstance(form_cfg, dict):
                        raise ValueError(
                            f"{prefix}.routes.{route}.api_forms.{api_form} must be an object."
                        )
                    transport = str(
                        form_cfg.get("transport")
                        or IMAGE_TRANSPORT_BY_API_FORM.get(str(api_form))
                        or ""
                    )
                    expected_api_form = api_form_for_transport(
                        transport, modality="image"
                    )
                    if api_form != expected_api_form:
                        raise ValueError(
                            f"{prefix} API form {api_form!r} does not match transport "
                            f"{transport!r} ({expected_api_form!r})."
                        )
                    allowed.append(transport)
        else:
            transport = str(raw.get("transport") or "")
            api_form = str(
                raw.get("api_form")
                or api_form_for_transport(transport, modality="image")
            )
            if api_form not in SUPPORTED_API_FORMS:
                raise ValueError(
                    f"{prefix}.api_form must be one of {sorted(SUPPORTED_API_FORMS)}."
                )
            expected_api_form = api_form_for_transport(transport, modality="image")
            if api_form != expected_api_form:
                raise ValueError(
                    f"{prefix}.api_form {api_form!r} does not match transport "
                    f"{transport!r} ({expected_api_form!r})."
                )
            allowed_raw = raw.get("allowed_transports")
            if allowed_raw is not None and (
                not isinstance(allowed_raw, list) or not allowed_raw
            ):
                raise ValueError(
                    f"{prefix}.allowed_transports must be a non-empty list."
                )
            allowed = (
                [str(item) for item in allowed_raw]
                if allowed_raw
                else [transport]
            )
            if len(set(allowed)) != len(allowed):
                raise ValueError(
                    f"{prefix}.allowed_transports must not contain duplicates."
                )
            if transport not in allowed:
                raise ValueError(
                    f"{prefix}.allowed_transports must include transport."
                )
        invalid = sorted(set(allowed) - SUPPORTED_IMAGE_TRANSPORTS)
        if invalid:
            raise ValueError(f"{prefix} contains unsupported transports: {invalid}.")
        banana_only = sorted(
            set(allowed) & {"chat-completions", "gemini-interactions"}
        )
        if family != "banana" and banana_only:
            raise ValueError(
                f"{prefix} allows Banana-only transports {banana_only} for a non-Banana model."
            )
        required_interfaces.update(IMAGE_TRANSPORT_INTERFACES[item] for item in allowed)
    default_model = str(image_cfg.get("default") or "")
    if default_model not in seen:
        raise ValueError(
            f"providers.{name}.image.default must reference a configured image model."
        )
    interfaces = provider.get("api_interfaces") or {}
    for interface_name in sorted(required_interfaces):
        interface = interfaces.get(interface_name)
        if not isinstance(interface, dict):
            raise ValueError(
                f"providers.{name}.api_interfaces.{interface_name} is required by image models."
            )
        transports = [
            transport
            for transport, mapped_interface in IMAGE_TRANSPORT_INTERFACES.items()
            if mapped_interface == interface_name
        ]
        allowed_auth = set().union(
            *(IMAGE_TRANSPORT_AUTH_MODES[transport] for transport in transports)
        )
        if str(interface.get("auth") or "") not in allowed_auth:
            requirement = (
                "bearer"
                if allowed_auth == {"bearer"}
                else f"one of {sorted(allowed_auth)}"
            )
            raise ValueError(
                f"providers.{name}.api_interfaces.{interface_name}.auth must be "
                f"{requirement} for image tests."
            )
        path = str(interface.get("path") or DEFAULT_INTERFACE_PATHS[interface_name])
        if not path.startswith("/"):
            raise ValueError(
                f"providers.{name}.api_interfaces.{interface_name}.path must start with '/'."
            )
        base_url = str(interface.get("base_url") or provider.get("base_url") or "")
        _validate_image_endpoint(base_url.rstrip("/") + path)


def _validate_image_endpoint(endpoint: str) -> None:
    parsed = urlsplit(str(endpoint))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid image provider endpoint: {endpoint!r}.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Image provider endpoint must not contain user information, a query, or a fragment."
        )
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote image provider endpoints must use HTTPS.")


def _api_key_env_names(provider_cfg: dict[str, Any]) -> list[str]:
    configured = provider_cfg.get("api_key_env")
    names: list[str]
    if isinstance(configured, list):
        names = [str(item) for item in configured]
    elif configured:
        names = [str(configured)]
    else:
        names = []
    if not names and str(provider_cfg.get("name") or "") == "yibu":
        names.extend(LEGACY_API_KEY_ENV_NAMES)
    return names


_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd])")
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()
    if text.isdigit():
        return float(text)

    total = 0.0
    consumed = ""
    for match in _DURATION_RE.finditer(text):
        consumed += match.group(0)
        total += float(match.group("value")) * _DURATION_MULTIPLIERS[match.group("unit")]

    if not consumed or consumed != text.replace(" ", ""):
        raise ValueError(f"Unsupported duration: {value!r}. Use values like 30s, 5m, or 1h.")
    return total


def now_run_id(prefix: str) -> str:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}"
