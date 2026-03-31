import os
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from nlp import generate_clinical_summary


class TransformerModeTests(unittest.TestCase):
    def test_transformer_toggle_keeps_summary_stable_when_model_missing(self):
        os.environ['ENABLE_TRANSFORMER_SUMMARIZATION'] = 'True'
        os.environ['TRANSFORMER_MODEL_NAME'] = 'this-model-does-not-exist'

        result = generate_clinical_summary(
            'Patient complains of headache for 2 days. BP: 130/85. Assessment migraine. Plan: rest and hydration.'
        )

        self.assertEqual(result['status'], 'ready')
        self.assertIn(result['method'], ('rule-based-nlp', 'transformer+rule-based'))
        self.assertIn('chief_complaint', result)
        self.assertIn('plan', result)


if __name__ == '__main__':
    unittest.main()
