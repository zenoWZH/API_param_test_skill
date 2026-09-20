"""Archived capability fixtures for pure historical image-contract regressions.

These dictionaries are only supplied to unit-test factories; they never restore
an active execution binding or permission to send historical v1 requests.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def historical_image_capability(model: str, api_form: str = "gemini_generate_content") -> dict:
    root = Path(__file__).resolve().parents[1]
    if not (root / "packages/model-profile-db/model_profile_db/data/catalog.json").is_file():
        root = root.parent
    catalog = json.loads((root / "packages/model-profile-db/model_profile_db/data/catalog.json").read_text())
    transport = api_form.replace("_", "-")
    interface = catalog["interfaces"][f"image/google_ai_studio/banana/{model}#{transport}-default"]
    historical = interface["source_conflicts"]["api_version_execution_policy_20260908"]["historical_versions"]["v1"]
    reference = historical["model_test_policy_evidence"]
    archive_path = (root / reference["path"]).resolve()
    if not archive_path.is_relative_to(root / "references"):
        raise ValueError("Historical image policy evidence escaped the public references directory")
    raw = archive_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("Historical image policy evidence hash changed")
    ids = reference["policy_ids"]
    if len(ids) != 1:
        raise ValueError("Historical image fixture requires one exact policy")
    policy = copy.deepcopy(json.loads(raw)["policies"][ids[0]])
    contract = historical["interface"]["default_contract_id"]
    return {
        **policy,
        "known_model": True, "known_api_profile": True, "route_profile_known": True,
        "default_reference_source": contract, "reference_source": contract,
        "route_profile": "google_ai_studio", "api_form": api_form,
        "test_policy_parameter_test_enabled": policy["parameter_test_enabled"],
        "parameter_constraints": copy.deepcopy(historical["interface"]["parameter_constraints"]),
    }
