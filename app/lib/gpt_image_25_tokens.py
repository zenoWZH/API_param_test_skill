"""GPT Image 2.5 output-image counts from the 2026-09-08 official calculator.

This is independent of the earlier GPT Image 2 calculator. It excludes input,
mainline Responses text/reasoning, and streaming partial-image token charges.
"""
from __future__ import annotations

CALCULATOR_URL = "https://developers.openai.com/_astro/GptImageTokenCalculator.react.yz8GdjDh.js"
CALCULATOR_SHA256 = "a3717b517e5fc4350bea13db6c590cf2cb28949a22881d048f929f3005a12440"
MODEL_IDS = frozenset(
    f"gpt-image-2.5-{variant}{suffix}"
    for variant in ("sunburst", "flare")
    for suffix in ("", "-2026-09-08")
)
QUALITY_GRID = {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96}


def gpt_image_25_output_tokens(width: int, height: int, quality: str) -> int | None:
    if any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in (width, height)):
        return None
    grid = QUALITY_GRID.get(quality)
    longest, shortest = max(width, height), min(width, height)
    pixels = width * height
    if (grid is None or width % 16 or height % 16 or longest > 3840
            or longest > 3 * shortest or not 655_360 <= pixels <= 8_294_400):
        return None
    # The official JavaScript rounds exact half ties to the even grid integer.
    quotient, remainder = divmod(grid * shortest, longest)
    short_grid = quotient + int(2 * remainder > longest or (2 * remainder == longest and quotient % 2 == 1))
    return (grid * short_grid * (2_000_000 + pixels) + 3_999_999) // 4_000_000
