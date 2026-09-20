"""Public MPDB migration inventory with explicit gaps and exact reference targets.

The inventory never loads provider configuration, private overlays or credentials.
A registered workflow descriptor is evidence of a declared adapter, not of a live
successful run or complete migration of a related legacy suite.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
from pathlib import Path
from typing import Any

from .common import digest_json


def _reference(interface_id: str, interface: dict, contract_id: str | None) -> dict:
    return {
        'source_id': interface['source_id'], 'profile_id': interface['profile_id'],
        'interface_id': interface_id, 'contract_id': contract_id,
        'api_form': interface['api_form'],
    }


def _disabled_reasons(row: dict, reference: dict | None = None) -> list[str]:
    reasons = []
    for label, value in (('binding', row), ('reference', reference or {})):
        if value.get('disabled_reason'):
            reasons.append(label + ': ' + str(value['disabled_reason']))
        for field in ('enabled', 'executable', 'runner_enabled', 'parameter_test_enabled'):
            if value.get(field) is False:
                reasons.append(label + ': ' + field + '=false')
        status = value.get('test_binding_status')
        if status and status != 'required':
            reasons.append(label + ': test_binding_status=' + str(status))
    return sorted(set(reasons))


def unreachable_parameter_declarations(catalog: Any, *, _payload=None) -> list[dict]:
    """Retain enabled declarations that no enabled policy can select.

    Interface membership alone is not execution permission. Scan every policy,
    including alternate suites, and keep actual resolver rejections as evidence.
    These declarations are neither compiled nor relabeled as disabled.
    """
    payload = catalog.payload if _payload is None else _payload
    policies = {key: row for key, row in payload['test_bindings'].items()
                if row['extension_type'] == 'model_test_policy'}
    enabled = {key: row for key, row in policies.items()
               if row.get('parameter_test_enabled') is True and not _disabled_reasons(row)}
    workflow_contracts = {row['contract_id'] for row in catalog.list_workflows(enabled=True)}
    result = []
    for key, binding in sorted(payload['test_bindings'].items()):
        if binding['extension_type'] != 'parameter' or _disabled_reasons(binding):
            continue
        contract = binding.get('contract_id')
        if (not contract or contract in workflow_contracts
                or any(contract in row.get('reference_contract_ids', []) for row in enabled.values())):
            continue
        interfaces = {iid: row for iid, row in payload['interfaces'].items()
                      if contract in row.get('contract_ids', []) or row.get('default_contract_id') == contract}
        if interfaces and not any(not _disabled_reasons(interface)
                and any(row.get('interface_id') == iid for row in enabled.values())
                for iid, interface in interfaces.items()):
            continue  # Already accounted for by the existing-disabled disposition.
        if not interfaces and _disabled_reasons(payload['contracts'][contract]):
            continue
        rejections = []
        for iid, interface in sorted(interfaces.items()):
            profile = payload['profiles'][interface['profile_id']]
            for policy_id, policy in sorted(policies.items()):
                if policy.get('interface_id') != iid:
                    continue
                try:
                    catalog.resolve_parameter_config(source_id=profile['source_id'], modality=profile['modality'],
                        family_id=profile['family_id'], model_slug=profile['model_slug'], interface_id=iid,
                        api_form=interface['api_form'], test_binding_id=policy_id, contract_id=contract)
                except Exception as exc:
                    rejections.append({'policy_binding_id': policy_id, 'interface_id': iid,
                        'reference_contract_ids': list(policy.get('reference_contract_ids', [])),
                        'default_reference_contract_id': policy.get('default_reference_contract_id'),
                        'error': {'type': type(exc).__name__, 'message': str(exc)}})
                else:
                    raise ValueError('Unenumerated executable parameter consumer: ' + key + ' / ' + policy_id)
        result.append({'test_binding_id': key, 'contract_id': contract, 'source_id': binding['source_id'],
            'status': 'existing_unreachable', 'reason': 'no_enabled_model_policy_consumer',
            'case_ids': list(binding.get('test_cases', [])), 'interface_ids': sorted(interfaces),
            'original_execution_flags': {field: copy.deepcopy(binding[field]) for field in
                ('enabled', 'executable', 'runner_enabled', 'parameter_test_enabled', 'disabled_reason', 'test_binding_status') if field in binding},
            'model_policies_scanned': len(policies), 'enabled_policy_consumers': [], 'resolver_rejections': rejections})
    return result


def build_workflow_inventory(catalog: Any, coverage_report: dict | None = None) -> dict[str, Any]:
    """Enumerate every binding and interface without inferring runtime providers.

    ``catalog`` is a validated Catalog. Every legacy suite remains pending until
    its case coverage is explicitly represented; related workflow references
    alone never establish behavioral parity.
    """
    if not catalog.test_extensions_loaded:
        raise ValueError('Workflow inventory requires test extensions')
    payload = catalog.payload
    interfaces = payload['interfaces']
    contracts = payload['contracts']
    workflows = catalog.list_workflows()
    unreachable = unreachable_parameter_declarations(catalog, _payload=payload)
    unreachable_ids = {row['test_binding_id'] for row in unreachable}
    interface_states = {}
    for key, interface in interfaces.items():
        reasons = _disabled_reasons({}, interface)
        policies = [row for row in payload['test_bindings'].values()
                    if row['extension_type'] == 'model_test_policy' and row.get('interface_id') == key]
        if not any(row.get('parameter_test_enabled') is True and not _disabled_reasons(row) for row in policies):
            reasons.extend(reason for row in policies for reason in _disabled_reasons(row))
            if not reasons:
                reasons.append('reference: no enabled model_test_policy')
        interface_states[key] = {'disabled_reasons': sorted(set(reasons)), 'execution_enabled': not reasons}
    bindings = []
    interface_bindings: dict[str, set[str]] = {key: set() for key in interfaces}
    for binding_id, row in sorted(payload['test_bindings'].items()):
        extension_type = row['extension_type']
        contract_ids = list(dict.fromkeys(
            ([row['contract_id']] if row.get('contract_id') else []) + list(row.get('reference_contract_ids', []))
        ))
        interface_ids = ([row['interface_id']] if row.get('interface_id') else [
            key for key, interface in sorted(interfaces.items())
            if any(contract_id in interface.get('contract_ids', []) for contract_id in contract_ids)
        ])
        references = []
        for interface_id in interface_ids:
            interface = interfaces[interface_id]
            interface_bindings[interface_id].add(binding_id)
            selected_contracts = (contract_ids if row.get('interface_id') else
                                  [key for key in contract_ids if key in interface.get('contract_ids', [])])
            references.extend(_reference(interface_id, interface, key) for key in selected_contracts or [None])
        if not references:
            references = [{
                'source_id': row['source_id'], 'profile_id': None, 'interface_id': None,
                'contract_id': key, 'api_form': contracts[key]['api_form'],
            } for key in contract_ids]
        reasons = _disabled_reasons(row)
        reference_reasons = [
            interface_states[ref['interface_id']]['disabled_reasons'] if ref['interface_id']
            else _disabled_reasons({}, contracts[ref['contract_id']]) for ref in references
        ]
        if not reasons and reference_reasons and all(reference_reasons) and extension_type != 'test_workflow':
            reasons = sorted({reason for group in reference_reasons for reason in group})
        related = sorted(workflow['workflow_id'] for workflow in workflows if any(
            workflow['source_id'] == ref['source_id'] and workflow['interface_id'] == ref['interface_id']
            and (ref['contract_id'] is None or workflow['contract_id'] == ref['contract_id'])
            for ref in references
        ))
        workflow_descriptor = row.get('workflow') or {}
        is_workflow = extension_type == 'test_workflow'
        disposition = 'existing_disabled' if reasons else (
            'enabled' if extension_type in {'parameter', 'model_test_policy', 'test_workflow'} else 'metadata_only'
        )
        migration = ('workflow_declared' if is_workflow else
                     'existing_disabled' if reasons else 'pending_adapter')
        if binding_id in unreachable_ids:
            disposition = migration = 'existing_unreachable'
        bindings.append({
            'test_binding_id': binding_id, 'extension_type': extension_type,
            'reference_targets': references, 'legacy_disposition': disposition,
            'disabled_reasons': reasons, 'migration_status': migration,
            'case_count': len(row.get('test_cases') or workflow_descriptor.get('case_ids')
                              or workflow_descriptor.get('cases') or []),
            'related_workflow_ids': related,
            'execution_target': copy.deepcopy(row.get('execution_target')),
            'factory': {key: workflow_descriptor[key] for key in ('factory_id', 'version', 'source_sha256')
                        if key in workflow_descriptor} or None,
            'live_verified': False,
        })
    interface_rows = [{
        **_reference(key, interface, interface.get('default_contract_id')),
        'contract_ids': list(interface.get('contract_ids', [])),
        'test_binding_ids': sorted(interface_bindings[key]),
        **interface_states[key],
        'reference_enabled': interface.get('enabled', True),
        'reference_executable': interface.get('executable', True),
    } for key, interface in sorted(interfaces.items())]
    pending = [row['test_binding_id'] for row in bindings if row['legacy_disposition'] == 'enabled'
               and row['migration_status'] != 'offline_compiled']
    result = {
        'inventory_schema_version': 1, 'scope': 'public_mpdb_bindings_and_interfaces',
        'catalog_digest': catalog.digest,
        'test_extension_digest': payload.get('test_extension_digest'),
        'bindings': bindings, 'interfaces': interface_rows, 'existing_unreachable_bindings': unreachable,
        'summary': {
            'bindings': len(bindings), 'interfaces': len(interface_rows),
            'extension_types': dict(sorted(Counter(row['extension_type'] for row in bindings).items())),
            'migration_statuses': dict(sorted(Counter(row['migration_status'] for row in bindings).items())),
            'pending_binding_ids': pending,
            'all_enabled_bindings_migrated': not pending and not unreachable,
            'all_enabled_runnable_bindings_migrated': not pending,
            'all_enabled_declarations_have_consumer': not unreachable,
            'existing_unreachable_binding_ids': sorted(unreachable_ids),
            'runtime_provider_routes_inventoried': False, 'live_verified': False,
        },
    }
    if coverage_report is not None:
        _apply_compiled_coverage(result, catalog, coverage_report)
    result['inventory_sha256'] = digest_json(result)
    return result


def coverage_code_digest(workspace: Path | None = None) -> str:
    """Bind the local audit and all public Python producer/adapter code, not reports."""
    root = Path(workspace) if workspace is not None else Path(__file__).resolve().parents[2]
    files = [*root.joinpath('lib').rglob('*.py'), *root.joinpath('scripts').rglob('*.py')]
    files += [root / 'config.yaml']
    return digest_json({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(files) if path.is_file() and not path.is_symlink()})


def parameter_leaf_inventory(catalog: Any) -> list[dict]:
    """Enumerate exact enabled policy/contract/interface/request-model leaves once."""
    inventory = build_workflow_inventory(catalog)
    payload = catalog.payload
    enabled = {row['test_binding_id'] for row in inventory['bindings']
               if row['extension_type'] == 'model_test_policy' and row['legacy_disposition'] == 'enabled'}
    leaves = []
    for policy in sorted(catalog.list_test_bindings(extension_type='model_test_policy'), key=lambda row: row['test_binding_id']):
        if policy['test_binding_id'] not in enabled or policy.get('parameter_test_enabled') is not True:
            continue
        interface = catalog.get_interface(policy['interface_id'])
        profile = catalog.get_profile(interface['profile_id'])
        models = interface.get('request_model_ids') or profile.get('request_model_ids') or [profile['model_slug']]
        contracts = policy.get('reference_contract_ids') or [None]
        excluded = sorted(set(profile.get('request_model_ids') or []) - set(models))
        for contract in sorted(contracts, key=str):
            for model in sorted(set(models)):
                identity = {'policy_binding_id': policy['test_binding_id'], 'source_id': profile['source_id'],
                            'profile_id': profile['profile_id'], 'interface_id': interface['interface_id'],
                            'contract_id': contract, 'request_model_id': model, 'modality': profile['modality'],
                            'family_id': profile['family_id'], 'suite_family_id': policy.get('suite_family_id') or profile['family_id'],
                            'api_form': interface['api_form'], 'transport': interface.get('transport_adapter_id')}
                row = {**identity, 'leaf_id': digest_json(identity), 'excluded_interface_aliases': excluded,
                       'expected_case_ids': [], 'parameter_binding_id': None}
                candidates = [binding_id for binding_id, item in payload['test_bindings'].items()
                              if item['extension_type'] == 'parameter' and item.get('contract_id') == contract
                              and item['source_id'] == profile['source_id']]
                if len(candidates) == 1:
                    row['parameter_binding_id'] = candidates[0]
                try:
                    if not contract:
                        raise ValueError('Enabled policy has no reference contracts')
                    resolved = catalog.resolve_parameter_config(source_id=profile['source_id'], modality=profile['modality'],
                        family_id=profile['family_id'], model_slug=profile['model_slug'], interface_id=interface['interface_id'],
                        api_form=interface['api_form'], test_binding_id=policy['test_binding_id'], contract_id=contract)
                    cases = resolved.get('test_cases')
                    if not isinstance(cases, list) or not cases or any(not isinstance(case, str) or not case for case in cases):
                        raise ValueError('Enabled parameter leaf has no complete case-ID list')
                    if len(cases) != len(set(cases)):
                        raise ValueError('Enabled parameter leaf repeats a case ID')
                    row['expected_case_ids'] = list(cases)
                    row['parameter_binding_id'] = resolved['parameter_test_binding_id']
                except Exception as exc:
                    row['resolution_error'] = {'type': type(exc).__name__, 'message': str(exc)}
                leaves.append(row)
    return leaves


def conditional_empty_case_expansions(leaf: dict) -> dict[str, dict]:
    """Named legacy categories whose exact model-specific factory emits no case.

    These are recorded explicitly as non-applicable declarations, never aliased
    to an executed request or used to waive an unknown missing case.
    """
    result = {}
    if leaf.get('modality') != 'image':
        return result
    model, contract = leaf.get('request_model_id'), leaf.get('contract_id')
    if leaf.get('source_id') == 'google_ai_studio' and leaf.get('api_form') == 'gemini_generate_content':
        exclusions = {
            ('gemini-3.1-flash-image', 'gemini_3_1_flash_image_generate_content_v1beta'): ('image_size_boundaries', 'extreme_aspect_boundaries'),
            ('gemini-3.1-flash-lite-image', 'gemini_3_1_flash_lite_image_generate_content_v1beta'): ('extreme_aspect_boundaries',),
        }
        for suffix in exclusions.get((model, contract), ()):
            result[contract + '_' + suffix] = {'reason': 'The existing exact-model factory emits no cells for this category',
                'factory_source': 'lib/banana_generate_content.py', 'condition': 'model-specific boundary_tiers and EXTREME_RATIOS branch'}
    if leaf.get('source_id') == 'openai' and leaf.get('api_form') == 'openai_responses':
        from lib.gpt_image_25_responses import SNAPSHOTS
        if model in SNAPSHOTS.values() and contract in {'gpt_image_25_flare_responses_image_generation', 'gpt_image_25_sunburst_responses_image_generation'}:
            result['gpt_image_25_responses_dated_snapshot'] = {'reason': 'Already-dated request models do not emit the rolling-to-dated transition case',
                'factory_source': 'lib/gpt_image_25_responses.py', 'condition': 'dated_snapshot is emitted only for model in MODELS'}
    return result


def _apply_compiled_coverage(inventory: dict, catalog: Any, report: dict) -> None:
    claimed = copy.deepcopy(report)
    checksum = claimed.pop('coverage_sha256', None)
    if checksum != digest_json(claimed) or report.get('coverage_schema_version') != 1:
        raise ValueError('Invalid workflow compilation coverage report')
    if report.get('catalog_digest') != catalog.digest or report.get('test_extension_digest') != catalog.payload.get('test_extension_digest'):
        raise ValueError('Workflow coverage describes a different catalog snapshot')
    if report.get('code_digest') != coverage_code_digest() or report.get('source_changed_during_audit'):
        raise ValueError('Workflow coverage source code changed; rerun the offline audit')
    if any(report.get('summary', {}).get(field) != 0 for field in ('network_attempts', 'credential_access_attempts')):
        raise ValueError('Workflow coverage crossed the offline network or credential boundary')
    if digest_json(report.get('existing_unreachable_bindings')) != digest_json(inventory['existing_unreachable_bindings']):
        raise ValueError('Workflow coverage changed existing unreachable declarations or their resolver evidence')
    expected = parameter_leaf_inventory(catalog)
    by_id = {row['leaf_id']: row for row in report.get('leaves', [])}
    if len(by_id) != len(report.get('leaves', [])) or set(by_id) != {row['leaf_id'] for row in expected}:
        raise ValueError('Workflow coverage does not enumerate the exact enabled leaf set')
    binding_leaves = {}
    compiled_ids = set()
    for original in expected:
        row = by_id[original['leaf_id']]
        if any(row.get(key) != original.get(key) for key in original):
            raise ValueError('Workflow coverage changed an original leaf identity or case set')
        excluded = row.get('not_applicable_case_ids') or {}
        allowed = conditional_empty_case_expansions(original)
        if not isinstance(excluded, dict) or any(case not in allowed or reason != allowed[case] for case, reason in excluded.items()):
            raise ValueError('Workflow coverage invented an unsupported case exclusion')
        covered = set(row.get('covered_case_ids', []))
        proven = (row.get('status') == 'compiled' and row.get('missing_case_ids') == []
                  and not covered.intersection(excluded)
                  and covered.union(excluded) == set(original['expected_case_ids'])
                  and isinstance(row.get('plans'), list) and bool(row['plans'])
                  and all(plan.get('plan_digest') and plan.get('handler_bindings') for plan in row['plans']))
        if proven:
            compiled_ids.add(row['leaf_id'])
        for binding in (original['policy_binding_id'], original['parameter_binding_id']):
            if binding:
                binding_leaves.setdefault(binding, set()).add(original['leaf_id'])
    workflow_rows = {row['test_binding_id']: row for row in report.get('workflows', [])}
    declared_workflows = {row['test_binding_id']: row for row in catalog.list_workflows(enabled=True)}
    if len(workflow_rows) != len(report.get('workflows', [])) or set(workflow_rows) != set(declared_workflows):
        raise ValueError('Workflow coverage does not enumerate the exact registered workflow set')
    for row in inventory['bindings']:
        if row['legacy_disposition'] in {'existing_disabled', 'existing_unreachable'}:
            continue
        leaves = binding_leaves.get(row['test_binding_id'])
        if leaves:
            row['coverage_leaf_ids'] = sorted(leaves)
            if leaves <= compiled_ids:
                row['migration_status'] = 'offline_compiled'
        if row['extension_type'] == 'test_workflow':
            workflow = workflow_rows.get(row['test_binding_id'])
            descriptor = catalog.get_test_binding(row['test_binding_id'])['workflow']
            expected_cases = descriptor.get('case_ids', [])
            if (workflow and workflow.get('status') == 'compiled' and workflow.get('missing_case_ids') == []
                    and workflow.get('expected_case_ids') == expected_cases and workflow.get('covered_case_ids') == expected_cases
                    and workflow.get('plans') and all(plan.get('plan_digest') and plan.get('handler_bindings') for plan in workflow['plans'])):
                row['migration_status'] = 'offline_compiled'
            else:
                row['migration_status'] = 'pending_adapter'
    pending = [row['test_binding_id'] for row in inventory['bindings'] if row['legacy_disposition'] == 'enabled' and row['migration_status'] != 'offline_compiled']
    inventory['summary'].update(migration_statuses=dict(sorted(Counter(row['migration_status'] for row in inventory['bindings']).items())),
        pending_binding_ids=pending, all_enabled_bindings_migrated=not pending and not inventory['existing_unreachable_bindings'],
        all_enabled_runnable_bindings_migrated=not pending,
        offline_coverage_sha256=report['coverage_sha256'], enabled_leaf_count=len(expected), compiled_leaf_count=len(compiled_ids))
