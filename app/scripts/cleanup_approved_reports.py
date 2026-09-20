#!/usr/bin/env python3
"""Expire only explicitly registered fixed cache job reports in the App root."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.approved_report_retention import cleanup_cache_reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports-root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = cleanup_cache_reports(reports_root=args.reports_root, apply=args.apply)
    except (OSError, ValueError):
        print(json.dumps({'error':'unsafe_or_unreadable_report_root','apply':args.apply}))
        return 2
    print(json.dumps(result, indent=2))
    return int(any(b.get('reason') or any(f.get('reason') for f in b['files']) for b in result['batches']))


if __name__ == '__main__':
    raise SystemExit(main())
