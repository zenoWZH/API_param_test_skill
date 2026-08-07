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
    return {
        "source": source,
        "kind": str(spec.get("kind") or "unknown"),
        "input": _count_dimension(counter, input_text, "input" in exact_dimensions),
        "output": _count_dimension(counter, output_text, "output" in exact_dimensions),
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
    counter: Callable[[str], int], text: str, exact: bool
) -> dict[str, Any]:
    tokens = int(counter(text)) if text else 0
    return {
        "tokens": tokens,
        "evidence_level": "exact" if exact else "estimate",
        "note": None if exact else "counter does not include a declared exact protocol template",
    }


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
