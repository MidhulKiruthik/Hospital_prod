import os
import tempfile
import unittest
from pathlib import Path

from scripts import check_stack, restore_backup


class OpsScriptTests(unittest.TestCase):
    def test_check_stack_url_resolution_defaults_to_frontend(self):
        previous_api = os.environ.pop('HOSPITAL_API_BASE', None)
        previous_frontend = os.environ.pop('HOSPITAL_FRONTEND_BASE', None)
        try:
            api_base, root_base = check_stack.resolve_urls()
            self.assertEqual(api_base, 'http://localhost:8080/api')
            self.assertEqual(root_base, 'http://localhost:8080')
        finally:
            if previous_api is not None:
                os.environ['HOSPITAL_API_BASE'] = previous_api
            if previous_frontend is not None:
                os.environ['HOSPITAL_FRONTEND_BASE'] = previous_frontend

    def test_check_stack_url_resolution_accepts_api_base(self):
        previous_api = os.environ.get('HOSPITAL_API_BASE')
        previous_frontend = os.environ.get('HOSPITAL_FRONTEND_BASE')
        os.environ['HOSPITAL_API_BASE'] = 'http://localhost:5000'
        os.environ['HOSPITAL_FRONTEND_BASE'] = 'http://localhost:8080'
        try:
            api_base, root_base = check_stack.resolve_urls()
            self.assertEqual(api_base, 'http://localhost:5000/api')
            self.assertEqual(root_base, 'http://localhost:5000')
        finally:
            if previous_api is None:
                os.environ.pop('HOSPITAL_API_BASE', None)
            else:
                os.environ['HOSPITAL_API_BASE'] = previous_api
            if previous_frontend is None:
                os.environ.pop('HOSPITAL_FRONTEND_BASE', None)
            else:
                os.environ['HOSPITAL_FRONTEND_BASE'] = previous_frontend

    def test_restore_sqlite_path_resolution_uses_relative_sqlite_path(self):
        resolved = restore_backup.resolve_sqlite_path('sqlite:///hospital.db')
        self.assertEqual(resolved, Path('hospital.db'))

    def test_restore_sqlite_path_resolution_rejects_memory_path(self):
        with self.assertRaises(ValueError):
            restore_backup.resolve_sqlite_path('sqlite:///:memory:')

    def test_restore_sqlite_path_resolution_rejects_unsupported_prefix(self):
        with self.assertRaises(ValueError):
            restore_backup.resolve_sqlite_path('sqlite://hospital.db')

    def test_restore_script_checksum_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / 'sample.db'
            backup_path.write_bytes(b'sample-backup-data')
            digest = '9ba58d79ca4aafa327559a173282f6be1fab2c00262aae2281fca5f354134b23'
            self.assertEqual(restore_backup.compute_sha256(backup_path), digest)


if __name__ == '__main__':
    unittest.main()
