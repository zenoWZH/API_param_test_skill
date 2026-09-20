from __future__ import annotations

import unittest

from lib.gpt_image_25_tokens import gpt_image_25_output_tokens
from lib.image_token_expectations import image_output_token_expectation


class GptImage25TokenTests(unittest.TestCase):
    def test_official_calculator_reference_vectors(self):
        for size, expected in {
            (1024, 1024): (196, 439, 1756, 3122, 7024),
            (1536, 1024): (158, 343, 1372, 2459, 5488),
            (1536, 864): (120, 280, 1078, 1917, 4312),
            (2048, 2048): (397, 892, 3568, 6343, 14272),
            (3840, 2160): (371, 865, 3336, 5930, 13342),
        }.items():
            for quality, tokens in zip(("low", "medium", "high", "xhigh", "max"), expected):
                with self.subTest(size=size, quality=quality):
                    self.assertEqual(gpt_image_25_output_tokens(*size, quality), tokens)
                    self.assertEqual(gpt_image_25_output_tokens(*reversed(size), quality), tokens)

    def test_half_tie_uses_even_grid(self):
        # Official JS medium grid rounds 24*704/1024=16.5 down to 16.
        self.assertEqual(gpt_image_25_output_tokens(1024, 704, "medium"), 262)

    def test_invalid_shapes_and_unmapped_quality_fail_closed(self):
        for width, height, quality in [(1025, 1024, "low"), (512, 512, "low"),
                                       (4096, 2048, "low"), (3840, 3840, "low"),
                                       (3072, 768, "low"), (True, 1024, "low"),
                                       (1024, 1024, "ultra")]:
            self.assertIsNone(gpt_image_25_output_tokens(width, height, quality))

    def test_exact_model_source_and_multi_image_integration(self):
        images = [{"width": 1024, "height": 1024}] * 2
        for variant in ("sunburst", "flare"):
            for suffix in ("", "-2026-09-08"):
                result = image_output_token_expectation("gpt-image-2.5-" + variant + suffix,
                    {"quality": "xhigh", "n": 2}, images, reference_source="openai")
                self.assertEqual(result["tokens"], 6244)
                self.assertIn("GptImageTokenCalculator", result["calculator_source"])
        for model, source in [("gpt-image-2.5", "openai"), ("gpt-image-2.5-sunburst", "google_vertex")]:
            self.assertIsNone(image_output_token_expectation(model, {"quality": "high"}, images, reference_source=source))

    def test_auto_keeps_all_five_discrete_qualities(self):
        result = image_output_token_expectation("gpt-image-2.5-flare", {"quality": "auto"},
                                               [{"width": 1024, "height": 1024}])
        self.assertEqual([row["tokens"] for row in result["ranges"]], [196, 439, 1756, 3122, 7024])

    def test_only_observed_stream_previews_add_tokens(self):
        body = {"quality": "low", "stream": True, "partial_images": 3}
        images = [{"width": 1024, "height": 1024}]
        self.assertIsNone(image_output_token_expectation("gpt-image-2.5-flare", body, images))
        result = image_output_token_expectation("gpt-image-2.5-flare", body, images, observed_partial_images=2)
        self.assertEqual(result["tokens"], 396)
        self.assertEqual(result["streamed_preview_tokens"], 200)
        self.assertIsNone(image_output_token_expectation("gpt-image-2.5-flare", body, images, observed_partial_images=4))


if __name__ == "__main__":
    unittest.main()
