from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from lib.reference_specs import (
    get_reference_source,
    load_model_capability_profile,
    load_model_capability_profiles,
    test_profiles_for_reference as profiles_for_reference,
)
from scripts.generate_test_docs import (
    CAPABILITY_PATH,
    FAMILY_META,
    PROJECT_ROOT,
    _image_case_sets,
    build_documents,
)


LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class DocumentationCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_model_capability_profiles(CAPABILITY_PATH)
        cls.rendered = build_documents()

    def test_generated_family_documents_are_current(self) -> None:
        for path, expected in self.rendered.items():
            self.assertTrue(path.exists(), f"missing generated document: {path}")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                expected.rstrip() + "\n",
                f"regenerate {path.relative_to(PROJECT_ROOT)}",
            )

    def test_every_registered_family_model_route_and_form_is_documented(self) -> None:
        registered: set[tuple[str, str]] = set()
        for modality, modality_cfg in self.capabilities["modalities"].items():
            for family, family_cfg in modality_cfg["families"].items():
                key = (str(modality), str(family))
                registered.add(key)
                self.assertIn(key, FAMILY_META)
                doc = (
                    PROJECT_ROOT
                    / "docs"
                    / "model_profiles"
                    / f"{FAMILY_META[key]['slug']}.md"
                ).read_text(encoding="utf-8")
                models = family_cfg.get("models") or family_cfg.get("canonical_models") or {}
                for model in models:
                    self.assertIn(f"`{model}`", doc, f"{key} model is undocumented")
                for route, route_cfg in (family_cfg.get("route_profiles") or {}).items():
                    self.assertIn(f"`{route}`", doc, f"{key} route is undocumented")
                    for api_form in (route_cfg.get("api_forms") or {}):
                        self.assertIn(
                            f"`{api_form}`", doc, f"{key}/{route} form is undocumented"
                        )
        self.assertEqual(registered, set(FAMILY_META))

    def test_every_text_reference_source_and_profile_is_documented(self) -> None:
        text_families = self.capabilities["modalities"]["text"]["families"]
        for family, family_cfg in text_families.items():
            key = ("text", str(family))
            doc = (
                PROJECT_ROOT
                / "docs"
                / "model_profiles"
                / f"{FAMILY_META[key]['slug']}.md"
            ).read_text(encoding="utf-8")
            sources: set[str] = set()
            for route, route_cfg in (family_cfg.get("route_profiles") or {}).items():
                for api_form, form_cfg in (route_cfg.get("api_forms") or {}).items():
                    for model in (form_cfg.get("model_profiles") or {}):
                        profile = load_model_capability_profile(
                            "text",
                            str(family),
                            str(model),
                            path=CAPABILITY_PATH,
                            route_profile=str(route),
                            api_form=str(api_form),
                        )
                        sources.update(profile.get("allowed_reference_sources") or [])
            for source_id in sources:
                source = get_reference_source(source_id)
                self.assertEqual(source.get("model_family"), family)
                self.assertIn(f"`{source_id}`", doc)
                for profile_name in profiles_for_reference(source_id):
                    self.assertIn(
                        f"`{profile_name}`",
                        doc,
                        f"{family}/{source_id}/{profile_name} is undocumented",
                    )

    def test_every_image_case_is_documented(self) -> None:
        for family in self.capabilities["modalities"]["image"]["families"]:
            key = ("image", str(family))
            doc = (
                PROJECT_ROOT
                / "docs"
                / "model_profiles"
                / f"{FAMILY_META[key]['slug']}.md"
            ).read_text(encoding="utf-8")
            for cases in _image_case_sets(str(family)).values():
                for case in cases:
                    self.assertIn(
                        f"`{case.name}`", doc, f"{family}/{case.name} is undocumented"
                    )

    def test_master_and_readme_link_all_primary_guides(self) -> None:
        targets = (
            "docs/testing_guide.md",
            "docs/parameter_testing.md",
            "docs/cache_testing.md",
            "docs/load_testing.md",
            "docs/model_profiles/README.md",
        )
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        master = (PROJECT_ROOT / "docs" / "testing_guide.md").read_text(
            encoding="utf-8"
        )
        for target in targets:
            self.assertIn(target, readme)
        for target in (
            "parameter_testing.md",
            "cache_testing.md",
            "load_testing.md",
            "model_profiles/README.md",
        ):
            self.assertIn(target, master)

    def test_primary_guides_and_family_manuals_include_ui_diagrams(self) -> None:
        guide_images = {
            "testing_guide.md": ("assets/ui/testing-overview.svg",),
            "parameter_testing.md": (
                "assets/ui/parameter-testing-console.svg",
                "assets/ui/image-parameter-console.svg",
            ),
            "cache_testing.md": ("assets/ui/cache-testing-console.svg",),
            "load_testing.md": ("assets/ui/load-testing-console.svg",),
        }
        for filename, image_targets in guide_images.items():
            content = (PROJECT_ROOT / "docs" / filename).read_text(encoding="utf-8")
            for target in image_targets:
                self.assertIn(f"]({target})", content)

        for (modality, _family), meta in FAMILY_META.items():
            content = (
                PROJECT_ROOT / "docs" / "model_profiles" / f"{meta['slug']}.md"
            ).read_text(encoding="utf-8")
            expected = (
                "../assets/ui/parameter-testing-console.svg"
                if modality == "text"
                else "../assets/ui/image-parameter-console.svg"
            )
            self.assertIn(f"]({expected})", content)

    def test_ui_diagrams_are_accessible_svg_assets(self) -> None:
        asset_dir = PROJECT_ROOT / "docs" / "assets" / "ui"
        expected = {
            "testing-overview.svg",
            "parameter-testing-console.svg",
            "image-parameter-console.svg",
            "cache-testing-console.svg",
            "load-testing-console.svg",
        }
        self.assertEqual({path.name for path in asset_dir.glob("*.svg")}, expected)
        namespace = "{http://www.w3.org/2000/svg}"
        for filename in expected:
            root = ET.parse(asset_dir / filename).getroot()
            self.assertEqual(root.tag, f"{namespace}svg")
            self.assertEqual(root.get("role"), "img")
            self.assertIsNotNone(root.find(f"{namespace}title"))
            self.assertIsNotNone(root.find(f"{namespace}desc"))
            self.assertTrue(root.get("width"))
            self.assertTrue(root.get("height"))

    def test_new_documentation_has_no_broken_relative_markdown_links(self) -> None:
        paths = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "docs" / "testing_guide.md",
            PROJECT_ROOT / "docs" / "parameter_testing.md",
            PROJECT_ROOT / "docs" / "cache_testing.md",
            PROJECT_ROOT / "docs" / "load_testing.md",
            *self.rendered.keys(),
        ]
        for path in paths:
            content = path.read_text(encoding="utf-8")
            for raw_target in LINK_PATTERN.findall(content):
                target = raw_target.split("#", 1)[0].strip()
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (
                    Path(target)
                    if Path(target).is_absolute()
                    else (path.parent / target).resolve()
                )
                self.assertTrue(
                    resolved.exists(),
                    f"broken link in {path.relative_to(PROJECT_ROOT)}: {raw_target}",
                )


if __name__ == "__main__":
    unittest.main()
