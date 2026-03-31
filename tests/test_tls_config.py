import unittest
from pathlib import Path


class TlsConfigTests(unittest.TestCase):
    def test_nginx_uses_real_certificate_paths(self):
        conf = Path('frontend/nginx.conf').read_text(encoding='utf-8')
        self.assertIn('/etc/nginx/certs/fullchain.pem', conf)
        self.assertIn('/etc/nginx/certs/privkey.pem', conf)

    def test_entrypoint_blocks_missing_certs_without_override(self):
        script = Path('frontend/docker-entrypoint.d/10-generate-cert.sh').read_text(encoding='utf-8')
        self.assertIn('ALLOW_SELF_SIGNED_TLS', script)
        self.assertIn('mount real certificates', script)


if __name__ == '__main__':
    unittest.main()
