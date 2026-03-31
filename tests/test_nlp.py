import os
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from nlp import generate_clinical_summary


class ClinicalSummaryTests(unittest.TestCase):
    def test_rule_based_summary_extracts_expected_sections(self):
        os.environ['ENABLE_TRANSFORMER_SUMMARIZATION'] = 'False'
        result = generate_clinical_summary(
            'Patient c/o chest pain for 2 weeks. BP: 145/90. ECG ordered. '
            'Likely hypertension. Start amlodipine 5mg OD.'
        )

        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['method'], 'rule-based-nlp')
        self.assertIn('chest pain', result['chief_complaint'].lower())
        self.assertIn('hypertension', result['assessment'].lower())
        self.assertIn('amlodipine', result['plan'].lower())


if __name__ == '__main__':
    unittest.main()
