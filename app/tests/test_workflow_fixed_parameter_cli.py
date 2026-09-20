"""Frozen fixed JobSpec reaches the App public CLI without replacing source bodies."""
import copy
import json

import pytest

from lib.job_spec import make_job_spec
from lib.test_runner.adapters import fixed_parameter as fixed
from test_deepseek_fim_runner import config, client, snapshot, plan, records, cli_setup, fake_session, Response


def frozen_job(cli, domain):
    plan, _ = fixed.prepare_fixed_parameter_plan(domain)
    capability = cli.capability_profile_snapshot('text', 'deepseek', domain.snapshot['execution_target']['request_model_id'],
        list(fixed.fim.CASE_IDS), reference_source=fixed.fim.CONTRACT_ID,
        api_form=fixed.fim.API_FORM, route_profile='vendor_direct', model_profile_database=domain.snapshot)
    capability['model_profile_database'] = domain.snapshot
    job = make_job_spec(job_type='param_test', provider='deepseek_official', model='deepseek-v4-pro',
        workload='param_test', request_mode='fixed', target_rpm=0, target_tpm=0,
        model_family='deepseek', api_form=fixed.fim.API_FORM, route_profile='vendor_direct',
        model_profile_id=capability.get('model_api_profile_id'), model_profile_database=domain.snapshot,
        reference_contract_id=fixed.fim.CONTRACT_ID, model_capability_profile=capability,
        parameter_suite=fixed.fim.SUITE_ID, param_test_runs=1, execution_plan=plan)
    return job


def test_actual_app_cli_reuses_v6_fixed_plan_and_reports_matching_counted_runs(monkeypatch, config, client, plan, records, tmp_path, capsys):
    cli = cli_setup(monkeypatch, config, client, tmp_path)
    job = frozen_job(cli, plan)
    calls = []
    fake_session(monkeypatch, [Response(row) for row in records], calls)
    cli.main(config=config, job_spec=job, output_dir=tmp_path)
    capsys.readouterr()
    verdict = json.loads((tmp_path / 'verdict.json').read_text())
    report = verdict['workflow_result']
    assert len(calls) == report['runs'][0]['business_request_count'] == 8
    assert report['plan_digest'] == job['execution_plan']['plan_digest']
    assert report == verdict['bounded_fim_observations']['workflow_result']
    assert verdict['identity_probe'] is None and verdict['full_parameter_matrix_verified'] is False
    assert [call[2]['data'] for call in calls] == [r.body_bytes for r in plan.requests]
    with pytest.raises(ValueError, match='ledger'):
        cli.main(config=config, job_spec=job, output_dir=tmp_path)
    assert len(calls) == 8


@pytest.mark.parametrize('mutation', ['factory', 'plan', 'config_route', 'config_auth', 'redundant_identity'])
def test_app_fixed_v6_rejects_drift_before_credential_constructor(monkeypatch, config, client, plan, tmp_path, mutation):
    cli = cli_setup(monkeypatch, config, client, tmp_path)
    job = frozen_job(cli, plan)
    if mutation == 'factory': monkeypatch.setattr(fixed, 'execution_digest', lambda: '0' * 64)
    elif mutation == 'plan': job['execution_plan']['ordered_steps'][0]['inputs']['ordinal'] = 7
    elif mutation == 'config_route': config['providers']['deepseek_official']['api_interfaces'][fixed.fim.TRANSPORT]['path'] = '/unapproved/completions'
    elif mutation == 'config_auth': config['providers']['deepseek_official']['api_interfaces'][fixed.fim.TRANSPORT]['auth'] = 'anthropic'
    else:
        job['reference_identity'] = {key: job.get(key) for key in ('source_id', 'profile_id', 'interface_id', 'test_binding_id', 'reference_contract_id')}
        job['reference_identity']['source_id'] = 'anthropic'
    monkeypatch.setattr(cli.DeepSeekClient, 'from_config', lambda *a, **k: pytest.fail('credentials read before fixed preflight'))
    with pytest.raises((ValueError, fixed.IntegrityError)):
        cli.main(config=config, job_spec=job, output_dir=tmp_path)
    assert not list(tmp_path.glob('fim_causal_*_attempt.json'))
