"""P1M ownership for new fixed Anthropic cache jobs only.

The package's internal retention kernel is the byte-identical audited root
kernel; all calls supply an explicit App report root. Existing reports without
our manifest are never adopted by cleanup.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from . import model_profile_catalog as _catalog_bridge  # Shared-package bootstrap.
from model_profile_db import _report_retention as retention
from .config import default_reports_root

SUMMARY_FILES = ('param_results.json', 'cache_observations.json', 'token_audit.json', 'model_identity.json',
                 'verdict.json', 'param_failed_cases.json', 'param_failed_cases.log')
IMMUTABLE = re.compile(r'(?:fixed_parameter_plan|job_spec|anthropic_cache_dispatch_started|anthropic_cache_dispatch_finished|anthropic_cache_0[1-5]_(?:attempt|observation))\.json\Z')


def jobs_root(reports_root=None):
    return Path(os.path.abspath(reports_root if reports_root is not None else default_reports_root())) / 'jobs'


def cache_report_directory(requested=None, *, reports_root=None):
    root = jobs_root(reports_root)
    batch = (Path(os.path.abspath(requested)) if requested else root / (
        'param_test_anthropic_cache_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:12]))
    if batch.parent != root or batch.is_symlink():
        raise retention.RetentionError('Fixed cache reports require a direct batch under the managed reports/jobs root for P1M cleanup')
    return batch


def _suite(suite_id):
    from .anthropic_cache_reference import SUITE_IDS
    if suite_id not in SUITE_IDS:
        raise retention.RetentionError('Report retention is limited to the explicit approved cache suites')


def initialize_cache_report(batch, suite_id, *, reports_root=None, on_exit=True, created_at=None):
    _suite(suite_id)
    batch = cache_report_directory(batch, reports_root=reports_root)
    root = jobs_root(reports_root)
    if batch.exists():
        with retention._batch_directory(batch) as fd:
            names = set(os.listdir(fd))
            manifest = retention.MANIFEST_NAME in names
            if any(name.startswith('anthropic_cache_') for name in names) or names.intersection(SUMMARY_FILES):
                raise retention.RetentionError('Cache batch already has execution evidence; create a new batch')
            if not manifest and names - {'job_spec.json', 'job.log'}:
                raise retention.RetentionError('Refusing to adopt or overwrite an existing non-cache report')
            if not manifest and 'job.log' in names and 'job_spec.json' not in names:
                raise retention.RetentionError('An existing log without this cache JobSpec cannot be adopted')
            if 'job_spec.json' in names:
                with retention._file_parent(fd, 'job_spec.json') as (parent, name):
                    file_fd = os.open(name, retention._FILE_FLAGS, dir_fd=parent)
                    with os.fdopen(file_fd, 'r', encoding='utf-8') as source:
                        job = json.load(source)
                if job.get('parameter_suite') != suite_id or job.get('type') != 'param_test':
                    raise retention.RetentionError('Existing JobSpec does not describe this exact cache suite')
    meta = retention.initialize_report_retention(batch, root=root, created_at=created_at)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00')):
        raise retention.RetentionError('The cache report has expired; create a new batch')
    if (batch / 'job_spec.json').exists():
        register_cache_immutable(batch, 'job_spec.json', reports_root=reports_root)
    if on_exit:
        # The CLI closes its summaries before normal exit or KeyboardInterrupt.
        # The Web parent owns job.log and registers it only after process.wait.
        atexit.register(_finalize_on_exit, batch, root.parent)
    return meta


def register_cache_immutable(batch, name, *, reports_root=None):
    batch = cache_report_directory(batch, reports_root=reports_root)
    if not isinstance(name, str) or not IMMUTABLE.fullmatch(name):
        raise retention.RetentionError('Not a fixed cache immutable report filename')
    return retention.register_report_files(batch, [name], root=jobs_root(reports_root))


def register_cache_workflow(batch, report, *, reports_root=None):
    """Own only finalized ledger files named by this producer's actual run report."""
    batch = cache_report_directory(batch, reports_root=reports_root)
    if type(report) is not dict or type(report.get("runs")) is not list:
        raise retention.RetentionError("Missing fixed cache workflow run report")
    paths = []
    for run in report["runs"]:
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise retention.RetentionError("Invalid fixed cache workflow run ID")
        relative = f"workflow_runs/{run_id}/ledger.json"
        if Path(run.get("ledger_path", "")).absolute() != (batch / relative).absolute():
            raise retention.RetentionError("Fixed cache workflow ledger is outside its report")
        paths.extend((relative, f"workflow_runs/{run_id}/run.lock"))
    return retention.register_report_files(batch, paths, root=jobs_root(reports_root))


def finalize_cache_report(batch, *, reports_root=None, include_job_log=False):
    batch = cache_report_directory(batch, reports_root=reports_root)
    if not (batch / retention.MANIFEST_NAME).exists():
        return None
    names = SUMMARY_FILES + (('job.log',) if include_job_log else ())
    # Explicit producer filenames only; never discover other files to own.
    existing = [name for name in names if (batch / name).exists() or (batch / name).is_symlink()]
    return retention.register_report_files(batch, existing, root=jobs_root(reports_root))


def _finalize_on_exit(batch, reports_root):
    try:
        finalize_cache_report(batch, reports_root=reports_root)
    except (OSError, ValueError):
        # Do not conceal an integrity problem by adopting edited files. The
        # explicit finalizer still raises during normal CLI/Web completion.
        pass


def cleanup_cache_reports(*, reports_root=None, apply=False, now=None):
    return retention.cleanup_approved_reports(root=jobs_root(reports_root), apply=apply, now=now)
