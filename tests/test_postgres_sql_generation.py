import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import Appointment
from sqlalchemy import select


class PostgresSqlGenerationTests(unittest.TestCase):
    def test_compile_query_for_postgres_dialect(self):
        from sqlalchemy.dialects import postgresql

        stmt = select(Appointment).where(Appointment.status == 'booked')
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        self.assertIn('appointments', compiled)
        self.assertIn('status', compiled)


if __name__ == '__main__':
    unittest.main()
