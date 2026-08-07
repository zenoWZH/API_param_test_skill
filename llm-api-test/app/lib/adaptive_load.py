from __future__ import annotations

import math
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import resolve_project_path
from .deepseek_params import build_request
from .token_audit import normalize_usage


DEFAULT_CONTEXT_WINDOW_TOKENS = 131_072


def estimated_text_token_units(value: Any) -> float:
    """Cheap tokenizer-independent estimate used until provider usage calibrates it."""
    if isinstance(value, str):
        ascii_chars = sum(1 for char in value if ord(char) < 128)
        return (ascii_chars / 4.0) + (len(value) - ascii_chars)
    if isinstance(value, dict):
        return sum(
            estimated_text_token_units(key) + estimated_text_token_units(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(estimated_text_token_units(item) for item in value)
    return 0.0


def estimate_request_tokens(body: dict[str, Any], expected_completion_tokens: float | None = None) -> int:
    prompt_units = estimate_prompt_token_units(body)
    completion_tokens = expected_completion_tokens
    if completion_tokens is None:
        completion_tokens = body.get("max_completion_tokens")
    if completion_tokens is None:
        completion_tokens = body.get("max_tokens")
    try:
        completion_budget = max(float(completion_tokens or 0), 0.0)
    except (TypeError, ValueError):
        completion_budget = 0.0
    return max(int(math.ceil(prompt_units + completion_budget)), 1)


def estimate_prompt_token_units(body: dict[str, Any]) -> float:
    units = estimated_text_token_units(body.get("messages") or [])
    units += estimated_text_token_units(body.get("tools") or [])
    return units


def resolve_context_window(
    config: dict[str, Any],
    provider_config: dict[str, Any],
    model: str,
) -> tuple[int, str]:
    adaptive = config.get("adaptive_load") or {}
    fallback = _positive_int(
        adaptive.get("fallback_context_window_tokens"),
        DEFAULT_CONTEXT_WINDOW_TOKENS,
    )
    models = provider_config.get("models") or {}
    configured = models.get("context_windows") or {}
    if isinstance(configured, dict):
        value = configured.get(model)
        if value is None:
            value = configured.get("default")
        parsed = _positive_int(value, 0)
        if parsed:
            return parsed, f"providers.{provider_config.get('name', 'provider')}.models.context_windows"
    return fallback, "adaptive_load.fallback_context_window_tokens"


def filter_context_unsafe_profiles(
    config: dict[str, Any],
    provider_config: dict[str, Any],
    model: str,
    entries: list[tuple[str, str, int]],
) -> tuple[list[tuple[str, str, int]], list[dict[str, Any]]]:
    """Remove static throughput profiles that cannot fit the model context window."""
    adaptive = config.get("adaptive_load") or {}
    if not bool(adaptive.get("filter_context_unsafe_profiles", True)):
        return list(entries), []

    context_window_tokens, context_window_source = resolve_context_window(
        config,
        provider_config,
        model,
    )
    safety_ratio = _bounded_float(
        adaptive.get("context_safety_ratio"),
        0.95,
        0.1,
        1.0,
    )
    safe_context_tokens = max(
        int(math.floor(context_window_tokens * safety_ratio)),
        1,
    )
    allowed: list[tuple[str, str, int]] = []
    skipped: list[dict[str, Any]] = []

    for group, profile, weight in entries:
        if group not in {"throughput_profiles", "qwen_throughput_profiles"}:
            allowed.append((group, profile, weight))
            continue
        body = build_request(config, group, profile).body
        estimated_tokens = estimate_request_tokens(body)
        if estimated_tokens <= safe_context_tokens:
            allowed.append((group, profile, weight))
            continue
        skipped.append(
            {
                "group": group,
                "profile": profile,
                "estimated_tokens": estimated_tokens,
                "safe_context_tokens": safe_context_tokens,
                "context_window_tokens": context_window_tokens,
                "context_window_source": context_window_source,
            }
        )

    if entries and not allowed:
        raise ValueError(
            "All workload profiles exceed the safe context limit "
            f"({safe_context_tokens} tokens)."
        )
    return allowed, skipped


def rebalance_band_targets(
    target_mean: float,
    bands: list[dict[str, Any]],
    minimum: float,
    maximum: float,
) -> tuple[list[float], bool]:
    """Clip band values and redistribute remaining headroom to preserve the weighted mean."""
    if not bands:
        raise ValueError("adaptive_load.bands must not be empty")
    if maximum < minimum:
        maximum = minimum
    desired_mean = min(max(float(target_mean), minimum), maximum)
    values = [
        min(max(float(item.get("ratio", 1.0)) * float(target_mean), minimum), maximum)
        for item in bands
    ]
    weights = [max(float(item.get("weight", 0)), 0.0) for item in bands]
    if sum(weights) <= 0:
        raise ValueError("adaptive_load.bands must contain a positive weight")

    for _ in range(len(values) + 2):
        current = _weighted_mean(values, weights)
        delta = desired_mean - current
        if abs(delta) < 1e-6:
            break
        candidates = [
            index
            for index, value in enumerate(values)
            if (delta > 0 and value < maximum - 1e-9)
            or (delta < 0 and value > minimum + 1e-9)
        ]
        candidate_weight = sum(weights[index] for index in candidates)
        if candidate_weight <= 0:
            break
        adjustment = delta * sum(weights) / candidate_weight
        for index in candidates:
            values[index] = min(max(values[index] + adjustment, minimum), maximum)

    unreachable = not math.isclose(desired_mean, float(target_mean), rel_tol=0, abs_tol=1e-6)
    return values, unreachable


@dataclass
class AdaptiveRequestPlan:
    band: str
    target_total_tokens: float
    target_prompt_tokens: float
    estimated_prompt_tokens: int
    estimated_total_tokens: int
    context_window_tokens: int
    context_window_source: str
    context_clamped: bool
    warnings: list[str] = field(default_factory=list)
    estimated_prompt_units: float = 0.0

    def record_extra(self) -> dict[str, Any]:
        return {
            "adaptive_band": self.band,
            "adaptive_band_target_tokens": self.target_total_tokens,
            "target_prompt_tokens": self.target_prompt_tokens,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "context_window_tokens": self.context_window_tokens,
            "context_window_source": self.context_window_source,
            "context_clamped": self.context_clamped,
            "adaptive_warnings": list(self.warnings),
        }


class AdaptiveLengthController:
    def __init__(
        self,
        config: dict[str, Any],
        provider_config: dict[str, Any],
        model: str,
        target_tokens_per_request: float,
    ) -> None:
        if target_tokens_per_request <= 0:
            raise ValueError("target_tokens_per_request must be positive")
        cfg = config.get("adaptive_load") or {}
        self.target_tokens_per_request = float(target_tokens_per_request)
        self.context_window_tokens, self.context_window_source = resolve_context_window(
            config, provider_config, model
        )
        self.context_safety_ratio = _bounded_float(cfg.get("context_safety_ratio"), 0.95, 0.1, 1.0)
        self.ema_alpha = _bounded_float(cfg.get("ema_alpha"), 0.2, 0.01, 1.0)
        self.tolerance_ratio = _bounded_float(cfg.get("tolerance_ratio"), 0.10, 0.0, 1.0)
        self.min_samples = _positive_int(cfg.get("min_samples"), 20)
        self.usage_coverage_warn_below = _bounded_float(
            cfg.get("usage_coverage_warn_below"), 0.90, 0.0, 1.0
        )
        self.minimum_user_prompt_tokens = _positive_int(
            cfg.get("minimum_user_prompt_tokens"), 16
        )
        self.predicted_completion_tokens = float(
            _positive_int(cfg.get("initial_completion_tokens"), 64)
        )
        raw_bands = cfg.get("bands") or [
            {"name": "short", "ratio": 0.5, "weight": 25},
            {"name": "target", "ratio": 1.0, "weight": 50},
            {"name": "long", "ratio": 1.5, "weight": 25},
        ]
        self.bands = [dict(item) for item in raw_bands if isinstance(item, dict)]
        if not self.bands:
            raise ValueError("adaptive_load.bands must contain at least one band")
        self._band_schedule = _weighted_schedule(self.bands)
        self._corpus = _load_corpus(cfg.get("corpus_fixtures"))
        self._prompt_tokens_per_unit = 1.0
        self._band_index = 0
        self._corpus_offset = 0
        self._request_sequence = 0
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, int, float]] = deque()
        self._attempt_count = 0
        self._usage_count = 0
        self._clamped_count = 0

    def apply_to_body(self, body: dict[str, Any]) -> AdaptiveRequestPlan:
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Adaptive request sizing requires a chat-completions messages list")
        user_message = next(
            (
                item
                for item in reversed(messages)
                if isinstance(item, dict) and item.get("role") == "user"
            ),
            None,
        )
        if user_message is None or not isinstance(user_message.get("content"), str):
            raise ValueError("Adaptive request sizing requires a text user message")

        with self._lock:
            band_name = self._band_schedule[self._band_index % len(self._band_schedule)]
            self._band_index += 1
            band_index = next(
                index for index, item in enumerate(self.bands)
                if str(item.get("name") or f"band_{index}") == band_name
            )
            max_output_tokens = _max_output_tokens(body)
            safe_context = max(
                int(math.floor(self.context_window_tokens * self.context_safety_ratio)),
                1,
            )
            old_content = str(user_message["content"])
            fixed_units = max(
                estimate_prompt_token_units(body) - estimated_text_token_units(old_content),
                0.0,
            )
            fixed_prompt_tokens = fixed_units * self._prompt_tokens_per_unit
            minimum_total = (
                fixed_prompt_tokens
                + self.minimum_user_prompt_tokens
                + self.predicted_completion_tokens
            )
            safe_prompt_tokens = max(safe_context - max_output_tokens, fixed_prompt_tokens)
            maximum_total = safe_prompt_tokens + self.predicted_completion_tokens
            band_targets, globally_clamped = rebalance_band_targets(
                self.target_tokens_per_request,
                self.bands,
                minimum_total,
                maximum_total,
            )
            target_total = band_targets[band_index]
            target_prompt = min(
                max(target_total - self.predicted_completion_tokens, fixed_prompt_tokens),
                safe_prompt_tokens,
            )
            desired_user_tokens = max(target_prompt - fixed_prompt_tokens, 0.0)
            desired_user_units = desired_user_tokens / max(self._prompt_tokens_per_unit, 1e-6)
            prefix = (
                f"[loadtest request={self._request_sequence} nonce={uuid.uuid4().hex[:12]}]\n"
            )
            suffix = "\n\n问题：请用一句话总结以上内容。"
            self._request_sequence += 1
            fixed_user_units = estimated_text_token_units(prefix + suffix)
            corpus_units = max(desired_user_units - fixed_user_units, 0.0)
            corpus_text, self._corpus_offset = _slice_corpus_by_units(
                self._corpus,
                corpus_units,
                self._corpus_offset,
            )
            user_message["content"] = prefix + corpus_text + suffix

            estimated_prompt_units = estimate_prompt_token_units(body)
            estimated_prompt_tokens = max(
                int(round(estimated_prompt_units * self._prompt_tokens_per_unit)),
                1,
            )
            estimated_total = max(
                int(round(estimated_prompt_tokens + self.predicted_completion_tokens)),
                1,
            )
            context_clamped = (
                globally_clamped
                or target_total >= maximum_total - 1e-6
                or target_total <= minimum_total + 1e-6
            )
            warnings: list[str] = []
            if self.context_window_source == "adaptive_load.fallback_context_window_tokens":
                warnings.append(
                    f"No model context window configured; using fallback {self.context_window_tokens}."
                )
            if globally_clamped:
                warnings.append(
                    f"Requested {self.target_tokens_per_request:.0f} tokens/request is outside "
                    f"the safe range {minimum_total:.0f}..{maximum_total:.0f}; band targets were clamped."
                )
            if context_clamped:
                self._clamped_count += 1

            return AdaptiveRequestPlan(
                band=band_name,
                target_total_tokens=target_total,
                target_prompt_tokens=target_prompt,
                estimated_prompt_tokens=estimated_prompt_tokens,
                estimated_total_tokens=estimated_total,
                estimated_prompt_units=estimated_prompt_units,
                context_window_tokens=self.context_window_tokens,
                context_window_source=self.context_window_source,
                context_clamped=context_clamped,
                warnings=warnings,
            )

    def feedback(
        self,
        plan: AdaptiveRequestPlan,
        usage: dict[str, Any],
        transport: str | None = None,
    ) -> None:
        accounting = normalize_usage(usage, transport)
        prompt_tokens = accounting.get("input_tokens")
        completion_tokens = accounting.get("output_tokens")
        with self._lock:
            self._attempt_count += 1
            if prompt_tokens is None and completion_tokens is None:
                return
            self._usage_count += 1
            if prompt_tokens is not None and plan.estimated_prompt_units > 0:
                observed_ratio = prompt_tokens / plan.estimated_prompt_units
                self._prompt_tokens_per_unit = _ema(
                    self._prompt_tokens_per_unit, observed_ratio, self.ema_alpha
                )
            if completion_tokens is not None:
                self.predicted_completion_tokens = _ema(
                    self.predicted_completion_tokens,
                    float(completion_tokens),
                    self.ema_alpha,
                )
            actual_total = (prompt_tokens or 0) + (completion_tokens or 0)
            self._samples.append((time.time(), actual_total, plan.target_total_tokens))
            cutoff = time.time() - 60
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sample_count = len(self._samples)
            actual_average = (
                sum(item[1] for item in self._samples) / sample_count
                if sample_count
                else None
            )
            target_average = (
                sum(item[2] for item in self._samples) / sample_count
                if sample_count
                else self.target_tokens_per_request
            )
            deviation = (
                (actual_average - target_average) / target_average
                if actual_average is not None and target_average > 0
                else None
            )
            coverage = (
                self._usage_count / self._attempt_count if self._attempt_count else 0.0
            )
            if sample_count < self.min_samples:
                status = "learning"
            elif coverage < self.usage_coverage_warn_below:
                status = "usage_degraded"
            elif deviation is not None and abs(deviation) <= self.tolerance_ratio:
                status = "on_target"
            else:
                status = "off_target"
            return {
                "status": status,
                "sample_count_60s": sample_count,
                "actual_avg_tokens_per_request_60s": actual_average,
                "target_avg_tokens_per_request_60s": target_average,
                "deviation_ratio_60s": deviation,
                "usage_coverage": coverage,
                "prompt_tokens_per_estimated_unit": self._prompt_tokens_per_unit,
                "predicted_completion_tokens": self.predicted_completion_tokens,
                "context_clamped_count": self._clamped_count,
            }


