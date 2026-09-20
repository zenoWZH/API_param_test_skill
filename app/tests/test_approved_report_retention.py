"""Only new fixed-cache producers can register App files for P1M deletion."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import approved_report_retention as bridge
from lib.anthropic_cache_reference import SUITE_IDS


@pytest.fixture
def batch(tmp_path):
    report_root=tmp_path/'reports'; path=bridge.cache_report_directory(reports_root=report_root)
    bridge.initialize_cache_report(path,SUITE_IDS[0],reports_root=report_root,on_exit=False)
    return report_root,path


def test_shared_kernel_is_identical_to_frozen_audited_root():
    root=Path(__file__).resolve().parents[1]
    assert Path(bridge.retention.__file__).read_bytes()==(root/'lib/report_retention.py').read_bytes()


def test_fresh_jobs_have_unique_managed_paths(tmp_path):
    a=bridge.cache_report_directory(reports_root=tmp_path)
    b=bridge.cache_report_directory(reports_root=tmp_path)
    assert a!=b and a.parent==b.parent==tmp_path/'jobs'
    with pytest.raises(ValueError):bridge.cache_report_directory(tmp_path/'outside',reports_root=tmp_path)


def test_no_adoption_of_old_or_other_suite_reports(tmp_path):
    p=tmp_path/'jobs'/'old';p.mkdir(parents=True);(p/'verdict.json').write_text('{}')
    with pytest.raises(ValueError):bridge.initialize_cache_report(p,SUITE_IDS[0],reports_root=tmp_path,on_exit=False)
    assert not (p/bridge.retention.MANIFEST_NAME).exists()
    with pytest.raises(ValueError):bridge.initialize_cache_report(tmp_path/'jobs'/'other','deepseek_fim_causal_20260908',reports_root=tmp_path,on_exit=False)


def test_job_spec_must_match_exact_suite_and_type(tmp_path):
    p=tmp_path/'jobs'/'job';p.mkdir(parents=True)
    (p/'job_spec.json').write_text(json.dumps({'type':'param_test','parameter_suite':SUITE_IDS[1]}))
    with pytest.raises(ValueError):bridge.initialize_cache_report(p,SUITE_IDS[0],reports_root=tmp_path,on_exit=False)
    bridge.initialize_cache_report(p,SUITE_IDS[1],reports_root=tmp_path,on_exit=False)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text())
    assert [r['path'] for r in m['owned_files']]==['job_spec.json']


def test_reinitialization_never_extends_expiry_and_reentry_fails(batch):
    root,p=batch
    before=(p/bridge.retention.MANIFEST_NAME).read_bytes()
    bridge.initialize_cache_report(p,SUITE_IDS[0],reports_root=root,on_exit=False)
    assert (p/bridge.retention.MANIFEST_NAME).read_bytes()==before
    (p/'anthropic_cache_dispatch_started.json').write_text('{}')
    bridge.register_cache_immutable(p,'anthropic_cache_dispatch_started.json',reports_root=root)
    with pytest.raises(ValueError):bridge.initialize_cache_report(p,SUITE_IDS[0],reports_root=root,on_exit=False)


def test_cli_and_web_finalize_after_their_last_writer_closes(batch):
    root,p=batch
    (p/'param_results.json').write_text('[]');(p/'job.log').write_text('running')
    bridge.finalize_cache_report(p,reports_root=root)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text())
    assert [r['path'] for r in m['owned_files']]==['param_results.json']
    (p/'job.log').write_text('completed')
    bridge.finalize_cache_report(p,reports_root=root,include_job_log=True)
    bridge.finalize_cache_report(p,reports_root=root,include_job_log=True)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text())
    assert {r['path'] for r in m['owned_files']}=={'param_results.json','job.log'}


def test_cleanup_deletes_only_owned_unchanged_files_at_calendar_month(batch):
    root,p=batch
    (p/'verdict.json').write_text('{}');(p/'unregistered.txt').write_text('keep')
    bridge.finalize_cache_report(p,reports_root=root)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text());expiry=datetime.fromisoformat(m['expires_at'].replace('Z','+00:00'))
    assert bridge.cleanup_cache_reports(reports_root=root,apply=True,now=expiry-timedelta(seconds=1))['batches'][0]['status']=='not_expired'
    result=bridge.cleanup_cache_reports(reports_root=root,apply=True,now=expiry)
    assert result['batches'][0]['files'][0]['status']=='deleted'
    assert not (p/'verdict.json').exists() and (p/'unregistered.txt').read_text()=='keep'
    old=root/'jobs'/'unmanaged';old.mkdir();(old/'verdict.json').write_text('{}')
    bridge.cleanup_cache_reports(reports_root=root,apply=True,now=expiry)
    assert (old/'verdict.json').exists()


def test_changed_or_symlink_reports_cannot_be_adopted_or_deleted(batch):
    root,p=batch
    (p/'verdict.json').write_text('{}');bridge.finalize_cache_report(p,reports_root=root)
    (p/'verdict.json').write_text('{"edited":true}')
    with pytest.raises(ValueError):bridge.finalize_cache_report(p,reports_root=root)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text());expiry=datetime.fromisoformat(m['expires_at'].replace('Z','+00:00'))
    result=bridge.cleanup_cache_reports(reports_root=root,apply=True,now=expiry)
    assert result['batches'][0]['files'][0]['reason']=='report_changed'
    (p/'token_audit.json').symlink_to(p/'verdict.json')
    with pytest.raises((ValueError,OSError)):bridge.finalize_cache_report(p,reports_root=root)


@pytest.mark.parametrize('name',['other.json','../verdict.json','anthropic_cache_06_attempt.json','job.log'])
def test_immutable_registration_has_exact_filename_scope(batch,name):
    root,p=batch
    with pytest.raises(ValueError):bridge.register_cache_immutable(p,name,reports_root=root)


def test_expired_manifest_refuses_new_cache_run(batch):
    root,p=batch;m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text())
    m.update(created_at='2020-01-31T00:00:00Z',expires_at='2020-02-29T00:00:00Z')
    (p/bridge.retention.MANIFEST_NAME).write_text(json.dumps(m))
    with pytest.raises(ValueError,match='expired'):bridge.initialize_cache_report(p,SUITE_IDS[0],reports_root=root,on_exit=False)


def test_web_lifecycle_registers_log_only_after_terminal(tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_API_TEST_REPORTS_DIR',str(tmp_path/'reports'))
    from scripts import web_console as web
    monkeypatch.setattr(web,'REPORTS_ROOT',tmp_path/'reports')
    p=web.REPORTS_ROOT/'jobs'/'test';p.mkdir(parents=True)
    spec={'type':'param_test','parameter_suite':SUITE_IDS[0]};(p/'job_spec.json').write_text(json.dumps(spec))
    job=SimpleNamespace(type='param_test',job_spec=spec,report_dir=p,created_at=datetime.now(timezone.utc).timestamp())
    web._initialize_fixed_cache_retention(job);(p/'job.log').write_text('done')
    web._finalize_fixed_cache_retention(job)
    m=json.loads((p/bridge.retention.MANIFEST_NAME).read_text())
    assert {r['path'] for r in m['owned_files']}=={'job_spec.json','job.log'}
