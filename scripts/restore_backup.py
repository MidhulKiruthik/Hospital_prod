#!/usr/bin/env python3
"""Restore the latest or selected backup into SQLite or PostgreSQL."""

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def resolve_sqlite_path(database_url: str) -> Path:
    value = (database_url or '').strip()
    parsed = urlparse(value)
    if parsed.scheme != 'sqlite':
        raise ValueError('Only sqlite DATABASE_URL values are supported by this restore helper')
    if not value.startswith('sqlite:///'):
        raise ValueError('sqlite DATABASE_URL must start with sqlite:///')

    raw_path = value[len('sqlite:///') :]
    if not raw_path or raw_path == ':memory:':
        raise ValueError('sqlite restore requires a filesystem path, not in-memory DB')

    return Path(raw_path)


def latest_backup(backup_dir: Path, extension: str) -> Path:
    candidates = sorted(backup_dir.glob(f'*{extension}'), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f'No {extension} backups found in {backup_dir}')
    return candidates[0]


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Restore a hospital-ops backup')
    parser.add_argument('--backup', help='Specific backup file to restore')
    parser.add_argument('--backup-dir', default=os.environ.get('BACKUP_DIR', './backups'))
    parser.add_argument(
        '--database-url',
        default=os.environ.get('DATABASE_URL', 'sqlite:///hospital.db'),
        help='Target DATABASE_URL to restore into',
    )
    parser.add_argument('--force', action='store_true', help='Overwrite without keeping a pre-restore snapshot')
    parser.add_argument('--verify-sha256', default='', help='Expected SHA-256 for the selected backup')
    parser.add_argument(
        '--dr-report',
        action='store_true',
        help='Print machine-readable DR verification details after restore',
    )
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir).resolve()
    expected_ext = '.db' if str(args.database_url).startswith('sqlite') else '.sql'
    backup_path = Path(args.backup).resolve() if args.backup else latest_backup(backup_dir, expected_ext)
    if not backup_path.exists():
        raise FileNotFoundError(f'Backup file not found: {backup_path}')

    if args.verify_sha256:
        calculated = compute_sha256(backup_path)
        if calculated.lower() != args.verify_sha256.strip().lower():
            raise ValueError('Backup checksum mismatch; refusing restore')

    parsed = urlparse(args.database_url)
    if parsed.scheme == 'sqlite':
        db_path = resolve_sqlite_path(args.database_url).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        if db_path.exists() and not args.force:
            snapshot = db_path.with_suffix(db_path.suffix + f".pre_restore_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(db_path, snapshot)
            print(f'Created safety snapshot: {snapshot}')

        shutil.copy2(backup_path, db_path)
        print(f'Restored {backup_path} -> {db_path}')
        if args.dr_report:
            print('DR_REPORT=' + json.dumps({
                'status': 'ok',
                'database': 'sqlite',
                'backup': str(backup_path),
                'target': str(db_path),
                'verified_sha256': bool(args.verify_sha256),
            }))
        return

    if parsed.scheme.startswith('postgres'):
        import subprocess

        command = [
            'psql',
            args.database_url,
            '-v',
            'ON_ERROR_STOP=1',
            '-f',
            str(backup_path),
        ]
        subprocess.run(command, check=True)
        print(f'Restored {backup_path} into PostgreSQL database')
        if args.dr_report:
            print('DR_REPORT=' + json.dumps({
                'status': 'ok',
                'database': 'postgres',
                'backup': str(backup_path),
                'target': args.database_url,
                'verified_sha256': bool(args.verify_sha256),
            }))
        return

    raise ValueError(f'Unsupported DATABASE_URL scheme: {parsed.scheme}')


if __name__ == '__main__':
    main()
