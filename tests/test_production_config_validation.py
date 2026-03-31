import unittest

from helpers import load_app


class ProductionConfigValidationTests(unittest.TestCase):
    def test_production_rejects_weak_and_local_defaults(self):
        with self.assertRaises(RuntimeError):
            load_app(
                {
                    'APP_ENV': 'production',
                    'SECRET_KEY': 'weak-secret',
                    'JWT_SECRET_KEY': 'weak-jwt-secret',
                    'DATABASE_URL': 'sqlite:///prod.db',
                    'CORS_ORIGINS': 'http://localhost:8080',
                    'ENFORCE_HTTPS': 'False',
                }
            )


if __name__ == '__main__':
    unittest.main()
