"""Run the bounded V4.1 research CLI with the standalone data configuration."""
from __future__ import annotations

import sys
import argparse
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_env

skill_env.configure_skill_env()


def validate_research_binding():
    from lib.deepseek_v4_1_matrix import MODEL, PROFILE_ID, PATHS, build_cases
    from lib.model_profile_catalog import get_model_profile_catalog

    catalog = get_model_profile_catalog()
    bindings = [row for row in catalog.list_test_bindings(source="deepseek", extension_type="research_parameter_matrix")
                if row.get("profile_id") == PROFILE_ID]
    if len(bindings) != len(PATHS) or {row.get("api_form") for row in bindings} != set(PATHS):
        raise ValueError("The installed MPDB does not contain the exact five-form V4.1 research matrix")
    cases = build_cases()
    root = skill_env.APP_ROOT.resolve()
    for binding in bindings:
        runner = binding.get("dedicated_research_runner") or {}
        if (binding.get("dedicated_execution_enabled") is not True
                or binding.get("execution_request_model_id") != MODEL
                or runner.get("generic_dispatch") is not False
                or binding.get("case_definitions") != [case for case in cases if case["api_form"] == binding["api_form"]]):
            raise ValueError("The dedicated research selection differs from its installed MPDB binding")
        for reference in (binding["factory_reference"], binding["validation_reference"], runner,
                          *binding.get("documentation_references", [])):
            path = (root / reference["path"]).resolve()
            if root not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
                raise ValueError("Dedicated research source differs from the installed MPDB reference")


def main(argv=None):
    from scripts import run_deepseek_v4_1_matrix as research
    from matrix import _outside_bundle

    argv = list(sys.argv[1:] if argv is None else argv)
    if any(flag in argv for flag in ("--help", "-h")):
        return research.main(argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-dir", type=Path, required=True)
    args, _ = parser.parse_known_args(argv)
    try:
        _outside_bundle(args.output_dir)
        validate_research_binding()
    except ValueError as exc:
        parser.error(str(exc))
    # Freeze the consumer pin along with the exact source-defined requests so
    # a subsequent catalog upgrade cannot replay an old prepared batch.
    research.SOURCE_FILES = tuple(dict.fromkeys((*research.SOURCE_FILES, "lib/model_profile_catalog.py")))
    return research.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
