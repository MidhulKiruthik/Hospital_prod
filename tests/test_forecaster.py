import unittest
from datetime import datetime, timedelta

from helpers import load_app, seed_basic_entities


class ForecastingTests(unittest.TestCase):
    def test_requested_advanced_model_falls_back_cleanly_when_unavailable(self):
        app_module, _ = load_app({'FORECAST_MODEL': 'arima'})
        ids = seed_basic_entities(app_module)

        from models import WorkloadMetric, db

        with app_module.app.app_context():
            now = datetime.utcnow()
            for index, score in enumerate([0.22, 0.31, 0.44, 0.47, 0.5, 0.56]):
                db.session.add(WorkloadMetric(
                    doctor_id=ids['doctor_2_id'],
                    score=score,
                    timestamp=now - timedelta(minutes=(30 - index * 5)),
                ))
            db.session.commit()

            result = app_module.forecast_workload(ids['doctor_2_id'], horizon_minutes=60)

        self.assertEqual(result['selected_model'], 'arima')
        self.assertIn(result['effective_model'], ('arima+poisson', 'ewma+linear+poisson', 'baseline-default'))
        self.assertTrue(len(result['forecast']) >= 2)
        self.assertIn('model_comparison', result)
        self.assertIn('mae', result)
        self.assertIn('rmse', result)
        self.assertIn('backtest', result)


if __name__ == '__main__':
    unittest.main()
