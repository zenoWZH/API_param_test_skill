from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT, get_provider_config


def count_semantic_tokens(
    config: dict[str, Any],
    *,
    provider: str | None,
    model: str | None,
    input_text: str,
    output_text: str,
    transport: str | None = None,
) -> dict[str, Any]:
    spec = _counter_spec(config, provider, model)
    if not spec:
        return {
            "source": None,
            "kind": None,
            "input": _unavailable("no model token counter is configured"),
            "output": _unavailable("no model token counter is configured"),
        }
    try:
        counter, source = _load_counter(spec, model)
    except Exception as exc:
        note = f"token counter unavailable: {exc.__class__.__name__}: {exc}"
        return {
            "source": _counter_source(spec, model),
            "kind": str(spec.get("kind") or "unknown"),
            "input": _unavailable(note),
            "output": _unavailable(note),
        }

    exact_dimensions = {
        str(item)
        for item in spec.get("exact_dimensions", [])
        if str(item) in {"input", "output"}
    }
    exact_transport = _transport_exactness_is_declared(spec, transport)
    return {
        "source": source,
        "kind": str(spec.get("kind") or "unknown"),
        "input": _count_dimension(
            counter,
            input_text,
            exact="input" in exact_dimensions and exact_transport,
            note=_evidence_note("input", exact_dimensions, transport, exact_transport),
        ),
        "output": _count_dimension(
            counter,
            output_text,
            exact="output" in exact_dimensions and exact_transport,
            note=_evidence_note("output", exact_dimensions, transport, exact_transport),
        ),
    }


def _counter_spec(
    config: dict[str, Any], provider: str | None, model: str | None
) -> dict[str, Any] | None:
    candidates: list[Any] = []
    if provider:
        try:
            provider_cfg = get_provider_config(config, provider)
        except Exception:
            provider_cfg = {}
        models = provider_cfg.get("models") if isinstance(provider_cfg, dict) else None
        counters = models.get("token_counters") if isinstance(models, dict) else None
        if isinstance(counters, dict):
            candidates.extend([counters.get(model), counters.get("*")])
    settings = ((config.get("test_cases") or {}).get("token_accuracy") or {})
    counters = settings.get("counters") if isinstance(settings, dict) else None
    if isinstance(counters, dict):
        candidates.extend([counters.get(model), counters.get("*")])
    return next((dict(item) for item in candidates if isinstance(item, dict)), None)


def _load_counter(
    spec: dict[str, Any], model: str | None
) -> tuple[Callable[[str], int], str]:
    kind = str(spec.get("kind") or "").strip().lower()
    if kind == "tiktoken":
        import tiktoken  # type: ignore[import-not-found]

        encoding_name = spec.get("encoding")
        if encoding_name:
            encoding = tiktoken.get_encoding(str(encoding_name))
        else:
            target_model = str(spec.get("model") or model or "")
            if not target_model:
                raise ValueError("tiktoken counter requires model or encoding")
            encoding = tiktoken.encoding_for_model(target_model)
        return lambda text: len(encoding.encode(text)), _counter_source(spec, model)
    if kind == "tokenizer_json":
        from tokenizers import Tokenizer  # type: ignore[import-not-found]

        raw_path = str(spec.get("path") or "")
        if not raw_path:
            raise ValueError("tokenizer_json counter requires path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        tokenizer = Tokenizer.from_file(str(resolved))
        return lambda text: len(tokenizer.encode(text).ids), str(resolved)
    raise ValueError(f"unsupported token counter kind: {kind or 'missing'}")


def _count_dimension(
    counter: Callable[[str], int],
    text: str,
    *,
    exact: bool,
    note: str | None,
) -> dict[str, Any]:
    try:
        tokens = counter(text) if text else 0
    except Exception as exc:
        return _unavailable(
            f"token counter failed: {exc.__class__.__name__}: {exc}"
        )
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        return _unavailable(
            "token counter returned an invalid value; expected a non-negative int, "
            f"got {type(tokens).__name__}"
        )
    return {
        "tokens": tokens,
        "evidence_level": "exact" if exact else "estimate",
        "note": None if exact else note,
    }


def _transport_exactness_is_declared(
    spec: dict[str, Any], transport: str | None
) -> bool:
    # Calls made before transport-aware auditing keep their historical behavior.
    if transport is None:
        return True
    configured = spec.get("exact_transports", [])
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple, set)):
        return False
    declared = {str(item).strip().lower() for item in configured}
    return "*" in declared or transport.strip().lower() in declared


def _evidence_note(
    dimension: str,
    exact_dimensions: set[str],
    transport: str | None,
    exact_transport: bool,
) -> str | None:
    if dimension not in exact_dimensions:
        return "counter does not include a declared exact protocol template"
    if transport is not None and not exact_transport:
        return (
            f"counter exactness is not declared for transport {transport!r}; "
            "exact_transports must include that transport or '*'"
        )
    return None


def _unavailable(note: str) -> dict[str, Any]:
    return {"tokens": None, "evidence_level": "unavailable", "note": note}


def _counter_source(spec: dict[str, Any], model: str | None) -> str:
    return str(
        spec.get("encoding")
        or spec.get("path")
        or spec.get("model")
        or model
        or spec.get("kind")
        or "unknown"
    )
