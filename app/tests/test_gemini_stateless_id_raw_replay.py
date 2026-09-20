"""Optional local P1M replay through the production profile validator."""
import hashlib
import json
import os
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from lib.profile_validation import validate_profile_response
from lib.report_retention import _batch_directory,_read_manifest,_snapshot

ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'references/ai_studio_stateless_cache_facts_20260909.json').is_file():ROOT=ROOT.parent
FACT='references/ai_studio_stateless_cache_facts_20260909.json'
FACT_SHA='d7a4e2217cb4183b89d146cd9a470244cf287aa3ab0de90fb1f8412d3556edf4'


def retained(link):
    path=ROOT/link['path'];assert path.parent.parent==ROOT/'reports/approved_live_20260907'
    if not path.parent.exists():pytest.skip('Local P1M evidence batch is unavailable')
    with _batch_directory(path.parent) as fd:
        meta=_read_manifest(fd,path.parent,datetime.now(timezone.utc))
        if datetime.now(timezone.utc)>=datetime.fromisoformat(meta['expires_at'].replace('Z','+00:00')):
            pytest.skip('Local evidence has reached P1M expiry')
        owner=next(row for row in meta['owned_files'] if row['path']==path.name)
        assert _snapshot(fd,path.name)==owner and owner['sha256']==link['sha256']
        with os.fdopen(os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd),'rb') as stream:
            raw=stream.read(owner['size']+1)
        assert len(raw)==owner['size'] and hashlib.sha256(raw).hexdigest()==link['sha256'] and _snapshot(fd,path.name)==owner
    return json.loads(raw)


def test_all_six_retained_stateless_responses_pass_production_validation_without_id():
    raw=(ROOT/FACT).read_bytes();assert hashlib.sha256(raw).hexdigest()==FACT_SHA
    count=0
    for sample in json.loads(raw)['observations']:
        for control in sample['controls']:
            attempt=retained(control['evidence']['attempt']);payload=retained(control['evidence']['response'])
            assert 'id' not in payload and attempt['case']['body']['store'] is False
            result=SimpleNamespace(success=True,usage=payload['usage'])
            assert validate_profile_response('gemini_3_7_flash_interactions_basic',payload,result,
                request_body=attempt['case']['body'],transport='gemini_interactions',reference_source='gemini_3_7_flash_interactions',
                request_context={'requested_model':attempt['case']['body']['model'],'request_url':attempt['case']['url']}) is None
            count+=1
    assert count==6
