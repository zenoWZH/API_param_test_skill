from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .catalog import Catalog, diff_catalogs, load_catalog
from .compiler import (
    build_catalog,
    compile_catalog,
    compose_catalog,
    load_source_catalog,
    load_test_extensions,
    verify_artifact_manifest,
)
from .paths import TEST_EXTENSIONS_PATH


def _query_row_matches_source(
    catalog: Catalog,
    row: dict[str, Any],
    source: str,
) -> bool:
    """Apply --source to exact source-owned query results as well as lists."""

    if "source_id" in row:
        return str(row.get("source_id") or "") == source
    if "source_ids" in row:
        return source in {str(value) for value in row.get("source_ids") or []}
    interface_id = str(row.get("interface_id") or "")
    if interface_id:
        interface_source = str(
            catalog.get_interface(interface_id).get("source_id") or ""
        )
        return interface_source == source
    contract_id = str(row.get("contract_id") or "")
    if contract_id:
        return source in {
            str(value)
            for value in catalog.get_contract(contract_id).get("source_ids") or []
        }
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpdb")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate source catalog and extensions")
    validate.add_argument("--source")
    validate.add_argument("--extensions")

    build = sub.add_parser("build", help="compile JSON, SQLite, and manifest artifacts")
    build.add_argument("--source")
    build.add_argument("--extensions")
    build.add_argument("--json")
    build.add_argument("--sqlite")
    build.add_argument("--manifest")

    verify = sub.add_parser(
        "verify-artifacts",
        help="verify manifest hashes plus SQLite integrity and metadata",
    )
    verify.add_argument("--manifest")

    info = sub.add_parser("info", help="show database identity and row counts")
    info.add_argument("--catalog")
    info.add_argument("--core-only", action="store_true")

    query = sub.add_parser("query", help="query compiled model profiles")
    query.add_argument("--catalog")
    query.add_argument(
        "--entity",
        choices=(
            "modalities",
            "sources",
            "families",
            "models",
            "profiles",
            "interfaces",
            "route-templates",
            "contracts",
            "test-bindings",
        ),
        default="profiles",
    )
    query.add_argument("--modality")
    query.add_argument("--source")
    query.add_argument("--family")
    query.add_argument("--model")
    query.add_argument("--api-form")
    query.add_argument("--routing-mode")
    query.add_argument("--extension-type")
    query.add_argument("--enabled", choices=("true", "false"))
    query.add_argument("--core-only", action="store_true")
    query.add_argument("--profile-id")
    query.add_argument("--interface-id")
    query.add_argument("--route-template-id")
    query.add_argument("--contract-id")
    query.add_argument("--test-binding-id")

    resolve = sub.add_parser(
        "resolve",
        help="resolve a source-scoped request model to one callable interface",
    )
    resolve.add_argument("source")
    resolve.add_argument("model")
    resolve.add_argument("--catalog")
    resolve.add_argument("--modality")
    resolve.add_argument("--api-form")
    resolve.add_argument("--routing-mode")
    resolve.add_argument("--core-only", action="store_true")

    parameter_configs = sub.add_parser(
        "parameter-configs",
        help="list the source-first parameter-test projection",
    )
    parameter_configs.add_argument("--catalog")
    parameter_configs.add_argument("--source", required=True)
    parameter_configs.add_argument("--modality")
    parameter_configs.add_argument("--family")
    parameter_configs.add_argument("--model")
    parameter_configs.add_argument("--interface-id")
    parameter_configs.add_argument("--api-form")
    parameter_configs.add_argument("--test-binding-id")
    parameter_configs.add_argument("--suite")
    parameter_configs.add_argument("--context")
    parameter_configs.add_argument("--contract-id")
    parameter_configs.add_argument("--view", action="store_true")
    parameter_configs.add_argument("--core-only", action="store_true")

    resolve_parameter = sub.add_parser(
        "resolve-parameter-config",
        help="strictly resolve one source-first parameter-test leaf",
    )
    resolve_parameter.add_argument("--catalog")
    resolve_parameter.add_argument("--source", required=True)
    resolve_parameter.add_argument("--modality", required=True)
    resolve_parameter.add_argument("--family", required=True)
    resolve_parameter.add_argument("--model", required=True)
    resolve_parameter.add_argument("--interface-id", required=True)
    resolve_parameter.add_argument("--api-form", required=True)
    resolve_parameter.add_argument("--test-binding-id")
    resolve_parameter.add_argument("--suite")
    resolve_parameter.add_argument("--context")
    resolve_parameter.add_argument("--contract-id")
    resolve_parameter.add_argument("--parameter-test-binding-id")
    resolve_parameter.add_argument("--core-only", action="store_true")

    for command, help_text in (
        ("workflows", "list exact reference and execution workflow bindings"),
        ("resolve-workflow", "resolve one enabled workflow without enabling its reference"),
    ):
        workflow = sub.add_parser(command, help=help_text)
        workflow.add_argument("--catalog")
        workflow.add_argument("--extensions")
        workflow.add_argument("--core-only", action="store_true")
        exact = command == "resolve-workflow"
        for selector in ("source", "profile-id", "interface-id", "contract-id"):
            workflow.add_argument("--" + selector, required=exact)
        workflow.add_argument("--workflow-id")
        workflow.add_argument("--provider", required=exact)
        workflow.add_argument("--request-model", required=exact)
        workflow.add_argument("--api-form", required=exact)
        if exact:
            workflow.add_argument("--transport-adapter", required=True)
            workflow.add_argument("--test-binding-id")
        else:
            workflow.add_argument("--enabled", choices=("true", "false"))

    diff = sub.add_parser("diff", help="compare two compiled catalogs")
    diff.add_argument("old")
    diff.add_argument("new")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        compiled = compile_catalog(load_source_catalog(args.source))
        extension_path = (
            Path(args.extensions)
            if args.extensions
            else (
                Path(args.source).with_name(TEST_EXTENSIONS_PATH.name)
                if args.source
                else TEST_EXTENSIONS_PATH
            )
        )
        candidate = (
            compose_catalog(compiled, load_test_extensions(extension_path))
            if extension_path.exists()
            else compiled
        )
        print(
            json.dumps(
                {
                    "catalog_version": compiled["catalog_version"],
                    "catalog_digest": compiled["catalog_digest"],
                    "test_extension_digest": candidate.get(
                        "test_extension_digest"
                    ),
                    "profiles": len(compiled["profiles"]),
                    "interfaces": len(compiled["interfaces"]),
                    "contracts": len(compiled["contracts"]),
                    "test_bindings": len(
                        candidate.get("test_bindings") or {}
                    ),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "build":
        compiled = build_catalog(
            args.source,
            test_extensions_path=args.extensions,
            output_json=args.json,
            output_sqlite=args.sqlite,
            output_manifest=args.manifest,
        )
        print(compiled["catalog_digest"])
        return 0
    if args.command == "verify-artifacts":
        manifest = verify_artifact_manifest(args.manifest)
        print(
            json.dumps(
                {
                    "catalog_digest": manifest["catalog_digest"],
                    "test_extension_digest": manifest[
                        "test_extension_digest"
                    ],
                    "artifacts": len(manifest["artifacts"]),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "info":
        catalog = load_catalog(
            args.catalog,
            include_test_extensions=not args.core_only,
        )
        print(
            json.dumps(
                catalog.database_info(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "query":
        catalog = load_catalog(
            args.catalog,
            include_test_extensions=not args.core_only,
        )
        exact_source_owned_result = False
        if args.profile_id:
            payload = catalog.get_profile(args.profile_id)
            exact_source_owned_result = True
        elif args.interface_id and args.entity != "test-bindings":
            payload = catalog.get_interface(args.interface_id)
            exact_source_owned_result = True
        elif args.route_template_id:
            payload = catalog.get_route_template(args.route_template_id)
        elif args.contract_id and args.entity != "test-bindings":
            payload = catalog.get_contract(args.contract_id)
            exact_source_owned_result = True
        elif args.test_binding_id:
            payload = catalog.get_test_binding(args.test_binding_id)
            exact_source_owned_result = True
        elif args.entity == "modalities":
            payload = catalog.list_modalities()
        elif args.entity == "sources":
            payload = catalog.list_sources(args.modality)
        elif args.entity == "families":
            payload = catalog.list_families(
                modality=args.modality,
                source=args.source,
            )
        elif args.entity == "models":
            payload = catalog.list_models(
                modality=args.modality,
                source=args.source,
                family=args.family,
            )
        elif args.entity == "route-templates":
            payload = catalog.list_route_templates(
                modality=args.modality,
                family=args.family,
                routing_mode=args.routing_mode,
            )
        elif args.entity == "interfaces":
            payload = catalog.list_interfaces(
                modality=args.modality,
                source=args.source,
                family=args.family,
                model=args.model,
                api_form=args.api_form,
                routing_mode=args.routing_mode,
                enabled=(
                    None if args.enabled is None else args.enabled == "true"
                ),
            )
        elif args.entity == "contracts":
            payload = catalog.list_contracts(
                source=args.source,
                family=args.family,
                api_form=args.api_form,
                routing_mode=args.routing_mode,
            )
        elif args.entity == "test-bindings":
            payload = catalog.list_test_bindings(
                source=args.source,
                interface_id=args.interface_id,
                contract_id=args.contract_id,
                extension_type=args.extension_type,
            )
        else:
            payload = catalog.list_profiles(
                modality=args.modality,
                source=args.source,
                family=args.family,
                model=args.model,
                api_form=args.api_form,
            )
        if (
            exact_source_owned_result
            and args.source is not None
            and not _query_row_matches_source(catalog, payload, args.source)
        ):
            payload = []
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "resolve":
        catalog = load_catalog(
            args.catalog,
            include_test_extensions=not args.core_only,
        )
        payload = catalog.resolve_request_model(
            args.source,
            args.model,
            args.api_form,
            args.routing_mode,
            modality=args.modality,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "parameter-configs":
        catalog = load_catalog(
            args.catalog,
            include_test_extensions=not args.core_only,
        )
        if args.view:
            payload = catalog.source_first_parameter_view(args.source)
        else:
            payload = catalog.list_parameter_configs(
                source_id=args.source,
                modality=args.modality,
                family_id=args.family,
                model_slug=args.model,
                interface_id=args.interface_id,
                api_form=args.api_form,
                test_binding_id=args.test_binding_id,
                suite=args.suite,
                context=args.context,
                contract_id=args.contract_id,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "resolve-parameter-config":
        catalog = load_catalog(
            args.catalog,
            include_test_extensions=not args.core_only,
        )
        payload = catalog.resolve_parameter_config(
            source_id=args.source,
            modality=args.modality,
            family_id=args.family,
            model_slug=args.model,
            interface_id=args.interface_id,
            api_form=args.api_form,
            test_binding_id=args.test_binding_id,
            suite=args.suite,
            context=args.context,
            contract_id=args.contract_id,
            parameter_test_binding_id=args.parameter_test_binding_id,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command in {"workflows", "resolve-workflow"}:
        catalog = load_catalog(
            args.catalog, include_test_extensions=not args.core_only,
            test_extensions_path=args.extensions,
        )
        selection = {
            "source_id": args.source, "profile_id": args.profile_id,
            "interface_id": args.interface_id, "contract_id": args.contract_id,
            "workflow_id": args.workflow_id,
        }
        if args.command == "workflows":
            payload = catalog.list_workflows(
                **selection, provider_id=args.provider,
                request_model_id=args.request_model, api_form=args.api_form,
                enabled=None if args.enabled is None else args.enabled == "true",
            )
        else:
            payload = catalog.resolve_workflow(
                **selection, test_binding_id=args.test_binding_id,
                execution_target={
                    "provider_id": args.provider, "request_model_id": args.request_model,
                    "api_form": args.api_form, "transport_adapter_id": args.transport_adapter,
                },
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "diff":
        payload = diff_catalogs(load_catalog(Path(args.old)), load_catalog(Path(args.new)))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
