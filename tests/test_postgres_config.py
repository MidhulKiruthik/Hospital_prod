import unittest

from helpers import BACKEND_DIR

import sys

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import normalize_database_url


class PostgresConfigTests(unittest.TestCase):
    def test_postgres_database_url_normalization(self):
        uri = normalize_database_url(
            'postgresql://hospital_ops:password@localhost:5432/hospital_ops'
        )
        self.assertTrue(uri.startswith('postgresql+psycopg://'))


if __name__ == '__main__':
    unittest.main()
