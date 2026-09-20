"""Shared fixed-job execution and legacy token/identity report production."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from lib.credential_security import redact_secrets
from lib.metrics import write_json
from lib.parameter_job_controls import bind_parameter_execution, workflow_execution_from_job
from lib.parameter_output_limit import configured_parameter_test_min_output_tokens
from lib.token_audit import flatten_token_audits, summarize_token_audits
from lib.model_identity import summarize_model_identity_audits
from .adapters import fixed_parameter as fixed
from .fixed_results import run_fixed_parameter_params
from .fixed_service import validate_fixed_configuration, fixed_execution_config


def execute_fixed_parameter_job(config, job, output_dir, *, runner_module):
    """Execute only the exact frozen source suite; all domain results retain native audits."""
    from lib.job_spec import _current_job_snapshot_integrity
    from lib import report_retention as retention
    if workflow_execution_from_job(job) is None:
        raise ValueError('Fixed workflow entry requires a schema-v6 JobSpec')
    valid, reasons = _current_job_snapshot_integrity(job)
    if not valid:
        raise ValueError('Invalid fixed workflow identity: ' + ', '.join(reasons))
    plan = job['execution_plan']
    domain = fixed.fixed_domain_from_plan(plan)
    kind = fixed._kind(domain)
    if domain.snapshot != job.get('model_profile_database'):
        raise ValueError('Fixed workflow source differs from its job snapshot')
    if job.get('parameter_suite') != fixed._suite_id(domain):
        raise ValueError('Fixed workflow suite differs from its job selection')
    if kind == 'anthropic_cache' and domain.frozen_payload != job.get('fixed_parameter_plan'):
        raise ValueError('Fixed workflow cache nonces differ from its job')
    runtime = fixed_execution_config(config, domain)
    bind_parameter_execution(runtime, job, os.environ)
    runtime['_parameter_identity_snapshot'] = domain.snapshot
    validate_fixed_configuration(runtime, domain)
    expected, _ = fixed.prepare_fixed_parameter_plan(domain,
        minimum_output_tokens=configured_parameter_test_min_output_tokens(runtime))
    if expected != plan:
        raise ValueError('Fixed workflow output floor differs from the reviewed plan')
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink() or output_dir.resolve() != output_dir:
        raise ValueError('Fixed report directory must not traverse a symlink')
    output_dir.mkdir(parents=True, exist_ok=True)
    # The claim is exclusive and precedes credentials. The source adapter also
    # writes its original immutable per-wire artifacts and explicit run marker.
    if (output_dir / 'workflow_runs').exists() or any(output_dir.glob(kind + '_*.json')):
        raise ValueError('The fixed report directory already contains an execution ledger')
    job_path = output_dir / 'job_spec.json'
    if job_path.exists() and json.loads(job_path.read_text()) != job:
        raise ValueError('Fixed report directory belongs to another job')
    for name in ('param_results.json', 'verdict.json', 'token_audit.json', 'model_identity.json',
                 'fixed_parameter_plan.json', 'execution_plan.json'):
        if (output_dir / name).exists():
            raise ValueError('Fixed report directory already owns execution evidence')
    cache_job = kind == 'anthropic_cache'
    if cache_job:
        retention.initialize_report_retention(output_dir, root=output_dir.parent)
    def write_once(name, value):
        path = output_dir / name
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(fd, 'wb') as stream:
                stream.write(fixed.beta.canonical_bytes(value) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if created and cache_job:
                retention.register_report_files(output_dir, [name], root=output_dir.parent)
    write_once('dispatch_started.json', {'plan_digest': plan['plan_digest'], 'target': plan['target']})
    if not job_path.exists():
        write_once('job_spec.json', job)
    elif cache_job:
        retention.register_report_files(output_dir, ['job_spec.json'], root=output_dir.parent)
    write_once('execution_plan.json', plan)
    if cache_job:
        write_once('fixed_parameter_plan.json', domain.frozen_payload)
    client = runner_module.DeepSeekClient.from_config(runtime, job['provider'])
    results, observations = run_fixed_parameter_params(runtime, client, domain, output_dir,
        runner_module=runner_module, execution_plan=plan, artifact_writer=write_once)
    report = observations['workflow_result']
    audited_results = fixed.cache.generation_token_audit_results(domain, results) if cache_job else results
    token_summary = summarize_token_audits(audited_results)
    identity_summary = summarize_model_identity_audits(audited_results)
    compatible = observations.get('pass') is True
    token_pass = token_summary.get('pass') is True
    identity_pass = identity_summary.get('pass') is True
    passed = compatible and token_pass and identity_pass and report.get('status') == 'passed'
    observation_key = {'beta': 'bounded_beta_observations', 'fim_causal': 'bounded_fim_observations',
        'anthropic_prefill': 'bounded_prefill_observations', 'anthropic_cache': 'bounded_cache_observations'}[kind]
    if kind == 'beta':
        specs = {'test_profiles': list(plan['selected_cases']),
            'tested_params': list(dict.fromkeys(r.target_parameter for r in domain.requests)),
            'untested_params': [], 'fixed_request_count': domain.request_cap,
            'full_parameter_matrix_verified': False, 'token_exact_proof': False}
    else:
        from lib.fixed_parameter_specs import fixed_spec_payload
        specs = fixed_spec_payload(domain)
    failures = [row for row in results if row.get('overall_pass') is not True]
    expected_rejections = [row for row in results if row.get('status') == 'expected_rejection']
    token_failures = [row for row in results if row.get('token_validation_pass') is False]
    verdict = {**copy.deepcopy(job), 'pass': passed, 'adapter_pass': passed,
        'certified_route_contract_pass': False, 'certification_scope': 'fixed_source_parameter_suite',
        'stage': 'param_test', 'reference_source': job['reference_contract_id'],
        'reference_family': job['model_family'], 'param_test_runs': 1,
        'tool_validation_mode': job['parameter_execution']['tool_validation_mode'],
        'compatibility_pass': compatible, 'token_accuracy_pass': token_pass, 'token_validation_pass': token_pass,
        'model_identity_pass': identity_pass, 'full_parameter_matrix_verified': False, 'token_exact_proof': False,
        'identity_probe': None, 'identity_probes': [], 'identity_probe_requests': 0,
        'workflow_result': report, observation_key: observations, 'param_specs': specs,
        'tested_params': specs['tested_params'], 'untested_params': specs['untested_params'],
        'planned_requests': domain.request_cap, 'total': len(results),
        'passed': sum(row.get('compatibility_pass') is True for row in results),
        'overall_passed': len(results) - len(failures), 'failed': len(failures), 'overall_failed': len(failures),
        'expected_rejection': len(expected_rejections), 'expected_rejections': expected_rejections,
        'failures': failures, 'token_failed': len(token_failures), 'token_failures': token_failures,
        'token_audit_summary': token_summary, 'model_identity_summary': identity_summary,
        'performance_summary': runner_module._performance_summary(results)}
    if cache_job:
        verdict.update(fixed_parameter_plan_digest=domain.plan_digest,
            planned_count_requests=sum(r.kind == 'count' for r in domain.requests), planned_generation_requests=3,
            count_requests=observations['count_requests_sent'], generation_requests=observations['generation_requests_sent'],
            count_precision=observations['count_precision'])
    write_json(output_dir / 'param_results.json', results)
    write_json(output_dir / 'token_audit.json', flatten_token_audits(audited_results))
    write_json(output_dir / 'model_identity.json', {'summary': identity_summary, 'probe': None,
        'results': [{key: row.get(key) for key in ('name', 'profile', 'run_index', 'model_identity_audit')} for row in results]})
    observation_filename = {'beta': 'beta_observations.json', 'fim_causal': 'fim_observations.json',
        'anthropic_prefill': 'prefill_observations.json', 'anthropic_cache': 'cache_observations.json'}[kind]
    write_json(output_dir / observation_filename, observations)
    write_json(output_dir / 'verdict.json', redact_secrets(verdict))
    runner_module._write_failed_cases(output_dir, results)
    if cache_job:
        names = ['param_results.json', 'token_audit.json', 'model_identity.json', observation_filename,
                 'verdict.json', 'param_failed_cases.json', 'param_failed_cases.log']
        for run in report['runs']:
            relative = Path('workflow_runs') / run['run_id'] / 'ledger.json'
            if Path(run['ledger_path']).absolute() != output_dir / relative:
                raise ValueError('Fixed workflow ledger escaped its report ownership')
            names.extend((relative, relative.with_name('run.lock')))
        retention.register_report_files(output_dir, names, root=output_dir.parent)
    return verdict
