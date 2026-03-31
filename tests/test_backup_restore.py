import hashlib
import os
import tempfile
import unittest
from pathlib import Path


class BackupRestoreScriptTests(unittest.TestCase):
    def test_restore_script_checksum_guard(self):
        from scripts import restore_backup

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / 'sample.db'
            backup_path.write_bytes(b'sample-backup-data')
            digest = hashlib.sha256(b'sample-backup-data').hexdigest()

            calculated = restore_backup.compute_sha256(backup_path)
            self.assertEqual(calculated, digest)


if __name__ == '__main__':
    unittest.main()
