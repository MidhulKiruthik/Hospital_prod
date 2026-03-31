import unittest
from datetime import datetime, timedelta

from helpers import load_app, seed_basic_entities


class AppFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, _ = load_app()
        cls.ids = seed_basic_entities(cls.app_module)
        cls.client = cls.app_module.app.test_client()

    def _login(self, username, password):
        response = self.client.post('/api/auth/login', json={
            'username': username,
            'password': password,
        })
        self.assertEqual(response.status_code, 200)
        return response.get_json()['token']

    def _login_tokens(self, username, password):
        response = self.client.post('/api/auth/login', json={
            'username': username,
            'password': password,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('refresh_token', payload)
        return payload

    def test_privileged_self_registration_is_blocked(self):
        response = self.client.post('/api/auth/register', json={
            'username': 'doctorx',
            'password': 'DoctorX#1234',
            'email': 'doctorx@test.local',
            'role': 'doctor',
        })
        self.assertEqual(response.status_code, 403)

    def test_password_policy_blocks_weak_registration_password(self):
        response = self.client.post('/api/auth/register', json={
            'username': 'weakpassuser',
            'password': 'weakpass',
            'email': 'weak@test.local',
            'role': 'patient',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Password', response.get_json().get('error', ''))

    def test_patient_booking_reassigns_from_overloaded_doctor(self):
        from models import Appointment, db

        patient_token = self._login('patient1', 'Patient#1234')

        with self.app_module.app.app_context():
            for offset in range(12):
                db.session.add(Appointment(
                    patient_id=self.ids['patient_id'],
                    doctor_id=self.ids['doctor_1_id'],
                    scheduled_at=datetime.utcnow() + timedelta(minutes=10 + offset),
                    status='booked',
                    priority='normal',
                    notes='Existing load',
                ))
            for offset in range(2):
                db.session.add(Appointment(
                    patient_id=self.ids['patient_id'],
                    doctor_id=self.ids['doctor_1_id'],
                    scheduled_at=datetime.utcnow() - timedelta(minutes=30 + offset),
                    status='cancelled',
                    priority='normal',
                    notes='Cancelled load',
                ))
            db.session.commit()

        response = self.client.post('/api/appointments', json={
            'specialty': 'Cardiology',
            'scheduled_at': (datetime.utcnow() + timedelta(hours=1)).isoformat(),
            'doctor_id': self.ids['doctor_1_id'],
            'notes': 'Need follow-up',
            'priority': 'normal',
        }, headers={'Authorization': f'Bearer {patient_token}'})

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertTrue(payload['reassigned'])
        self.assertEqual(payload['appointment']['doctor_id'], self.ids['doctor_2_id'])

    def test_summary_regeneration_sync_fallback_returns_generation_metadata(self):
        admin_token = self._login('admin', 'AdminPassword#123')

        create_response = self.client.post('/api/appointments', json={
            'patient_id': self.ids['patient_id'],
            'specialty': 'Cardiology',
            'scheduled_at': (datetime.utcnow() + timedelta(hours=2)).isoformat(),
            'doctor_id': self.ids['doctor_2_id'],
            'notes': 'Patient c/o fever for 3 days. BP: 120/80. Diagnosis: Viral infection.',
            'priority': 'normal',
        }, headers={'Authorization': f'Bearer {admin_token}'})
        self.assertEqual(create_response.status_code, 201)
        appointment_id = create_response.get_json()['appointment']['id']

        complete_response = self.client.put(
            f'/api/appointments/{appointment_id}/complete',
            json={'notes': 'Patient c/o fever for 3 days. BP: 120/80. Diagnosis: Viral infection.'},
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(complete_response.status_code, 200)

        response = self.client.post(
            f'/api/summaries/{appointment_id}/regenerate',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('generation_method', payload)

    def test_login_lockout_after_repeated_failed_attempts(self):
        from models import User, db

        for _ in range(5):
            response = self.client.post('/api/auth/login', json={
                'username': 'patient1',
                'password': 'WrongPassword#999',
            })
            self.assertEqual(response.status_code, 401)

        blocked = self.client.post('/api/auth/login', json={
            'username': 'patient1',
            'password': 'Patient#1234',
        })
        self.assertEqual(blocked.status_code, 423)

        with self.app_module.app.app_context():
            user = User.query.filter_by(username='patient1').first()
            user.failed_login_attempts = 0
            user.lockout_until = None
            db.session.commit()

    def test_audit_integrity_endpoint_returns_ok(self):
        admin_token = self._login('admin', 'AdminPassword#123')
        response = self.client.get(
            '/api/audit/integrity',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get('ok'))

    def test_refresh_and_logout_flow(self):
        tokens = self._login_tokens('admin', 'AdminPassword#123')

        refresh_response = self.client.post('/api/auth/refresh', json={
            'refresh_token': tokens['refresh_token'],
        })
        self.assertEqual(refresh_response.status_code, 200)
        self.assertIn('access_token', refresh_response.get_json())

        logout_response = self.client.post(
            '/api/auth/logout',
            json={'refresh_token': tokens['refresh_token']},
            headers={'Authorization': f"Bearer {tokens['access_token']}"},
        )
        self.assertEqual(logout_response.status_code, 200)

        refresh_after_logout = self.client.post('/api/auth/refresh', json={
            'refresh_token': tokens['refresh_token'],
        })
        self.assertEqual(refresh_after_logout.status_code, 401)

    def test_scheduling_blocks_double_booking_same_doctor_slot(self):
        admin_token = self._login('admin', 'AdminPassword#123')
        scheduled = (datetime.utcnow() + timedelta(hours=3)).replace(microsecond=0)

        first = self.client.post('/api/appointments', json={
            'patient_id': self.ids['patient_id'],
            'specialty': 'Cardiology',
            'scheduled_at': scheduled.isoformat(),
            'doctor_id': self.ids['doctor_2_id'],
            'notes': 'first booking',
            'priority': 'normal',
        }, headers={'Authorization': f'Bearer {admin_token}'})
        self.assertEqual(first.status_code, 201)

        second = self.client.post('/api/appointments', json={
            'patient_id': self.ids['patient_id'],
            'specialty': 'Cardiology',
            'scheduled_at': scheduled.isoformat(),
            'doctor_id': self.ids['doctor_2_id'],
            'notes': 'second booking',
            'priority': 'normal',
        }, headers={'Authorization': f'Bearer {admin_token}'})
        self.assertIn(second.status_code, (409, 422))

    def test_forecast_history_endpoint_available(self):
        admin_token = self._login('admin', 'AdminPassword#123')
        history = self.client.get(
            '/api/forecast/history?scope=hospital&limit=5',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(history.status_code, 200)
        self.assertIsInstance(history.get_json(), list)

    def test_security_events_endpoint_available(self):
        admin_token = self._login('admin', 'AdminPassword#123')
        response = self.client.get(
            '/api/security/events?limit=5',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.get_json(), list)

    def test_summary_review_and_revision_flow(self):
        admin_token = self._login('admin', 'AdminPassword#123')
        create_response = self.client.post('/api/appointments', json={
            'patient_id': self.ids['patient_id'],
            'specialty': 'Cardiology',
            'scheduled_at': (datetime.utcnow() + timedelta(hours=4)).isoformat(),
            'doctor_id': self.ids['doctor_2_id'],
            'notes': 'Patient has cough and mild fever for 2 days',
            'priority': 'normal',
        }, headers={'Authorization': f'Bearer {admin_token}'})
        self.assertEqual(create_response.status_code, 201)
        appt_id = create_response.get_json()['appointment']['id']

        self.client.put(
            f'/api/appointments/{appt_id}/complete',
            json={'notes': 'Patient has cough and mild fever for 2 days'},
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.client.post(
            f'/api/summaries/{appt_id}/regenerate',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        review = self.client.put(
            f'/api/summaries/{appt_id}/review',
            json={
                'summary_text': 'Clinician reviewed summary text',
                'review_note': 'corrected wording',
            },
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(review.status_code, 200)
        review_payload = review.get_json()
        self.assertTrue(review_payload.get('is_reviewed'))

        revisions = self.client.get(
            f'/api/summaries/{appt_id}/revisions',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        self.assertEqual(revisions.status_code, 200)
        self.assertTrue(len(revisions.get_json()) >= 1)


if __name__ == '__main__':
    unittest.main()
