import importlib
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


MODULES_TO_RESET = [
    'app',
    'auth',
    'config',
    'models',
    'scheduler',
    'forecaster',
    'nlp',
    'tasks',
    'celery_worker',
]


def load_app(extra_env=None):
    db_filename = f"tests_runtime_{uuid.uuid4().hex}.db"
    db_path = ROOT / db_filename
    if db_path.exists():
        db_path.unlink()

    env = {
        'APP_ENV': 'development',
        'DATABASE_URL': f'sqlite:///{db_filename}',
        'SECRET_KEY': 'test-secret',
        'JWT_SECRET_KEY': 'test-jwt-secret',
        'DATA_ENCRYPTION_KEY': 'MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=',
        'REDIS_URL': 'memory://',
        'RATE_LIMIT_STORAGE_URI': 'memory://',
        'AUTH_RATE_LIMIT': '1000 per minute',
        'DEFAULT_RATE_LIMIT': '10000 per hour',
        'AUTO_RUN_MIGRATIONS': 'False',
        'AUTO_CREATE_SCHEMA': 'True',
        'SEED_DEMO_DATA': 'False',
        'ALLOW_PRIVILEGED_SELF_REGISTRATION': 'False',
        'BOOTSTRAP_ADMIN_USERNAME': 'admin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'AdminPassword#123',
        'BOOTSTRAP_ADMIN_EMAIL': 'admin@test.local',
        'CORS_ORIGINS': 'http://localhost:8080',
        'CORS_ORIGINS_PRODUCTION': 'https://hospital.example.com',
        'FORECAST_MODEL': 'baseline',
        'ENABLE_TRANSFORMER_SUMMARIZATION': 'False',
        'BACKUP_HEALTH_CHECK_ENABLED': 'False',
        'REFRESH_TOKEN_EXPIRATION_DAYS': '7',
        'SECURITY_EVENT_EXPORT_ENABLED': 'False',
        'SECURITY_EVENT_WEBHOOK_URL': '',
    }
    if extra_env:
        env.update(extra_env)

    for key, value in env.items():
        os.environ[key] = value

    for module_name in MODULES_TO_RESET:
        sys.modules.pop(module_name, None)

    app_module = importlib.import_module('app')
    return app_module, db_path


def seed_basic_entities(app_module):
    from auth import hash_password
    from models import Doctor, Patient, User, db
    from security_utils import encrypt_text

    with app_module.app.app_context():
        admin = User.query.filter_by(username='admin').first()

        patient_user = User(
            username='patient1',
            password_hash=hash_password('Patient#1234'),
            role='patient',
            email='patient1@test.local',
        )
        db.session.add(patient_user)
        db.session.flush()

        patient = Patient(
            user_id=patient_user.id,
            name='Patient One',
            email=encrypt_text('patient1@test.local'),
            phone=encrypt_text('9876543210'),
        )
        db.session.add(patient)

        doctor_user_1 = User(
            username='doctora',
            password_hash=hash_password('Doctor#1234'),
            role='doctor',
            email='doctora@test.local',
        )
        db.session.add(doctor_user_1)
        db.session.flush()
        doctor_1 = Doctor(
            user_id=doctor_user_1.id,
            name='Dr. Alpha',
            specialty='Cardiology',
            max_per_day=1,
        )
        db.session.add(doctor_1)

        doctor_user_2 = User(
            username='doctorb',
            password_hash=hash_password('Doctor#1234'),
            role='doctor',
            email='doctorb@test.local',
        )
        db.session.add(doctor_user_2)
        db.session.flush()
        doctor_2 = Doctor(
            user_id=doctor_user_2.id,
            name='Dr. Beta',
            specialty='Cardiology',
            max_per_day=10,
        )
        db.session.add(doctor_2)

        db.session.commit()
        return {
            'admin_id': admin.id,
            'patient_id': patient.id,
            'doctor_1_id': doctor_1.id,
            'doctor_2_id': doctor_2.id,
        }
