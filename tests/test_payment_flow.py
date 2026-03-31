import unittest
from datetime import datetime, timedelta
import uuid

from helpers import load_app, seed_basic_entities


class PaymentFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, _ = load_app()
        cls.ids = seed_basic_entities(cls.app_module)
        cls.client = cls.app_module.app.test_client()

    def _login_patient(self):
        response = self.client.post('/api/auth/login', json={
            'username': 'patient1',
            'password': 'Patient#1234',
        })
        self.assertEqual(response.status_code, 200)
        return response.get_json()['access_token']

    def test_payment_verify_requires_signature_fields(self):
        token = self._login_patient()

        # Simulate an existing order row directly
        with self.app_module.app.app_context():
            from models import PaymentOrderCompat, db

            row = PaymentOrderCompat(
                order_id=f'order_test_{uuid.uuid4().hex[:10]}',
                patient_id=self.ids['patient_id'],
                doctor_id=self.ids['doctor_2_id'],
                appointment_date=(datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d'),
                appointment_time='10:00',
                reason='checkup',
                amount_cents=50000,
                status='created',
            )
            db.session.add(row)
            db.session.commit()
            order_id = row.order_id

        response = self.client.post(
            '/api/patient/appointments/verify-payment',
            json={'razorpay_order_id': order_id},
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn('ui_error', payload)


if __name__ == '__main__':
    unittest.main()
