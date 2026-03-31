import unittest

from scripts.run_dr_drill import parse_dr_report


class DrDrillScriptTests(unittest.TestCase):
    def test_parse_dr_report_reads_embedded_json(self):
        payload = parse_dr_report('line1\nDR_REPORT={"status":"ok","database":"sqlite"}\nline3')
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['database'], 'sqlite')

    def test_parse_dr_report_raises_when_missing(self):
        with self.assertRaises(ValueError):
            parse_dr_report('no dr payload here')


if __name__ == '__main__':
    unittest.main()
