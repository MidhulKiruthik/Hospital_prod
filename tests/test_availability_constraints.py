import unittest
from datetime import datetime, timedelta

from helpers import load_app, seed_basic_entities


class AvailabilityConstraintTests(unittest.TestCase):
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

    def test_break_window_not_returned_in_slots(self):
        token = self._login_patient()

        # Create a doctor-specific availability window through DB for deterministic test
        with self.app_module.app.app_context():
            from models import DoctorAvailabilityCompat, db

            DoctorAvailabilityCompat.query.filter_by(doctor_id=self.ids['doctor_2_id']).delete()
            db.session.add(DoctorAvailabilityCompat(
                doctor_id=self.ids['doctor_2_id'],
                day_of_week=(datetime.utcnow() + timedelta(days=1)).strftime('%A'),
                start_time='09:00',
                end_time='12:00',
                slot_duration=30,
                break_start='10:00',
                break_end='10:30',
            ))
            db.session.commit()

        target_date = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
        response = self.client.get(
            f"/api/patient/doctors/{self.ids['doctor_2_id']}/slots?date={target_date}",
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn('10:00', payload.get('slots', []))


if __name__ == '__main__':
    unittest.main()
