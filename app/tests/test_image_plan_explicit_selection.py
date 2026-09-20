"""An explicit image interface must survive either supported payload shape."""
import copy
from pathlib import Path

import pytest
import yaml

from lib.job_spec import resolve_image_plan


@pytest.fixture
def config():
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())


def plan(config, payload):
    return resolve_image_plan(config, payload, "gemini", "gemini-3.1-flash-lite-image", 120)


def test_top_level_and_nested_image_selection_agree_without_mutating_input(config):
    selection = {"route_profile": "google_ai_studio", "api_form": "gemini_generate_content"}
    options = {"suite": "smoke", "no_cross_control": True}
    top = {**selection, "image_plan": options}
    nested = {"image_plan": {**options, **selection}}
    both = {**selection, **nested}
    original = copy.deepcopy([top, nested, both])
    plans = [plan(config, p) for p in (top, nested, both)]
    assert plans[0] == plans[1] == plans[2]
    assert plans[0]["api_form"] == "gemini_generate_content"
    assert [top, nested, both] == original


def test_top_level_closed_image_form_cannot_use_default(config):
    with pytest.raises(ValueError):
        plan(config, {"api_form": "gemini_interactions", "image_plan": {"suite": "smoke", "no_cross_control": True}})


def test_top_level_foreign_route_cannot_use_default(config):
    with pytest.raises(ValueError):
        plan(config, {"route_profile": "unregistered-route", "image_plan": {"suite": "smoke", "no_cross_control": True}})


@pytest.mark.parametrize("field,top,nested", [
    ("api_form", "gemini_interactions", "gemini_generate_content"),
    ("route_profile", "google_vertex", "google_ai_studio"),
])
def test_conflicting_image_selection_fails_before_config_lookup(field, top, nested):
    with pytest.raises(ValueError, match="conflicts"):
        plan({}, {field: top, "image_plan": {field: nested}})


@pytest.mark.parametrize("field,value", [("api_form", None), ("api_form", []), ("route_profile", False), ("route_profile", " ")])
@pytest.mark.parametrize("nested", [False, True])
def test_invalid_explicit_selection_fails_before_config_lookup(field, value, nested):
    selection = {field: value}
    with pytest.raises(ValueError, match="must be a non-empty string"):
        plan({}, {"image_plan": selection} if nested else selection)
