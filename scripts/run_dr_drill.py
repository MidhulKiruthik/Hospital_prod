#!/usr/bin/env python3
"""Run a simple disaster-recovery drill and persist evidence."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def parse_dr_report(stdout_text: str) -> dict:
    for line in (stdout_text or '').splitlines():
        if line.startswith('DR_REPORT='):
            payload = line.split('=', 1)[1].strip()
            if not payload:
                break
            return json.loads(payload)
    raise ValueError('DR_REPORT payload not found in restore output')


def main() -> int:
    parser = argparse.ArgumentParser(description='Execute DR restore drill and write a JSON report')
    parser.add_argument('--backup-dir', default=os.environ.get('BACKUP_DIR', './backups'))
    parser.add_argument('--database-url', default=os.environ.get('DATABASE_URL', 'sqlite:///hospital.db'))
    parser.add_argument('--output', default='dr_drill_report.json')
    parser.add_argument('--backup', default='')
    parser.add_argument('--verify-sha256', default='')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    command = [
        sys.executable,
        str(Path(__file__).with_name('restore_backup.py')),
        '--backup-dir',
        args.backup_dir,
        '--database-url',
        args.database_url,
        '--dr-report',
    ]
    if args.backup:
        command.extend(['--backup', args.backup])
    if args.verify_sha256:
        command.extend(['--verify-sha256', args.verify_sha256])
    if args.force:
        command.append('--force')

    started = datetime.utcnow()
    result = subprocess.run(command, capture_output=True, text=True)
    finished = datetime.utcnow()

    report = {
        'status': 'ok' if result.returncode == 0 else 'error',
        'started_at_utc': started.isoformat() + 'Z',
        'finished_at_utc': finished.isoformat() + 'Z',
        'duration_seconds': max((finished - started).total_seconds(), 0.0),
        'restore_command': command,
        'restore_return_code': result.returncode,
        'restore_stdout_tail': (result.stdout or '')[-2000:],
        'restore_stderr_tail': (result.stderr or '')[-2000:],
    }

    if result.returncode == 0:
        try:
            report['restore_dr_report'] = parse_dr_report(result.stdout)
        except Exception as exc:
            report['status'] = 'error'
            report['parse_error'] = str(exc)

    output_path = Path(args.output).resolve()
    output_path.write_text(json.dumps(report, indent=2), encoding='utf-8')

    if report['status'] != 'ok':
        print(f'DR drill failed, see report: {output_path}')
        return 1

    print(f'DR drill completed: {output_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