def _load_corpus(raw_paths: Any) -> str:
    paths = raw_paths or [
        "fixtures/long_context.txt",
        "fixtures/half_million_context.txt",
    ]
    if not isinstance(paths, list) or not paths:
        raise ValueError("adaptive_load.corpus_fixtures must be a non-empty list")
    chunks: list[str] = []
    for raw_path in paths:
        path = resolve_project_path(str(raw_path))
        if not path.exists():
            raise FileNotFoundError(f"Adaptive corpus fixture not found: {path}")
        chunks.append(path.read_text(encoding="utf-8"))
    corpus = "\n".join(chunk for chunk in chunks if chunk)
    if not corpus:
        raise ValueError("Adaptive corpus fixtures are empty")
    return corpus


def _slice_corpus_by_units(corpus: str, desired_units: float, offset: int) -> tuple[str, int]:
    if desired_units <= 0 or not corpus:
        return "", offset
    output: list[str] = []
    units = 0.0
    index = offset % len(corpus)
    while units < desired_units:
        char = corpus[index]
        char_units = 0.25 if ord(char) < 128 else 1.0
        if output and units + char_units > desired_units:
            break
        output.append(char)
        units += char_units
        index = (index + 1) % len(corpus)
    return "".join(output), index


def _weighted_schedule(bands: list[dict[str, Any]]) -> list[str]:
    schedule: list[str] = []
    for index, band in enumerate(bands):
        name = str(band.get("name") or f"band_{index}")
        weight = max(int(round(float(band.get("weight", 0)))), 0)
        schedule.extend([name] * weight)
    if not schedule:
        raise ValueError("adaptive_load.bands must contain a positive weight")
    # Interleave the weighted slots so short runs still see all configured bands.
    counts = Counter(schedule)
    interleaved: list[str] = []
    while counts:
        for name in list(counts):
            interleaved.append(name)
            counts[name] -= 1
            if counts[name] <= 0:
                del counts[name]
    return interleaved


def _max_output_tokens(body: dict[str, Any]) -> int:
    value = body.get("max_completion_tokens")
    if value is None:
        value = body.get("max_tokens")
    return _positive_int(value, 0)


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _ema(current: float, observed: float, alpha: float) -> float:
    return current * (1.0 - alpha) + observed * alpha


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _bounded_float(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(parsed, minimum), maximum)
