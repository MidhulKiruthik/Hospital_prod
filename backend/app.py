"""
Cloud-Native Hospital Operations Platform
Flask API — Main Application
"""

from flask import Flask, jsonify, request, g
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import os
import uuid
import time
import hmac
import hashlib
import base64
from urllib import request as urllib_request
from urllib import error as urllib_error
from sqlalchemy import text

from config import Config, validate_runtime_config
from models import db, User, Doctor, Patient, Appointment, ClinicalSummary, \
                   WorkloadMetric, AuditLog, Notification, Department, \
                   DoctorProfileCompat, PatientProfileCompat, \
                   DoctorAvailabilityCompat, AppointmentDiagnosisCompat, \
                   PaymentOrderCompat, AuthSession, SecurityEvent, \
                   ForecastHistory, ClinicalSummaryRevision
from auth import (hash_password, verify_password, generate_token,
                  token_required, role_required, write_audit,
                  validate_password_policy, verify_audit_integrity,
                  create_refresh_session, refresh_access_token,
                  revoke_refresh_session, revoke_all_sessions_for_user,
                  log_security_event)
from scheduler import (compute_workload_score, book_appointment,
                       get_all_workloads, find_best_doctor,
                       estimate_wait_minutes)
from forecaster import (forecast_workload, forecast_patient_demand,
                        get_dashboard_metrics)
from security_utils import encrypt_text, decrypt_text
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    validate_runtime_config(app.config)
    db.init_app(app)
    cors_origins = _effective_cors_origins(app)
    CORS(app, origins=cors_origins, supports_credentials=True)

    with app.app_context():
        if app.config.get('AUTO_RUN_MIGRATIONS', False):
            _run_db_migrations(app)
        elif app.config.get('AUTO_CREATE_SCHEMA', True):
            db.create_all()
        _bootstrap_admin(app)
        if app.config.get('SEED_DEMO_DATA', True):
            _seed_data()

    return app


def _parse_cors_origins(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if not value:
        return []
    if isinstance(value, str):
        if value.strip() == '*':
            return '*'
        return [part.strip() for part in value.split(',') if part.strip()]
    return '*'


def _effective_cors_origins(flask_app):
    env_name = str(flask_app.config.get('APP_ENV', 'development')).lower()
    if env_name in ('production', 'staging'):
        return _parse_cors_origins(flask_app.config.get('CORS_ORIGINS_PRODUCTION', ''))
    return _parse_cors_origins(flask_app.config.get('CORS_ORIGINS', '*'))


def _run_db_migrations(flask_app):
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    alembic_ini = os.path.join(os.path.dirname(__file__), 'alembic.ini')
    if not os.path.exists(alembic_ini):
        raise RuntimeError(f'Alembic config not found: {alembic_ini}')

    alembic_cfg = AlembicConfig(alembic_ini)
    alembic_cfg.set_main_option('sqlalchemy.url', flask_app.config['SQLALCHEMY_DATABASE_URI'])
    alembic_command.upgrade(alembic_cfg, 'head')


def _bootstrap_admin(flask_app):
    username = flask_app.config.get('BOOTSTRAP_ADMIN_USERNAME', '').strip()
    password = flask_app.config.get('BOOTSTRAP_ADMIN_PASSWORD', '').strip()
    email = flask_app.config.get('BOOTSTRAP_ADMIN_EMAIL', 'admin@hospital.local').strip()

    if not username or not password:
        return
    if User.query.filter_by(role='admin').first():
        return
    if User.query.filter_by(username=username).first():
        return

    admin = User(
        username=username,
        password_hash=hash_password(password),
        role='admin',
        email=email,
    )
    db.session.add(admin)
    db.session.commit()


def _seed_data():
    """Seed demo data without overwriting existing users or patient records."""
    demo_users = _ensure_demo_users()
    demo_doctors = _ensure_demo_doctors(demo_users['doctor_users'])
    demo_patients = _ensure_demo_patients()
    _ensure_demo_appointments(demo_doctors, demo_patients)


def _ensure_demo_users():
    users_by_username = {user.username: user for user in User.query.all()}
    created = {}

    admin = users_by_username.get('admin')
    if not admin:
        admin = User(
            username='admin',
            password_hash=hash_password('admin123'),
            role='admin',
            email='admin@hospital.local',
        )
        db.session.add(admin)
        db.session.flush()
    created['admin'] = admin

    receptionist = users_by_username.get('receptionist')
    if not receptionist:
        receptionist = User(
            username='receptionist',
            password_hash=hash_password('recep123'),
            role='receptionist',
            email='reception@hospital.local',
        )
        db.session.add(receptionist)
        db.session.flush()
    created['receptionist'] = receptionist

    doctor_users = []
    for i in range(10):
        username = f'doctor{i + 1}'
        user = users_by_username.get(username)
        if not user:
            user = User(
                username=username,
                password_hash=hash_password('doc123'),
                role='doctor',
                email=f'{username}@hospital.local',
            )
            db.session.add(user)
            db.session.flush()
        doctor_users.append(user)

    db.session.commit()
    created['doctor_users'] = doctor_users
    return created


def _ensure_demo_doctors(doctor_users):
    doctor_specs = [
        ('Dr. Arjun Sharma',    'General Medicine'),
        ('Dr. Priya Nair',      'Cardiology'),
        ('Dr. Vikram Mehta',    'Orthopedics'),
        ('Dr. Kavitha Reddy',   'Pediatrics'),
        ('Dr. Suresh Kumar',    'Neurology'),
        ('Dr. Ananya Singh',    'ENT'),
        ('Dr. Rahul Patel',     'Dermatology'),
        ('Dr. Meena Iyer',      'General Medicine'),
        ('Dr. Karthik Raj',     'Cardiology'),
        ('Dr. Deepa Pillai',    'Pediatrics'),
    ]
    existing_by_name = {doctor.name: doctor for doctor in Doctor.query.all()}
    doctors = []
    changed = False

    for idx, (name, specialty) in enumerate(doctor_specs):
        doctor = existing_by_name.get(name)
        if not doctor:
            doctor = Doctor(
                user_id=doctor_users[idx].id if idx < len(doctor_users) else None,
                name=name,
                specialty=specialty,
                max_per_day=35 + (idx % 3) * 5,
            )
            db.session.add(doctor)
            db.session.flush()
            changed = True
        elif not doctor.user_id and idx < len(doctor_users):
            doctor.user_id = doctor_users[idx].id
            changed = True
        doctors.append(doctor)

    if changed:
        db.session.commit()
    return doctors


def _ensure_demo_patients():
    patient_names = [
        'Ramesh Babu', 'Lakshmi Devi', 'Murugan K', 'Sunita Shah',
        'Arun Kumar', 'Padmavathi R', 'Gopal Menon', 'Nalini T',
        'Senthil Kumar', 'Bhavani S', 'Rajan P', 'Usha Rani',
    ]
    existing_by_name = {patient.name: patient for patient in Patient.query.all()}
    patients = []
    changed = False

    for idx, name in enumerate(patient_names):
        patient = existing_by_name.get(name)
        if not patient:
            patient = Patient(
                name=name,
                phone=f'98{idx:08d}',
                email=f'patient{idx}@mail.com',
            )
            db.session.add(patient)
            db.session.flush()
            changed = True
        patients.append(patient)

    if changed:
        db.session.commit()
    return patients


def _ensure_demo_appointments(doctors, patients):
    if Appointment.query.count() > 0 or not doctors or not patients:
        return

    import random

    statuses = ['booked', 'completed', 'completed', 'cancelled']
    now = datetime.utcnow()

    for i in range(30):
        doc = random.choice(doctors)
        pat = random.choice(patients)
        offset_hours = random.randint(-8, 6)
        sched = now + timedelta(hours=offset_hours, minutes=random.randint(0, 59))
        status = random.choice(statuses)
        appt = Appointment(
            patient_id=pat.id,
            doctor_id=doc.id,
            scheduled_at=sched,
            status=status,
            notes=_sample_note(i),
            priority='normal',
            workload_score_at_booking=round(random.uniform(0.2, 0.8), 3)
        )
        db.session.add(appt)

    db.session.commit()


def _sample_note(i: int) -> str:
    notes = [
        "Patient c/o fever for 3 days. BP: 120/80. HR: 92 bpm. Temp: 101.2F. "
        "Diagnosis: Viral infection. Prescribed paracetamol, rest. Follow-up in 5 days.",
        "Complains of chest pain and palpitations for 2 weeks. ECG ordered. "
        "BP: 145/90. HR: 88. Likely hypertension. Start amlodipine 5mg OD.",
        "Back pain since 2 months. X-ray done. No fracture. Muscle strain. "
        "Ibuprofen 400mg TDS for 5 days. Physiotherapy advised.",
        "Child with cough and cold for 1 week. Temp: 99.8F. SPO2: 98%. "
        "Upper respiratory infection. Amoxicillin syrup. Plenty of fluids.",
        "Headache and dizziness for 3 days. MRI recommended. BP: 130/85. "
        "Migraine likely. Prescribed sumatriptan. Avoid triggers.",
        "Patient presenting with knee swelling and joint pain. "
        "Arthritis suspected. Blood test CBC, CRP. Start prednisolone.",
        "Follow-up visit. Diabetes management. HBA1C: 7.2. "
        "Continue metformin 500mg BD. Lifestyle modification advised.",
    ]
    return notes[i % len(notes)]


app = create_app()
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[app.config.get('DEFAULT_RATE_LIMIT', '300 per hour')],
    storage_uri=app.config.get('RATE_LIMIT_STORAGE_URI', app.config.get('REDIS_URL', 'redis://localhost:6379/0')),
)
PROMETHEUS_REGISTRY = CollectorRegistry()

HTTP_REQUESTS_TOTAL = Counter(
    'hospital_http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status_code'],
    registry=PROMETHEUS_REGISTRY,
)
HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    'hospital_http_request_latency_seconds',
    'HTTP request latency in seconds',
    ['method', 'endpoint'],
    registry=PROMETHEUS_REGISTRY,
)
LOGIN_ATTEMPTS_TOTAL = Counter(
    'hospital_login_attempts_total',
    'Login attempts by outcome',
    ['outcome'],
    registry=PROMETHEUS_REGISTRY,
)
APPOINTMENTS_CREATED_TOTAL = Counter(
    'hospital_appointments_created_total',
    'Appointments created by assignment mode',
    ['assignment'],
    registry=PROMETHEUS_REGISTRY,
)
SUMMARIES_QUEUED_TOTAL = Counter(
    'hospital_summaries_queued_total',
    'Clinical summary tasks queued',
    registry=PROMETHEUS_REGISTRY,
)
ACTIVE_DOCTORS_GAUGE = Gauge(
    'hospital_active_doctors',
    'Number of currently available doctors',
    registry=PROMETHEUS_REGISTRY,
)
BOOKED_APPOINTMENTS_GAUGE = Gauge(
    'hospital_booked_appointments',
    'Number of currently booked appointments',
    registry=PROMETHEUS_REGISTRY,
)
BACKUP_AGE_MINUTES_GAUGE = Gauge(
    'hospital_backup_latest_age_minutes',
    'Age in minutes of the most recent SQLite backup',
    registry=PROMETHEUS_REGISTRY,
)
SCHEDULING_LATENCY_SECONDS = Histogram(
    'hospital_scheduling_latency_seconds',
    'Latency for appointment scheduling requests',
    registry=PROMETHEUS_REGISTRY,
)
REASSIGNMENT_TOTAL = Counter(
    'hospital_reassignment_total',
    'Number of overload-driven reassignments',
    ['result'],
    registry=PROMETHEUS_REGISTRY,
)
PREDICTED_WAIT_MINUTES_GAUGE = Gauge(
    'hospital_predicted_wait_minutes',
    'Predicted wait time in minutes for latest scheduled appointments',
    ['doctor_id'],
    registry=PROMETHEUS_REGISTRY,
)
NLP_DURATION_SECONDS = Histogram(
    'hospital_nlp_duration_seconds',
    'Clinical summary NLP duration in seconds',
    ['mode'],
    registry=PROMETHEUS_REGISTRY,
)
SECURITY_EVENTS_TOTAL = Counter(
    'hospital_security_events_total',
    'Security events emitted by severity and type',
    ['severity', 'event_type'],
    registry=PROMETHEUS_REGISTRY,
)
WORKER_HEARTBEAT_AGE_SECONDS = Gauge(
    'hospital_worker_heartbeat_age_seconds',
    'Age of latest workload metric snapshot in seconds',
    registry=PROMETHEUS_REGISTRY,
)
DB_UP_GAUGE = Gauge(
    'hospital_db_up',
    'Database connectivity status (1=up,0=down)',
    registry=PROMETHEUS_REGISTRY,
)
REDIS_UP_GAUGE = Gauge(
    'hospital_redis_up',
    'Redis connectivity status (1=up,0=down)',
    registry=PROMETHEUS_REGISTRY,
)
SUMMARY_PENDING_GAUGE = Gauge(
    'hospital_summaries_pending',
    'Pending clinical summaries count',
    registry=PROMETHEUS_REGISTRY,
)
OVERLOAD_RISK_GAUGE = Gauge(
    'hospital_overload_risk_doctors',
    'Number of currently overloaded doctors',
    registry=PROMETHEUS_REGISTRY,
)
FORECAST_QUALITY_MAE = Gauge(
    'hospital_forecast_quality_mae',
    'Latest forecast mean absolute error estimate',
    ['scope'],
    registry=PROMETHEUS_REGISTRY,
)
FORECAST_QUALITY_RMSE = Gauge(
    'hospital_forecast_quality_rmse',
    'Latest forecast root mean square error estimate',
    ['scope'],
    registry=PROMETHEUS_REGISTRY,
)


@app.before_request
def _request_start_timer():
    g._request_started_at = time.perf_counter()
    if app.config.get('ENFORCE_HTTPS', False):
        proto = request.headers.get('X-Forwarded-Proto', request.scheme)
        if proto != 'https':
            return jsonify({'error': 'HTTPS is required'}), 403


@app.after_request
def _instrument_and_secure_response(response):
    endpoint = request.endpoint or 'unknown'
    method = request.method
    status_code = str(response.status_code)

    started = getattr(g, '_request_started_at', None)
    if started is not None:
        elapsed = max(time.perf_counter() - started, 0.0)
        HTTP_REQUEST_LATENCY_SECONDS.labels(method=method, endpoint=endpoint).observe(elapsed)
    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status_code=status_code).inc()

    # Baseline security headers.
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'

    if app.config.get('ENFORCE_HTTPS', False):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    return response


def _current_doctor_id():
    user_id = request.current_user.get('user_id')
    doctor = Doctor.query.filter_by(user_id=user_id).first()
    return doctor.id if doctor else None


def _current_patient_id():
    user_id = request.current_user.get('user_id')
    patient = Patient.query.filter_by(user_id=user_id).first()
    return patient.id if patient else None


def _calc_age(dob_str: str):
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, '%Y-%m-%d').date()
    except ValueError:
        return None
    today = datetime.utcnow().date()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _doctor_profile_row(doctor_id: int):
    row = DoctorProfileCompat.query.filter_by(doctor_id=doctor_id).first()
    if not row:
        row = DoctorProfileCompat(doctor_id=doctor_id)
        db.session.add(row)
        db.session.flush()
    return row


def _patient_profile_row(patient_id: int):
    row = PatientProfileCompat.query.filter_by(patient_id=patient_id).first()
    if not row:
        row = PatientProfileCompat(patient_id=patient_id)
        db.session.add(row)
        db.session.flush()
    return row


def _ensure_departments():
    specialties = [x[0] for x in db.session.query(Doctor.specialty).distinct().all() if x[0]]
    for name in specialties:
        existing = Department.query.filter_by(name=name).first()
        if existing:
            continue
        dept = Department(
            name=name,
            phone_number='0000000000',
            email=f"{name.lower().replace(' ', '')}@hospital.local",
            description=f"{name} department",
        )
        db.session.add(dept)
    db.session.commit()


def _department_for_specialty(specialty: str):
    _ensure_departments()
    spec_name = specialty or 'General'
    dept = Department.query.filter(db.func.lower(Department.name) == spec_name.lower()).first()
    if dept:
        return dept
    dept = Department(
        name=spec_name,
        phone_number='0000000000',
        email=f"{spec_name.lower().replace(' ', '')}@hospital.local",
        description=f"{spec_name} department",
    )
    db.session.add(dept)
    db.session.commit()
    return dept


def _doctor_payload(doc: Doctor):
    user = User.query.get(doc.user_id) if doc.user_id else None
    dept = _department_for_specialty(doc.specialty)
    extra = _doctor_profile_row(doc.id)
    return {
        'id': doc.id,
        'name': doc.name,
        'specialization': doc.specialty,
        'department_id': dept.id,
        'department_name': dept.name,
        'email': (user.email if user else ''),
        'phone': extra.phone,
        'experience': extra.experience,
        'consultation_fee': extra.dr_consultation_fee,
        'dr_consultation_fee': extra.dr_consultation_fee,
        'is_blacklisted': extra.is_blacklisted,
    }


def _patient_payload(p: Patient):
    extra = _patient_profile_row(p.id)
    return {
        'id': p.id,
        'name': p.name,
        'email': decrypt_text(p.email),
        'phone': decrypt_text(p.phone),
        'dob': p.dob,
        'age': _calc_age(p.dob),
        'gender': extra.gender,
        'address': extra.address,
        'city': extra.city,
        'state': extra.state,
        'zipcode': extra.zipcode,
        'blood_type': extra.blood_type,
        'allergies': extra.allergies,
        'medical_summary': decrypt_text(extra.medical_summary),
    }


def _appointment_compact(appt: Appointment):
    return {
        'id': appt.id,
        'patient_id': appt.patient_id,
        'patient_name': appt.patient.name if appt.patient else '',
        'doctor_id': appt.doctor_id,
        'doctor_name': appt.doctor.name if appt.doctor else '',
        'date': appt.scheduled_at.strftime('%Y-%m-%d'),
        'time': appt.scheduled_at.strftime('%H:%M'),
        'status': appt.status,
        'reason': appt.notes or '',
    }


def _latest_backup_age_minutes():
    if not app.config.get('BACKUP_HEALTH_CHECK_ENABLED', True):
        return None

    database_url = str(app.config.get('SQLALCHEMY_DATABASE_URI', ''))
    backup_ext = '.db' if database_url.startswith('sqlite') else '.sql'

    backup_dir = app.config.get('BACKUP_DIR', '/data/backups')
    if not os.path.isdir(backup_dir):
        return None

    backups = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.endswith(backup_ext)
    ]
    if not backups:
        return None
    latest_backup_at = max(os.path.getmtime(path) for path in backups)
    return round((time.time() - latest_backup_at) / 60, 2)


def _summary_queue_enabled():
    broker_url = app.config.get('CELERY_BROKER_URL') or app.config.get('REDIS_URL', '')
    return isinstance(broker_url, str) and broker_url.startswith('redis://')


def _generate_summary_sync(appt: Appointment):
    from nlp import generate_clinical_summary

    result = generate_clinical_summary(appt.notes, {
        'patient_name': appt.patient.name if appt.patient else '',
        'doctor_name': appt.doctor.name if appt.doctor else '',
        'specialty': appt.doctor.specialty if appt.doctor else 'General',
        'date': appt.scheduled_at.strftime('%Y-%m-%d'),
    })
    summary = ClinicalSummary.query.filter_by(appointment_id=appt.id).first()
    if not summary:
        summary = ClinicalSummary(appointment_id=appt.id)
        db.session.add(summary)
    summary.summary_text = encrypt_text(result['summary_text'])
    summary.chief_complaint = encrypt_text(result['chief_complaint'])
    summary.findings = encrypt_text(result['findings'])
    summary.assessment = encrypt_text(result['assessment'])
    summary.plan = encrypt_text(result['plan'])
    summary.status = result['status']
    summary.generation_method = result.get('method', 'rule-based-nlp')
    summary.generation_model = result.get('model_name', '')
    summary.processing_time_s = result['processing_time_s']
    summary.source_notes_hash = hashlib.sha256((appt.notes or '').encode('utf-8')).hexdigest()
    summary.is_reviewed = False
    summary.reviewed_by_user_id = None
    summary.reviewed_at = None
    summary.review_notes = ''
    summary.generated_at = datetime.utcnow()
    db.session.commit()
    NLP_DURATION_SECONDS.labels(mode='sync').observe(max(float(result['processing_time_s']), 0.0))
    return summary


def _record_forecast_history(scope: str, scope_id, payload: dict) -> None:
    forecast_items = payload.get('forecast', [])
    if not forecast_items:
        return

    errors = []
    for point in forecast_items:
        if 'actual_score' in point and point.get('actual_score') is not None:
            predicted = float(point.get('predicted_score', 0.0))
            actual = float(point.get('actual_score', 0.0))
            errors.append(actual - predicted)

    mae = None
    rmse = None
    if errors:
        abs_errors = [abs(x) for x in errors]
        mae = sum(abs_errors) / len(abs_errors)
        rmse = (sum((x * x) for x in errors) / len(errors)) ** 0.5

    row = ForecastHistory(
        scope=scope,
        scope_id=scope_id,
        selected_model=str(payload.get('selected_model', 'baseline')),
        effective_model=str(payload.get('effective_model', '')),
        horizon_minutes=int(payload.get('horizon_minutes', 120)),
        peak_predicted=float(payload.get('peak_predicted', 0.0)),
        avg_predicted=float(payload.get('avg_predicted', 0.0)),
        overload_expected=bool(payload.get('overload_expected', False)),
        mae=mae,
        rmse=rmse,
        payload_json=json.dumps(payload, sort_keys=True),
    )
    db.session.add(row)
    db.session.commit()

    scope_label = 'hospital' if scope == 'hospital' else 'doctor'
    if mae is not None:
        FORECAST_QUALITY_MAE.labels(scope=scope_label).set(mae)
    if rmse is not None:
        FORECAST_QUALITY_RMSE.labels(scope=scope_label).set(rmse)


def _log_and_count_security_event(event_type: str, severity: str, details: dict = None, user_id: int = None):
    try:
        log_security_event(event_type, severity=severity, details=details or {}, user_id=user_id)
        SECURITY_EVENTS_TOTAL.labels(severity=severity, event_type=event_type).inc()
    except Exception:
        pass


# ── Health Check ──────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'service': 'Hospital Operations Platform v1.0'
    })


@app.route('/api/health/deep', methods=['GET'])
def deep_health():
    status = {'status': 'ok', 'timestamp': datetime.utcnow().isoformat(), 'checks': {}}
    http_code = 200

    try:
        db.session.execute(text('SELECT 1'))
        status['checks']['database'] = {'status': 'ok'}
    except Exception as exc:
        status['checks']['database'] = {'status': 'error', 'detail': str(exc)}
        status['status'] = 'degraded'
        http_code = 503

    try:
        import redis
        redis.from_url(app.config.get('REDIS_URL', 'redis://localhost:6379/0')).ping()
        status['checks']['redis'] = {'status': 'ok'}
    except Exception as exc:
        status['checks']['redis'] = {'status': 'error', 'detail': str(exc)}
        status['status'] = 'degraded'
        http_code = 503

    latest_metric = WorkloadMetric.query.order_by(WorkloadMetric.timestamp.desc()).first()
    if latest_metric and latest_metric.timestamp:
        age_s = max((datetime.utcnow() - latest_metric.timestamp).total_seconds(), 0.0)
        status['checks']['worker'] = {
            'status': 'ok' if age_s <= 120 else 'stale',
            'heartbeat_age_seconds': round(age_s, 2),
        }
        if age_s > 120:
            status['status'] = 'degraded'
            http_code = 503
    else:
        status['checks']['worker'] = {'status': 'missing'}
        status['status'] = 'degraded'
        http_code = 503

    backup_check_enabled = app.config.get('BACKUP_HEALTH_CHECK_ENABLED', True)
    age_minutes = _latest_backup_age_minutes()
    if not backup_check_enabled:
        status['checks']['backup'] = {'status': 'disabled'}
    elif age_minutes is not None:
        status['checks']['backup'] = {
            'status': 'ok' if age_minutes <= app.config.get('BACKUP_HEALTH_MAX_AGE_MINUTES', 30) else 'stale',
            'age_minutes': age_minutes,
        }
        if age_minutes > app.config.get('BACKUP_HEALTH_MAX_AGE_MINUTES', 30):
            status['status'] = 'degraded'
            http_code = 503
    else:
        status['checks']['backup'] = {'status': 'missing'}
        status['status'] = 'degraded'
        http_code = 503

    return jsonify(status), http_code


# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit(lambda: app.config.get('AUTH_RATE_LIMIT', '5 per minute'))
def login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    user = User.query.filter_by(username=username).first()
    lockout_until = user.lockout_until if user else None
    if lockout_until and lockout_until > datetime.utcnow():
        LOGIN_ATTEMPTS_TOTAL.labels(outcome='blocked').inc()
        _log_and_count_security_event(
            'auth_login_blocked',
            severity='high',
            details={'username': username, 'lockout_until': lockout_until.isoformat()},
            user_id=user.id,
        )
        write_audit(
            'login_blocked',
            'user',
            user.id,
            {'username': username, 'lockout_until': lockout_until.isoformat()},
            user.id,
        )
        return jsonify({'error': 'Account temporarily locked. Try again later.'}), 423

    if not user or not verify_password(password, user.password_hash):
        if user:
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            max_attempts = app.config.get('LOGIN_MAX_FAILED_ATTEMPTS', 5)
            lockout_minutes = app.config.get('LOGIN_LOCKOUT_MINUTES', 15)
            if user.failed_login_attempts >= max_attempts:
                user.lockout_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
                user.failed_login_attempts = 0
            db.session.commit()

        LOGIN_ATTEMPTS_TOTAL.labels(outcome='failed').inc()
        _log_and_count_security_event(
            'auth_login_failed',
            severity='medium',
            details={'username': username},
            user_id=user.id if user else None,
        )
        write_audit('login_failed', 'user', user.id if user else None,
                    {'username': username}, user.id if user else None)
        return jsonify({'error': 'Invalid credentials'}), 401

    token = generate_token(user)
    refresh_token = create_refresh_session(user)
    user.failed_login_attempts = 0
    user.lockout_until = None
    user.last_login_at = datetime.utcnow()
    db.session.commit()
    LOGIN_ATTEMPTS_TOTAL.labels(outcome='success').inc()
    _log_and_count_security_event(
        'auth_login_success',
        severity='low',
        details={'username': username},
        user_id=user.id,
    )
    write_audit('login', 'user', user.id, {'username': username}, user.id)
    return jsonify({
        'token': token,
        'access_token': token,
        'refresh_token': refresh_token,
        'user': user.to_dict(),
    })


@app.route('/api/auth/register', methods=['POST'])
@limiter.limit('10 per hour')
def register():
    data = request.get_json() or {}
    required = ['username', 'password', 'email']
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'{f} is required'}), 400

    data['role'] = data.get('role', 'patient')

    if data['role'] not in ['admin', 'doctor', 'receptionist', 'patient']:
        return jsonify({'error': 'Invalid role'}), 400
    if (
        data['role'] != 'patient'
        and not app.config.get('ALLOW_PRIVILEGED_SELF_REGISTRATION', False)
    ):
        _log_and_count_security_event(
            'privileged_self_registration_attempt',
            severity='high',
            details={'username': data.get('username', ''), 'requested_role': data.get('role')},
            user_id=None,
        )
        return jsonify({
            'error': 'Privileged self-registration is disabled. Ask an admin to create this account.'
        }), 403
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 409

    password_error = validate_password_policy(data.get('password', ''))
    if password_error:
        return jsonify({'error': password_error}), 400

    user = User(
        username=data['username'],
        password_hash=hash_password(data['password']),
        role=data['role'],
        email=data['email']
    )
    db.session.add(user)
    db.session.flush()

    if data['role'] == 'patient':
        patient = Patient(
            user_id=user.id,
            name=data.get('name') or data['username'],
            email=encrypt_text(data.get('email', '')),
            phone=encrypt_text(data.get('phone', '')),
            dob=data.get('dob', '')
        )
        db.session.add(patient)
    elif data['role'] == 'doctor':
        doctor = Doctor(
            user_id=user.id,
            name=data.get('name') or f"Dr. {data['username']}",
            specialty=data.get('specialty', 'General Medicine'),
            max_per_day=data.get('max_per_day', 40)
        )
        db.session.add(doctor)

    db.session.commit()
    token = generate_token(user)
    refresh_token = create_refresh_session(user)
    return jsonify({
        'token': token,
        'access_token': token,
        'refresh_token': refresh_token,
        'user': user.to_dict(),
    }), 201


@app.route('/api/auth/refresh', methods=['POST'])
@limiter.limit('30 per hour')
def refresh_auth_token():
    data = request.get_json() or {}
    refresh_token = (data.get('refresh_token') or '').strip()
    if not refresh_token:
        return jsonify({'error': 'refresh_token is required'}), 400

    access_token, err = refresh_access_token(refresh_token)
    if err:
        _log_and_count_security_event(
            'auth_refresh_failed',
            severity='medium',
            details={'reason': err},
            user_id=None,
        )
        return jsonify({'error': err}), 401

    return jsonify({'access_token': access_token})


@app.route('/api/auth/logout', methods=['POST'])
@token_required
def logout():
    data = request.get_json() or {}
    refresh_token = (data.get('refresh_token') or '').strip()
    if refresh_token:
        revoke_refresh_session(refresh_token)
    _log_and_count_security_event(
        'auth_logout',
        severity='low',
        details={'user_id': request.current_user.get('user_id')},
        user_id=request.current_user.get('user_id'),
    )
    return jsonify({'status': 'ok'})


@app.route('/api/auth/logout-all', methods=['POST'])
@token_required
def logout_all_sessions():
    user = User.query.get_or_404(request.current_user.get('user_id'))
    revoke_all_sessions_for_user(user)
    _log_and_count_security_event(
        'auth_logout_all',
        severity='medium',
        details={'user_id': user.id},
        user_id=user.id,
    )
    return jsonify({'status': 'ok'})


@app.route('/api/auth/me', methods=['GET'])
@token_required
def me():
    user = User.query.get(request.current_user['user_id'])
    u = user.to_dict() if user else None
    if not u:
        return jsonify({'user': None}), 404
    return jsonify({'user': u, **u})


# ── Doctor Routes ─────────────────────────────────────────────────────────────

@app.route('/api/doctors', methods=['GET'])
@token_required
def list_doctors():
    specialty = request.args.get('specialty')
    q = Doctor.query
    if specialty:
        q = q.filter_by(specialty=specialty)
    doctors = q.all()
    return jsonify([d.to_dict() for d in doctors])


@app.route('/api/doctors/<int:doc_id>', methods=['GET'])
@token_required
def get_doctor(doc_id):
    doc = Doctor.query.get_or_404(doc_id)
    return jsonify(doc.to_dict())


@app.route('/api/doctors', methods=['POST'])
@role_required('admin', 'receptionist')
def create_doctor():
    data = request.get_json() or {}
    doc = Doctor(
        name=data.get('name', ''),
        specialty=data.get('specialty', 'General Medicine'),
        max_per_day=data.get('max_per_day', 40)
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify(doc.to_dict()), 201


@app.route('/api/doctors/<int:doc_id>/workload', methods=['GET'])
@token_required
def doctor_workload(doc_id):
    target = request.args.get('date')
    target_date = datetime.strptime(target, '%Y-%m-%d').date() if target else None
    ws = compute_workload_score(doc_id, target_date)
    doc = Doctor.query.get(doc_id)
    return jsonify({
        'doctor_id': doc_id,
        'doctor_name': doc.name if doc else '',
        **ws
    })


@app.route('/api/doctors/workloads', methods=['GET'])
@token_required
def all_workloads():
    workloads = get_all_workloads()
    return jsonify(workloads)


@app.route('/api/specialties', methods=['GET'])
def list_specialties():
    results = db.session.query(Doctor.specialty).distinct().all()
    return jsonify([r[0] for r in results])


# ── Patient Routes ─────────────────────────────────────────────────────────────

@app.route('/api/patients', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor')
def list_patients():
    search = request.args.get('search', '')
    q = Patient.query
    if search:
        q = q.filter(Patient.name.ilike(f'%{search}%'))
    patients = q.limit(50).all()
    return jsonify([_patient_payload(p) for p in patients])


@app.route('/api/patients', methods=['POST'])
@role_required('admin', 'receptionist')
def create_patient():
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'Patient name required'}), 400
    patient = Patient(
        name=data['name'],
        dob=data.get('dob', ''),
        phone=encrypt_text(data.get('phone', '')),
        email=encrypt_text(data.get('email', ''))
    )
    db.session.add(patient)
    db.session.commit()
    return jsonify(_patient_payload(patient)), 201


@app.route('/api/patients/<int:pid>', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor', 'patient')
def get_patient(pid):
    p = Patient.query.get_or_404(pid)
    if request.current_user.get('role') == 'patient' and p.user_id != request.current_user.get('user_id'):
        return jsonify({'error': 'Forbidden'}), 403
    appts = [a.to_dict() for a in p.appointments.order_by(
        Appointment.scheduled_at.desc()).limit(10).all()]
    result = _patient_payload(p)
    result['appointments'] = appts
    return jsonify(result)


# ── Appointment Routes ────────────────────────────────────────────────────────

@app.route('/api/appointments', methods=['GET'])
@token_required
def list_appointments():
    status    = request.args.get('status')
    doctor_id = request.args.get('doctor_id', type=int)
    patient_id = request.args.get('patient_id', type=int)
    date_str  = request.args.get('date')
    page      = request.args.get('page', 1, type=int)
    per_page  = request.args.get('per_page', 20, type=int)

    q = Appointment.query
    role = request.current_user.get('role')
    user_id = request.current_user.get('user_id')

    if role == 'doctor':
        q = q.join(Doctor, Appointment.doctor_id == Doctor.id).filter(Doctor.user_id == user_id)
    elif role == 'patient':
        patient_id_for_user = _current_patient_id()
        if not patient_id_for_user:
            return jsonify({'total': 0, 'page': page, 'per_page': per_page, 'appointments': []})
        q = q.filter_by(patient_id=patient_id_for_user)
    if status:
        q = q.filter_by(status=status)
    if doctor_id:
        q = q.filter_by(doctor_id=doctor_id)
    if patient_id:
        q = q.filter_by(patient_id=patient_id)
    if date_str:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d')
            q = q.filter(
                Appointment.scheduled_at >= d,
                Appointment.scheduled_at < d + timedelta(days=1)
            )
        except ValueError:
            pass

    total = q.count()
    appts = q.order_by(Appointment.scheduled_at.asc())\
             .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        'total': total,
        'page': page,
        'per_page': per_page,
        'appointments': [a.to_dict() for a in appts]
    })


@app.route('/api/appointments', methods=['POST'])
@role_required('admin', 'receptionist', 'patient')
def create_appointment():
    data = request.get_json() or {}
    required = ['specialty', 'scheduled_at']
    if request.current_user.get('role') != 'patient':
        required.insert(0, 'patient_id')
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'{f} is required'}), 400

    try:
        scheduling_started_at = time.perf_counter()
        scheduled_at = datetime.fromisoformat(data['scheduled_at'])
    except ValueError:
        return jsonify({'error': 'Invalid scheduled_at format (ISO 8601)'}), 400

    if scheduled_at <= datetime.utcnow():
        return jsonify({'error': 'Appointment must be scheduled in the future'}), 400

    role = request.current_user.get('role')
    patient_id = data.get('patient_id')
    if role == 'patient':
        patient_id = _current_patient_id()
        if not patient_id:
            return jsonify({'error': 'Patient profile not found for current user'}), 404

    patient = Patient.query.get(patient_id)
    if not patient:
        return jsonify({'error': 'Patient not found'}), 404

    result = book_appointment(
        patient_id=patient_id,
        specialty=data['specialty'],
        scheduled_at=scheduled_at,
        notes=data.get('notes', ''),
        priority=data.get('priority', 'normal'),
        preferred_doctor_id=data.get('doctor_id')
    )

    if 'error' in result:
        if 'capacity' in str(result['error']).lower() or 'booked' in str(result['error']).lower():
            REASSIGNMENT_TOTAL.labels(result='failed').inc()
        return jsonify({'error': result['error']}), 422

    appt = result['appointment']
    scheduling_latency = max(time.perf_counter() - scheduling_started_at, 0.0)
    SCHEDULING_LATENCY_SECONDS.observe(scheduling_latency)
    assignment = 'reassigned' if result.get('reassigned', False) else 'direct'
    APPOINTMENTS_CREATED_TOTAL.labels(assignment=assignment).inc()
    if result.get('reassigned', False):
        REASSIGNMENT_TOTAL.labels(result='success').inc()
        reassignment_detail = {
            'reassigned_to': appt.doctor_id,
            'original_doctor_id': result.get('original_doctor_id'),
            'overload_triggered': result.get('overload_triggered', False),
            'predicted_wait_minutes': result.get('predicted_wait_minutes', 0),
        }
        _log_and_count_security_event(
            'appointment_reassigned',
            severity='low',
            details=reassignment_detail,
            user_id=request.current_user.get('user_id'),
        )

    PREDICTED_WAIT_MINUTES_GAUGE.labels(doctor_id=str(appt.doctor_id)).set(
        float(result.get('predicted_wait_minutes', 0.0))
    )
    write_audit('book_appointment', 'appointment', appt.id,
                {
                    'patient_id': patient_id,
                    'reassigned': result.get('reassigned'),
                    'scheduling_latency_s': round(scheduling_latency, 4),
                    'predicted_wait_minutes': result.get('predicted_wait_minutes', 0),
                },
                request.current_user.get('user_id'))

    return jsonify({
        'appointment': appt.to_dict(),
        'reassigned': result.get('reassigned', False),
        'overload_triggered': result.get('overload_triggered', False),
        'overload_explanation': result.get('overload_explanation', ''),
        'predicted_wait_minutes': result.get('predicted_wait_minutes', 0),
        'message': result.get('message', 'Appointment booked successfully.')
    }), 201


@app.route('/api/appointments/<int:appt_id>', methods=['GET'])
@token_required
def get_appointment(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    result = appt.to_dict()
    if appt.summary:
        result['summary'] = appt.summary.to_dict()
    return jsonify(result)


@app.route('/api/appointments/<int:appt_id>/complete', methods=['PUT'])
@role_required('admin', 'receptionist', 'doctor')
def complete_appointment(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    if request.current_user.get('role') == 'doctor':
        current_doctor_id = _current_doctor_id()
        if not current_doctor_id or appt.doctor_id != current_doctor_id:
            return jsonify({'error': 'Forbidden'}), 403

    if appt.status != 'booked':
        return jsonify({'error': f'Cannot complete appointment with status: {appt.status}'}), 400

    data = request.get_json() or {}
    notes = data.get('notes', appt.notes)

    appt.status       = 'completed'
    appt.notes        = notes
    appt.completed_at = datetime.utcnow()
    db.session.commit()

    # Trigger NLP summary generation outside the main booking write path.
    summary_message = 'Appointment completed. Clinical summary being generated asynchronously.'
    nlp_mode = 'queued'
    try:
        if _summary_queue_enabled():
            from tasks import generate_clinical_summary_task
            generate_clinical_summary_task.delay(appt_id, notes)
            SUMMARIES_QUEUED_TOTAL.inc()
        else:
            _generate_summary_sync(appt)
            nlp_mode = 'sync'
            summary_message = 'Appointment completed. Clinical summary generated synchronously.'
    except Exception:
        _generate_summary_sync(appt)
        nlp_mode = 'sync-fallback'
        summary_message = 'Appointment completed. Clinical summary generated synchronously.'

    if nlp_mode == 'queued':
        NLP_DURATION_SECONDS.labels(mode='queued').observe(0.0)

    write_audit('complete_appointment', 'appointment', appt_id,
                {'notes_length': len(notes)},
                request.current_user.get('user_id'))

    return jsonify({
        'appointment': appt.to_dict(),
        'summary_mode': nlp_mode,
        'message': summary_message
    })


@app.route('/api/appointments/<int:appt_id>/cancel', methods=['PUT'])
@role_required('admin', 'receptionist', 'patient')
def cancel_appointment(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    if request.current_user.get('role') == 'patient':
        current_patient_id = _current_patient_id()
        if not current_patient_id or appt.patient_id != current_patient_id:
            return jsonify({'error': 'Forbidden'}), 403

    if appt.status not in ('booked',):
        return jsonify({'error': 'Only booked appointments can be cancelled'}), 400

    data = request.get_json() or {}
    appt.status = 'cancelled'
    appt.notes  = data.get('reason', appt.notes)
    db.session.commit()

    write_audit('cancel_appointment', 'appointment', appt_id,
                {'reason': data.get('reason', '')},
                request.current_user.get('user_id'))
    return jsonify({'appointment': appt.to_dict(), 'message': 'Appointment cancelled.'})


# ── Clinical Summary Routes ───────────────────────────────────────────────────

@app.route('/api/summaries/<int:appt_id>', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor', 'patient')
def get_summary(appt_id):
    if request.current_user.get('role') == 'patient':
        appt = Appointment.query.get_or_404(appt_id)
        current_patient_id = _current_patient_id()
        if not current_patient_id or appt.patient_id != current_patient_id:
            return jsonify({'error': 'Forbidden'}), 403
    summary = ClinicalSummary.query.filter_by(appointment_id=appt_id).first()
    if not summary:
        return jsonify({'status': 'pending', 'message': 'Summary not yet generated'}), 202
    payload = summary.to_dict()
    payload['summary_text'] = decrypt_text(payload.get('summary_text', ''))
    payload['chief_complaint'] = decrypt_text(payload.get('chief_complaint', ''))
    payload['findings'] = decrypt_text(payload.get('findings', ''))
    payload['assessment'] = decrypt_text(payload.get('assessment', ''))
    payload['plan'] = decrypt_text(payload.get('plan', ''))
    return jsonify(payload)


@app.route('/api/summaries/<int:appt_id>/regenerate', methods=['POST'])
@role_required('admin', 'receptionist', 'doctor')
def regenerate_summary(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    if request.current_user.get('role') == 'doctor':
        current_doctor_id = _current_doctor_id()
        if not current_doctor_id or appt.doctor_id != current_doctor_id:
            return jsonify({'error': 'Forbidden'}), 403

    try:
        if _summary_queue_enabled():
            from tasks import generate_clinical_summary_task
            generate_clinical_summary_task.delay(appt_id, appt.notes)
            NLP_DURATION_SECONDS.labels(mode='queued').observe(0.0)
            return jsonify({'message': 'Summary regeneration queued'})
    except Exception:
        pass

    summary = _generate_summary_sync(appt)
    payload = summary.to_dict()
    payload['summary_text'] = decrypt_text(payload.get('summary_text', ''))
    payload['chief_complaint'] = decrypt_text(payload.get('chief_complaint', ''))
    payload['findings'] = decrypt_text(payload.get('findings', ''))
    payload['assessment'] = decrypt_text(payload.get('assessment', ''))
    payload['plan'] = decrypt_text(payload.get('plan', ''))
    return jsonify(payload)


# Forecasting Routes ────────────────────────────────────────────────────────

@app.route('/api/forecast/workload', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor')
def workload_forecast():
    doctor_id       = request.args.get('doctor_id', type=int)
    horizon_minutes = request.args.get('horizon', 120, type=int)
    force_refresh = str(request.args.get('refresh', 'false')).lower() in ('1', 'true', 'yes')
    # Try Redis cache first
    if not force_refresh:
        try:
            import redis
            r = redis.from_url(app.config.get('REDIS_URL', 'redis://localhost:6379/0'))
            cache_key = f'forecast:doctor:{doctor_id}' if doctor_id else 'forecast:hospital'
            cached = r.get(cache_key)
            if cached:
                return jsonify(json.loads(cached))
        except Exception:
            pass
    result = forecast_workload(doctor_id, horizon_minutes)
    scope = 'doctor' if doctor_id else 'hospital'
    _record_forecast_history(scope, doctor_id, result)
    return jsonify(result)


@app.route('/api/forecast/demand', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor')
def demand_forecast():
    horizon_hours = request.args.get('hours', 4, type=int)
    try:
        import redis
        r = redis.from_url(app.config.get('REDIS_URL', 'redis://localhost:6379/0'))
        cached = r.get('forecast:demand')
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass
    result = forecast_patient_demand(horizon_hours)
    return jsonify(result)


@app.route('/api/forecast/history', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor')
def forecast_history():
    scope = (request.args.get('scope') or '').strip().lower()
    scope_id = request.args.get('scope_id', type=int)
    limit = min(request.args.get('limit', 100, type=int), 500)

    q = ForecastHistory.query
    if scope in ('hospital', 'doctor'):
        q = q.filter_by(scope=scope)
    if scope_id is not None:
        q = q.filter_by(scope_id=scope_id)

    rows = q.order_by(ForecastHistory.generated_at.desc()).limit(limit).all()
    return jsonify([row.to_dict() for row in rows])


@app.route('/api/forecast/best-doctor', methods=['GET'])
@role_required('admin', 'receptionist')
def best_doctor_forecast():
    specialty    = request.args.get('specialty', 'General Medicine')
    scheduled_at = request.args.get('at')
    priority     = request.args.get('priority', 'normal')
    try:
        dt = datetime.fromisoformat(scheduled_at) if scheduled_at else datetime.utcnow()
    except ValueError:
        dt = datetime.utcnow()
    result = find_best_doctor(specialty, dt, priority)
    if result.get('doctor_id'):
        result['predicted_wait_minutes'] = estimate_wait_minutes(result['doctor_id'], dt)
    return jsonify(result)


# ── Dashboard Metrics ─────────────────────────────────────────────────────────

@app.route('/api/dashboard', methods=['GET'])
@token_required
def dashboard():
    metrics = get_dashboard_metrics()
    metrics['security_events_last_24h'] = SecurityEvent.query.filter(
        SecurityEvent.timestamp >= (datetime.utcnow() - timedelta(hours=24))
    ).count()
    return jsonify(metrics)


# ── Audit Logs ────────────────────────────────────────────────────────────────

@app.route('/api/audit', methods=['GET'])
@token_required
def audit_logs():
    if request.current_user.get('role') != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    page    = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    logs    = AuditLog.query.order_by(AuditLog.timestamp.desc())\
                            .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify([l.to_dict() for l in logs])


@app.route('/api/audit/integrity', methods=['GET'])
@role_required('admin')
def audit_integrity_status():
    return jsonify(verify_audit_integrity())


@app.route('/api/security/events', methods=['GET'])
@role_required('admin')
def security_events():
    limit = min(request.args.get('limit', 100, type=int), 500)
    severity = (request.args.get('severity') or '').strip().lower()
    event_type = (request.args.get('event_type') or '').strip()

    q = SecurityEvent.query
    if severity:
        q = q.filter_by(severity=severity)
    if event_type:
        q = q.filter_by(event_type=event_type)

    rows = q.order_by(SecurityEvent.timestamp.desc()).limit(limit).all()
    return jsonify([row.to_dict() for row in rows])


@app.route('/api/summaries/<int:appt_id>/review', methods=['PUT'])
@role_required('admin', 'doctor')
def review_summary(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    summary = ClinicalSummary.query.filter_by(appointment_id=appt_id).first()
    if not summary:
        return jsonify({'error': 'Summary not found'}), 404

    if request.current_user.get('role') == 'doctor':
        current_doctor_id = _current_doctor_id()
        if not current_doctor_id or appt.doctor_id != current_doctor_id:
            _log_and_count_security_event(
                'summary_review_forbidden',
                severity='high',
                details={'appointment_id': appt_id},
                user_id=request.current_user.get('user_id'),
            )
            return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json() or {}
    edited_text = (data.get('summary_text') or '').strip()
    review_note = (data.get('review_note') or '').strip()
    if not edited_text:
        return jsonify({'error': 'summary_text is required'}), 400

    previous_text = summary.summary_text or ''
    revision = ClinicalSummaryRevision(
        summary_id=summary.id,
        edited_by_user_id=request.current_user.get('user_id'),
        previous_summary_text=previous_text,
        new_summary_text=encrypt_text(edited_text),
        edit_note=review_note,
    )
    db.session.add(revision)

    summary.summary_text = encrypt_text(edited_text)
    summary.is_reviewed = True
    summary.reviewed_by_user_id = request.current_user.get('user_id')
    summary.reviewed_at = datetime.utcnow()
    summary.review_notes = review_note
    db.session.commit()

    write_audit(
        'summary_reviewed',
        'clinical_summary',
        summary.id,
        {'appointment_id': appt_id, 'review_note': review_note},
        request.current_user.get('user_id'),
    )

    payload = summary.to_dict()
    payload['summary_text'] = decrypt_text(payload.get('summary_text', ''))
    payload['chief_complaint'] = decrypt_text(payload.get('chief_complaint', ''))
    payload['findings'] = decrypt_text(payload.get('findings', ''))
    payload['assessment'] = decrypt_text(payload.get('assessment', ''))
    payload['plan'] = decrypt_text(payload.get('plan', ''))
    return jsonify(payload)


@app.route('/api/summaries/<int:appt_id>/revisions', methods=['GET'])
@role_required('admin', 'doctor')
def summary_revisions(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    summary = ClinicalSummary.query.filter_by(appointment_id=appt_id).first()
    if not summary:
        return jsonify([])

    if request.current_user.get('role') == 'doctor':
        current_doctor_id = _current_doctor_id()
        if not current_doctor_id or appt.doctor_id != current_doctor_id:
            return jsonify({'error': 'Forbidden'}), 403

    rows = ClinicalSummaryRevision.query.filter_by(summary_id=summary.id).order_by(
        ClinicalSummaryRevision.created_at.desc()
    ).all()
    payload = []
    for row in rows:
        item = row.to_dict()
        item['previous_summary_text'] = decrypt_text(item.get('previous_summary_text', ''))
        item['new_summary_text'] = decrypt_text(item.get('new_summary_text', ''))
        payload.append(item)
    return jsonify(payload)


# ── Notifications ─────────────────────────────────────────────────────────────

@app.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications():
    user_id = request.current_user.get('user_id')
    notifs  = Notification.query.filter_by(user_id=user_id, is_read=False)\
                                .order_by(Notification.created_at.desc())\
                                .limit(20).all()
    return jsonify([n.to_dict() for n in notifs])


@app.route('/api/notifications/<int:nid>/read', methods=['PUT'])
@token_required
def mark_read(nid):
    notif = Notification.query.get_or_404(nid)
    if notif.user_id and notif.user_id != request.current_user.get('user_id'):
        return jsonify({'error': 'Forbidden'}), 403
    notif.is_read = True
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── Metrics / Observability ───────────────────────────────────────────────────

@app.route('/api/metrics/workload-history', methods=['GET'])
@role_required('admin', 'receptionist', 'doctor')
def workload_history():
    doctor_id = request.args.get('doctor_id', type=int)
    limit     = request.args.get('limit', 100, type=int)
    q = WorkloadMetric.query
    if doctor_id:
        q = q.filter_by(doctor_id=doctor_id)
    metrics = q.order_by(WorkloadMetric.timestamp.desc()).limit(limit).all()
    return jsonify([m.to_dict() for m in reversed(metrics)])


@app.route('/api/ops/worker-status', methods=['GET'])
@role_required('admin', 'receptionist')
def worker_status():
    latest_metric = WorkloadMetric.query.order_by(WorkloadMetric.timestamp.desc()).first()
    now = datetime.utcnow()

    heartbeat_age_seconds = None
    worker_alive = False
    if latest_metric and latest_metric.timestamp:
        heartbeat_age_seconds = max((now - latest_metric.timestamp).total_seconds(), 0.0)
        worker_alive = heartbeat_age_seconds <= 120

    backup_age_minutes = _latest_backup_age_minutes()
    return jsonify({
        'timestamp': now.isoformat(),
        'worker_alive': worker_alive,
        'worker_heartbeat_age_seconds': heartbeat_age_seconds,
        'queue_broker': app.config.get('REDIS_URL', ''),
        'backup_age_minutes': backup_age_minutes,
    })


@app.route('/api/ops/tasks/events', methods=['GET'])
@role_required('admin', 'receptionist')
def task_events():
    from models import AsyncTaskEvent

    limit = min(request.args.get('limit', 100, type=int), 500)
    task_name = (request.args.get('task_name') or '').strip()
    status = (request.args.get('status') or '').strip()

    q = AsyncTaskEvent.query
    if task_name:
        q = q.filter_by(task_name=task_name)
    if status:
        q = q.filter_by(status=status)

    rows = q.order_by(AsyncTaskEvent.timestamp.desc()).limit(limit).all()
    return jsonify([row.to_dict() for row in rows])


@app.route('/metrics', methods=['GET'])
def prometheus_metrics():
    if not app.config.get('ENABLE_PROMETHEUS_METRICS', True):
        return jsonify({'error': 'Metrics export disabled'}), 404

    try:
        db.session.execute(text('SELECT 1'))
        DB_UP_GAUGE.set(1)
    except Exception:
        DB_UP_GAUGE.set(0)

    try:
        import redis
        redis.from_url(app.config.get('REDIS_URL', 'redis://localhost:6379/0')).ping()
        REDIS_UP_GAUGE.set(1)
    except Exception:
        REDIS_UP_GAUGE.set(0)

    ACTIVE_DOCTORS_GAUGE.set(Doctor.query.filter_by(is_available=True).count())
    BOOKED_APPOINTMENTS_GAUGE.set(Appointment.query.filter_by(status='booked').count())
    SUMMARY_PENDING_GAUGE.set(ClinicalSummary.query.filter_by(status='pending').count())

    overloaded = 0
    for doc in Doctor.query.filter_by(is_available=True).all():
        ws = compute_workload_score(doc.id, datetime.utcnow().date())
        if ws.get('overloaded'):
            overloaded += 1
    OVERLOAD_RISK_GAUGE.set(overloaded)
    backup_age_minutes = _latest_backup_age_minutes()
    BACKUP_AGE_MINUTES_GAUGE.set(backup_age_minutes if backup_age_minutes is not None else -1)

    latest_metric = WorkloadMetric.query.order_by(WorkloadMetric.timestamp.desc()).first()
    if latest_metric and latest_metric.timestamp:
        WORKER_HEARTBEAT_AGE_SECONDS.set(max((datetime.utcnow() - latest_metric.timestamp).total_seconds(), 0.0))
    else:
        WORKER_HEARTBEAT_AGE_SECONDS.set(-1)

    latest_hospital = ForecastHistory.query.filter_by(scope='hospital').order_by(
        ForecastHistory.generated_at.desc()
    ).first()
    latest_doctor = ForecastHistory.query.filter_by(scope='doctor').order_by(
        ForecastHistory.generated_at.desc()
    ).first()
    if latest_hospital and latest_hospital.mae is not None:
        FORECAST_QUALITY_MAE.labels(scope='hospital').set(float(latest_hospital.mae))
    if latest_hospital and latest_hospital.rmse is not None:
        FORECAST_QUALITY_RMSE.labels(scope='hospital').set(float(latest_hospital.rmse))
    if latest_doctor and latest_doctor.mae is not None:
        FORECAST_QUALITY_MAE.labels(scope='doctor').set(float(latest_doctor.mae))
    if latest_doctor and latest_doctor.rmse is not None:
        FORECAST_QUALITY_RMSE.labels(scope='doctor').set(float(latest_doctor.rmse))

    return generate_latest(PROMETHEUS_REGISTRY), 200, {'Content-Type': CONTENT_TYPE_LATEST}


# ── Compatibility Routes For V2 Frontend ────────────────────────────────────

@app.route('/api/public/departments', methods=['GET'])
def public_departments():
    _ensure_departments()
    result = []
    for dept in Department.query.order_by(Department.name.asc()).all():
        doctor_count = Doctor.query.filter(Doctor.specialty == dept.name).count()
        result.append({**dept.to_dict(), 'doctor_count': doctor_count})
    return jsonify(result)


@app.route('/api/public/specializations', methods=['GET'])
def public_specializations():
    specialties = [x[0] for x in db.session.query(Doctor.specialty).distinct().all() if x[0]]
    return jsonify({'specializations': specialties})


@app.route('/api/public/doctors', methods=['GET'])
def public_doctors():
    search = (request.args.get('search') or '').strip().lower()
    specialty = (request.args.get('specialization') or '').strip().lower()
    doctors = Doctor.query.filter_by(is_available=True).all()
    rows = []
    for doc in doctors:
        payload = _doctor_payload(doc)
        if search and search not in payload['name'].lower():
            continue
        if specialty and specialty not in payload['specialization'].lower():
            continue
        rows.append(payload)
    return jsonify({'doctors': rows})


@app.route('/api/admin/dashboard', methods=['GET'])
@role_required('admin')
def admin_dashboard_compat():
    _ensure_departments()
    forecast_snapshot = forecast_workload(doctor_id=None, horizon_minutes=120)
    return jsonify({
        'doctors': Doctor.query.count(),
        'patients': Patient.query.count(),
        'appointments': Appointment.query.count(),
        'departments': Department.query.count(),
        'overload_expected_next_2h': bool(forecast_snapshot.get('overload_expected', False)),
        'peak_predicted_workload': forecast_snapshot.get('peak_predicted', 0.0),
    })


@app.route('/api/admin/departments', methods=['GET'])
@role_required('admin')
def admin_departments_list():
    _ensure_departments()
    rows = []
    for dept in Department.query.order_by(Department.name.asc()).all():
        doctor_count = Doctor.query.filter(Doctor.specialty == dept.name).count()
        rows.append({**dept.to_dict(), 'doctor_count': doctor_count})
    return jsonify(rows)


@app.route('/api/admin/departments/<int:dept_id>', methods=['GET'])
@role_required('admin')
def admin_departments_get(dept_id):
    _ensure_departments()
    dept = Department.query.get(dept_id)
    if not dept:
        return jsonify({'error': 'Department not found'}), 404
    return jsonify(dept.to_dict())


@app.route('/api/admin/departments', methods=['POST'])
@role_required('admin')
def admin_departments_create():
    _ensure_departments()
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    if Department.query.filter(db.func.lower(Department.name) == data.get('name', '').lower()).first():
        return jsonify({'error': 'Department already exists'}), 409
    dept = Department(
        name=data.get('name'),
        phone_number=data.get('phone_number', ''),
        email=data.get('email', ''),
        description=data.get('description', ''),
        head_doctor_id=data.get('head') or None,
    )
    db.session.add(dept)
    db.session.commit()
    return jsonify(dept.to_dict()), 201


@app.route('/api/admin/departments/<int:dept_id>', methods=['PUT'])
@role_required('admin')
def admin_departments_update(dept_id):
    _ensure_departments()
    dept = Department.query.get(dept_id)
    if not dept:
        return jsonify({'error': 'Department not found'}), 404
    data = request.get_json() or {}
    dept.name = data.get('name', dept.name)
    dept.phone_number = data.get('phone_number', dept.phone_number)
    dept.email = data.get('email', dept.email)
    dept.description = data.get('description', dept.description)
    dept.head_doctor_id = data.get('head', dept.head_doctor_id) or None
    db.session.commit()
    return jsonify(dept.to_dict())


@app.route('/api/admin/departments/<int:dept_id>', methods=['DELETE'])
@role_required('admin')
def admin_departments_delete(dept_id):
    _ensure_departments()
    dept = Department.query.get(dept_id)
    if not dept:
        return jsonify({'error': 'Department not found'}), 404
    db.session.delete(dept)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/admin/doctors', methods=['GET'])
@role_required('admin')
def admin_doctors_list():
    doctors = Doctor.query.all()
    return jsonify([_doctor_payload(d) for d in doctors])


@app.route('/api/admin/doctors/<int:doc_id>', methods=['GET'])
@role_required('admin')
def admin_doctors_get(doc_id):
    doc = Doctor.query.get_or_404(doc_id)
    return jsonify(_doctor_payload(doc))


@app.route('/api/admin/doctors', methods=['POST'])
@role_required('admin')
def admin_doctors_create():
    data = request.get_json() or {}
    for field in ('name', 'specialization', 'email', 'username', 'password'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 409

    user = User(
        username=data['username'],
        password_hash=hash_password(data['password']),
        role='doctor',
        email=data.get('email', ''),
    )
    db.session.add(user)
    db.session.flush()

    doc = Doctor(
        user_id=user.id,
        name=data['name'],
        specialty=data.get('specialization', 'General Medicine'),
        max_per_day=data.get('max_per_day', 40),
        is_available=True,
    )
    db.session.add(doc)
    db.session.flush()

    profile = _doctor_profile_row(doc.id)
    profile.phone = data.get('phone', '')
    profile.experience = data.get('experience', 0)
    profile.dr_consultation_fee = data.get('dr_consultation_fee', 500)

    db.session.commit()
    return jsonify(_doctor_payload(doc)), 201


@app.route('/api/admin/doctors/<int:doc_id>', methods=['PUT'])
@role_required('admin')
def admin_doctors_update(doc_id):
    doc = Doctor.query.get_or_404(doc_id)
    data = request.get_json() or {}
    doc.name = data.get('name', doc.name)
    doc.specialty = data.get('specialization', doc.specialty)
    if data.get('max_per_day'):
        doc.max_per_day = data['max_per_day']
    if doc.user_id:
        user = User.query.get(doc.user_id)
        if user and data.get('email'):
            user.email = data['email']
    profile = _doctor_profile_row(doc.id)
    profile.phone = data.get('phone', profile.phone)
    profile.experience = data.get('experience', profile.experience)
    profile.dr_consultation_fee = data.get('dr_consultation_fee', profile.dr_consultation_fee)
    db.session.commit()
    return jsonify(_doctor_payload(doc))


@app.route('/api/admin/doctors/<int:doc_id>', methods=['DELETE'])
@role_required('admin')
def admin_doctors_delete(doc_id):
    doc = Doctor.query.get_or_404(doc_id)
    if Appointment.query.filter_by(doctor_id=doc.id).count() > 0:
        return jsonify({'error': 'Cannot delete doctor with appointments'}), 400
    if doc.user_id:
        user = User.query.get(doc.user_id)
        if user:
            db.session.delete(user)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/admin/doctors/<int:doc_id>/blacklist', methods=['POST'])
@role_required('admin')
def admin_doctors_blacklist(doc_id):
    doc = Doctor.query.get_or_404(doc_id)
    profile = _doctor_profile_row(doc.id)
    profile.is_blacklisted = not bool(profile.is_blacklisted)
    db.session.commit()
    return jsonify({'doctor_id': doc.id, 'is_blacklisted': profile.is_blacklisted})


@app.route('/api/admin/patients', methods=['GET'])
@role_required('admin')
def admin_patients_list():
    rows = Patient.query.order_by(Patient.id.desc()).all()
    return jsonify([_patient_payload(p) for p in rows])


@app.route('/api/admin/patients/<int:pid>', methods=['GET'])
@role_required('admin')
def admin_patients_get(pid):
    p = Patient.query.get_or_404(pid)
    return jsonify(_patient_payload(p))


@app.route('/api/admin/patients', methods=['POST'])
@role_required('admin')
def admin_patients_create():
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    p = Patient(
        name=data['name'],
        email=encrypt_text(data.get('email', '')),
        phone=encrypt_text(data.get('phone', '')),
        dob=data.get('dob', ''),
    )
    db.session.add(p)
    db.session.flush()
    profile = _patient_profile_row(p.id)
    profile.gender = data.get('gender', '')
    profile.city = data.get('city', '')
    profile.state = data.get('state', '')
    profile.blood_type = data.get('blood_type', '')
    profile.allergies = data.get('allergies', '')
    profile.medical_summary = encrypt_text(data.get('medical_summary', ''))
    profile.address = data.get('address', '')
    profile.zipcode = data.get('zipcode', '')
    db.session.commit()
    return jsonify(_patient_payload(p)), 201


@app.route('/api/admin/patients/<int:pid>', methods=['PUT'])
@role_required('admin')
def admin_patients_update(pid):
    p = Patient.query.get_or_404(pid)
    data = request.get_json() or {}
    p.name = data.get('name', p.name)
    if 'email' in data:
        p.email = encrypt_text(data.get('email', ''))
    if 'phone' in data:
        p.phone = encrypt_text(data.get('phone', ''))
    p.dob = data.get('dob', p.dob)
    profile = _patient_profile_row(p.id)
    if 'gender' in data:
        profile.gender = data['gender']
    if 'city' in data:
        profile.city = data['city']
    if 'state' in data:
        profile.state = data['state']
    if 'blood_type' in data:
        profile.blood_type = data['blood_type']
    if 'allergies' in data:
        profile.allergies = data['allergies']
    if 'medical_summary' in data:
        profile.medical_summary = encrypt_text(data.get('medical_summary', ''))
    if 'address' in data:
        profile.address = data['address']
    if 'zipcode' in data:
        profile.zipcode = data['zipcode']
    db.session.commit()
    return jsonify(_patient_payload(p))


@app.route('/api/admin/patients/<int:pid>', methods=['DELETE'])
@role_required('admin')
def admin_patients_delete(pid):
    p = Patient.query.get_or_404(pid)
    if Appointment.query.filter_by(patient_id=p.id).count() > 0:
        return jsonify({'error': 'Cannot delete patient with appointments'}), 400
    db.session.delete(p)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/admin/appointments', methods=['GET'])
@role_required('admin')
def admin_appointments_list():
    rows = Appointment.query.order_by(Appointment.scheduled_at.desc()).all()
    payload = []
    for item in rows:
        row = _appointment_compact(item)
        row['predicted_wait_minutes'] = estimate_wait_minutes(item.doctor_id, item.scheduled_at)
        row['summary_status'] = item.summary.status if item.summary else 'pending'
        payload.append(row)
    return jsonify(payload)


@app.route('/api/doctor/dashboard', methods=['GET'])
@role_required('doctor')
def doctor_dashboard_compat():
    doc_id = _current_doctor_id()
    if not doc_id:
        return jsonify({'error': 'Doctor profile not found'}), 404
    today = datetime.utcnow().date()
    appts = Appointment.query.filter_by(doctor_id=doc_id).all()
    today_appts = [a for a in appts if a.scheduled_at.date() == today]
    return jsonify({
        'todayAppointments': len(today_appts),
        'totalPatients': len({a.patient_id for a in appts}),
        'pendingAppointments': len([a for a in appts if a.status == 'booked']),
        'completedToday': len([a for a in today_appts if a.status == 'completed']),
    })


@app.route('/api/doctor/appointments', methods=['GET'])
@role_required('doctor')
def doctor_appointments_compat():
    doc_id = _current_doctor_id()
    if not doc_id:
        return jsonify([])
    rows = Appointment.query.filter_by(doctor_id=doc_id).order_by(Appointment.scheduled_at.desc()).all()
    payload = []
    for item in rows:
        row = _appointment_compact(item)
        row['predicted_wait_minutes'] = estimate_wait_minutes(item.doctor_id, item.scheduled_at)
        row['summary_status'] = item.summary.status if item.summary else 'pending'
        payload.append(row)
    return jsonify(payload)


@app.route('/api/doctor/appointments/<int:appt_id>/complete', methods=['POST'])
@role_required('doctor')
def doctor_complete_compat(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    doc_id = _current_doctor_id()
    if not doc_id or appt.doctor_id != doc_id:
        return jsonify({'error': 'Forbidden'}), 403
    if appt.status == 'completed':
        return jsonify({'status': 'ok'})
    if appt.status != 'booked':
        return jsonify({'error': 'Only booked appointment can be completed'}), 400
    appt.status = 'completed'
    appt.completed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/doctor/appointments/<int:appt_id>/cancel', methods=['POST'])
@role_required('doctor')
def doctor_cancel_compat(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    doc_id = _current_doctor_id()
    if not doc_id or appt.doctor_id != doc_id:
        return jsonify({'error': 'Forbidden'}), 403
    if appt.status != 'booked':
        return jsonify({'error': 'Only booked appointment can be cancelled'}), 400
    appt.status = 'cancelled'
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/doctor/appointments/<int:appt_id>/diagnosis', methods=['GET'])
@role_required('doctor')
def doctor_get_diagnosis_compat(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    doc_id = _current_doctor_id()
    if not doc_id or appt.doctor_id != doc_id:
        return jsonify({'error': 'Forbidden'}), 403
    diag = AppointmentDiagnosisCompat.query.filter_by(appointment_id=appt_id).first()
    data = diag.to_dict() if diag else {}
    return jsonify({
        'appointment': {
            'id': appt.id,
            'date': appt.scheduled_at.strftime('%Y-%m-%d'),
            'time': appt.scheduled_at.strftime('%H:%M'),
            'patient': {'id': appt.patient_id, 'name': appt.patient.name if appt.patient else ''},
        },
        **data,
    })


@app.route('/api/doctor/appointments/<int:appt_id>/diagnosis', methods=['POST'])
@role_required('doctor')
def doctor_save_diagnosis_compat(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    doc_id = _current_doctor_id()
    if not doc_id or appt.doctor_id != doc_id:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    diag = AppointmentDiagnosisCompat.query.filter_by(appointment_id=appt_id).first()
    if not diag:
        diag = AppointmentDiagnosisCompat(appointment_id=appt_id)
        db.session.add(diag)
    diag.diagnosis = data.get('diagnosis', '')
    diag.symptoms = data.get('symptoms', '')
    diag.severity = data.get('severity', '')
    diag.treatment_plan = data.get('treatment_plan', '')
    diag.follow_up = data.get('follow_up', 'no')
    diag.notes = data.get('notes', '')
    diag.prescription_json = json.dumps(data.get('medicines', []))
    diag.updated_at = datetime.utcnow()
    if appt.status == 'booked':
        appt.status = 'completed'
        appt.completed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/doctor/patients', methods=['GET'])
@role_required('doctor')
def doctor_patients_compat():
    doc_id = _current_doctor_id()
    if not doc_id:
        return jsonify([])
    patient_ids = {x.patient_id for x in Appointment.query.filter_by(doctor_id=doc_id).all()}
    rows = [Patient.query.get(pid) for pid in patient_ids]
    rows = [r for r in rows if r]
    return jsonify([_patient_payload(r) for r in rows])


@app.route('/api/doctor/patients/<int:pid>/history', methods=['GET'])
@role_required('doctor')
def doctor_patient_history_compat(pid):
    doc_id = _current_doctor_id()
    if not doc_id:
        return jsonify({'error': 'Doctor profile not found'}), 404
    p = Patient.query.get_or_404(pid)
    appts = Appointment.query.filter_by(doctor_id=doc_id, patient_id=pid).order_by(Appointment.scheduled_at.desc()).all()
    visits = []
    diagnoses = []
    prescriptions = []
    for a in appts:
        diag = AppointmentDiagnosisCompat.query.filter_by(appointment_id=a.id).first()
        d = diag.to_dict() if diag else {}
        visit = _appointment_compact(a)
        visit['diagnosis'] = d.get('diagnosis', '')
        visits.append(visit)
        if d.get('diagnosis'):
            diagnoses.append({
                'date': visit['date'],
                'diagnosis': d.get('diagnosis', ''),
                'severity': d.get('severity', ''),
            })
        for med in d.get('prescription', []):
            prescriptions.append({
                'date': visit['date'],
                'medicine': med.get('medicine', ''),
                'dosage': med.get('dosage', ''),
                'frequency': med.get('frequency', ''),
                'duration': med.get('duration', ''),
            })
    return jsonify({
        'patient_name': p.name,
        'patient': _patient_payload(p),
        'visits': visits,
        'diagnoses': diagnoses,
        'prescriptions': prescriptions,
    })


@app.route('/api/doctor/availability', methods=['GET'])
@role_required('doctor')
def doctor_availability_get_compat():
    doc_id = _current_doctor_id()
    rows = DoctorAvailabilityCompat.query.filter_by(doctor_id=doc_id).all()
    return jsonify([r.to_dict() for r in rows])


@app.route('/api/doctor/availability', methods=['POST'])
@role_required('doctor')
def doctor_availability_save_compat():
    doc_id = _current_doctor_id()
    data = request.get_json() or {}
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    def _is_valid_hhmm(value: str) -> bool:
        try:
            datetime.strptime((value or '').strip(), '%H:%M')
            return True
        except Exception:
            return False

    def _lt(a: str, b: str) -> bool:
        return datetime.strptime(a, '%H:%M') < datetime.strptime(b, '%H:%M')

    DoctorAvailabilityCompat.query.filter_by(doctor_id=doc_id).delete()
    rows = []
    for i, day in enumerate(days):
        if data.get(f'available_{i}'):
            start_time = data.get(f'start_time_{i}', '09:00')
            end_time = data.get(f'end_time_{i}', '17:00')
            break_start = data.get(f'break_start_{i}', '')
            break_end = data.get(f'break_end_{i}', '')

            if not _is_valid_hhmm(start_time) or not _is_valid_hhmm(end_time):
                return jsonify({'error': f'Invalid time format for {day}'}), 400
            if not _lt(start_time, end_time):
                return jsonify({'error': f'start_time must be before end_time for {day}'}), 400
            if break_start or break_end:
                if not _is_valid_hhmm(break_start) or not _is_valid_hhmm(break_end):
                    return jsonify({'error': f'Invalid break time format for {day}'}), 400
                if not (_lt(start_time, break_start) and _lt(break_start, break_end) and _lt(break_end, end_time)):
                    return jsonify({'error': f'Break window must be inside working hours for {day}'}), 400

            row = DoctorAvailabilityCompat(
                doctor_id=doc_id,
                day_of_week=day,
                start_time=start_time,
                end_time=end_time,
                slot_duration=int(data.get(f'slot_duration_{i}', 30)),
                break_start=break_start,
                break_end=break_end,
            )
            db.session.add(row)
            rows.append(row)
    db.session.commit()
    return jsonify({'status': 'ok', 'count': len(rows)})


@app.route('/api/doctor/profile', methods=['GET'])
@role_required('doctor')
def doctor_profile_get_compat():
    doc_id = _current_doctor_id()
    if not doc_id:
        return jsonify({'error': 'Doctor profile not found'}), 404
    doc = Doctor.query.get_or_404(doc_id)
    user = User.query.get(doc.user_id) if doc.user_id else None
    extra = _doctor_profile_row(doc.id)
    return jsonify({
        'id': doc.id,
        'name': doc.name,
        'email': user.email if user else '',
        'phone': extra.phone,
        'specialization': doc.specialty,
        'experience': extra.experience,
        'dr_consultation_fee': extra.dr_consultation_fee,
    })


@app.route('/api/doctor/profile', methods=['PUT'])
@role_required('doctor')
def doctor_profile_update_compat():
    doc_id = _current_doctor_id()
    doc = Doctor.query.get_or_404(doc_id)
    data = request.get_json() or {}
    doc.name = data.get('name', doc.name)
    doc.specialty = data.get('specialization', doc.specialty)
    if doc.user_id:
        user = User.query.get(doc.user_id)
        if user and data.get('email'):
            user.email = data['email']
    extra = _doctor_profile_row(doc.id)
    extra.phone = data.get('phone', extra.phone)
    extra.experience = data.get('experience', extra.experience)
    extra.dr_consultation_fee = data.get('dr_consultation_fee', extra.dr_consultation_fee)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/patient/dashboard', methods=['GET'])
@role_required('patient')
def patient_dashboard_compat():
    pid = _current_patient_id()
    if not pid:
        return jsonify({'upcoming_count': 0, 'completed_count': 0, 'today_appointments_count': 0, 'pending_count': 0})
    now = datetime.utcnow()
    rows = Appointment.query.filter_by(patient_id=pid).all()
    return jsonify({
        'upcoming_count': len([a for a in rows if a.scheduled_at >= now and a.status == 'booked']),
        'completed_count': len([a for a in rows if a.status == 'completed']),
        'today_appointments_count': len([a for a in rows if a.scheduled_at.date() == now.date()]),
        'pending_count': len([a for a in rows if a.status == 'booked']),
        'forecast_overload_expected': bool(forecast_workload(doctor_id=None, horizon_minutes=120).get('overload_expected', False)),
    })


@app.route('/api/patient/profile', methods=['GET'])
@role_required('patient')
def patient_profile_get_compat():
    pid = _current_patient_id()
    if not pid:
        return jsonify({'error': 'Patient profile not found'}), 404
    p = Patient.query.get_or_404(pid)
    return jsonify(_patient_payload(p))


@app.route('/api/patient/profile', methods=['PUT'])
@role_required('patient')
def patient_profile_update_compat():
    pid = _current_patient_id()
    if not pid:
        return jsonify({'error': 'Patient profile not found'}), 404
    p = Patient.query.get_or_404(pid)
    data = request.get_json() or {}
    p.name = data.get('name', p.name)
    if 'email' in data:
        p.email = encrypt_text(data.get('email', ''))
    if 'phone' in data:
        p.phone = encrypt_text(data.get('phone', ''))
    p.dob = data.get('dob', p.dob)
    extra = _patient_profile_row(pid)
    if 'gender' in data:
        extra.gender = data['gender']
    if 'address' in data:
        extra.address = data['address']
    if 'city' in data:
        extra.city = data['city']
    if 'state' in data:
        extra.state = data['state']
    if 'zipcode' in data:
        extra.zipcode = data['zipcode']
    if 'blood_type' in data:
        extra.blood_type = data['blood_type']
    if 'allergies' in data:
        extra.allergies = data['allergies']
    if 'medical_summary' in data:
        extra.medical_summary = encrypt_text(data.get('medical_summary', ''))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/patient/appointments', methods=['GET'])
@role_required('patient')
def patient_appointments_list_compat():
    pid = _current_patient_id()
    if not pid:
        return jsonify({'upcoming': [], 'past': []})
    now = datetime.utcnow()
    rows = Appointment.query.filter_by(patient_id=pid).order_by(Appointment.scheduled_at.asc()).all()
    upcoming, past = [], []
    for a in rows:
        item = _appointment_compact(a)
        item['predicted_wait_minutes'] = estimate_wait_minutes(a.doctor_id, a.scheduled_at)
        item['summary_status'] = a.summary.status if a.summary else 'pending'
        if a.scheduled_at >= now and a.status == 'booked':
            upcoming.append(item)
        else:
            past.append(item)
    return jsonify({'upcoming': upcoming, 'past': past})


@app.route('/api/patient/appointments/<int:appt_id>', methods=['GET'])
@role_required('patient')
def patient_appointment_detail_compat(appt_id):
    pid = _current_patient_id()
    appt = Appointment.query.get_or_404(appt_id)
    if appt.patient_id != pid:
        return jsonify({'error': 'Forbidden'}), 403
    diag = AppointmentDiagnosisCompat.query.filter_by(appointment_id=appt.id).first()
    diagnosis = diag.to_dict() if diag else {}
    return jsonify({
        'id': appt.id,
        'date': appt.scheduled_at.strftime('%Y-%m-%d'),
        'time': appt.scheduled_at.strftime('%H:%M'),
        'status': appt.status,
        'reason': appt.notes or '',
        'doctor': {
            'id': appt.doctor_id,
            'name': appt.doctor.name if appt.doctor else '',
            'specialization': appt.doctor.specialty if appt.doctor else '',
        },
        'treatment': {
            'diagnosis': diagnosis.get('diagnosis', ''),
            'symptoms': diagnosis.get('symptoms', ''),
            'severity': diagnosis.get('severity', ''),
            'plan': diagnosis.get('treatment_plan', ''),
            'follow_up': diagnosis.get('follow_up', 'no'),
            'notes': diagnosis.get('notes', ''),
        },
        'prescription': diagnosis.get('prescription', []),
        'predicted_wait_minutes': estimate_wait_minutes(appt.doctor_id, appt.scheduled_at),
        'summary_status': (appt.summary.status if appt.summary else 'pending'),
        'summary_reviewed': (appt.summary.is_reviewed if appt.summary else False),
    })


@app.route('/api/patient/appointments/<int:appt_id>/cancel', methods=['POST'])
@role_required('patient')
def patient_cancel_appointment_compat(appt_id):
    pid = _current_patient_id()
    appt = Appointment.query.get_or_404(appt_id)
    if appt.patient_id != pid:
        return jsonify({'error': 'Forbidden'}), 403
    if appt.status != 'booked':
        return jsonify({'error': 'Only booked appointment can be cancelled'}), 400
    appt.status = 'cancelled'
    db.session.commit()
    write_audit(
        'cancel_appointment',
        'appointment',
        appt.id,
        {'reason': 'patient_self_cancel'},
        request.current_user.get('user_id'),
    )
    return jsonify({'status': 'ok'})


@app.route('/api/patient/doctors', methods=['GET'])
@role_required('patient')
def patient_doctors_search_compat():
    search = (request.args.get('search') or '').strip().lower()
    rows = []
    target_iso = request.args.get('target_at', '').strip()
    target_dt = None
    if target_iso:
        try:
            target_dt = datetime.fromisoformat(target_iso)
        except ValueError:
            target_dt = None

    for doc in Doctor.query.filter_by(is_available=True).all():
        payload = _doctor_payload(doc)
        if search and search not in payload['name'].lower():
            continue
        if target_dt:
            ws = compute_workload_score(doc.id, target_dt.date())
            payload['predicted_wait_minutes'] = estimate_wait_minutes(doc.id, target_dt)
            payload['overload_score'] = ws.get('score', 0.0)
            payload['overloaded'] = bool(ws.get('overloaded', False))
        rows.append(payload)
    return jsonify({'doctors': rows})


@app.route('/api/patient/doctors/<int:doc_id>/slots', methods=['GET'])
@role_required('patient')
def patient_doctor_slots_compat(doc_id):
    date_value = request.args.get('date') or datetime.utcnow().strftime('%Y-%m-%d')
    try:
        target = datetime.strptime(date_value, '%Y-%m-%d')
    except ValueError:
        return jsonify({'slots': []})

    weekday = target.strftime('%A')
    day_row = DoctorAvailabilityCompat.query.filter_by(doctor_id=doc_id, day_of_week=weekday).first()
    day_cfg = day_row.to_dict() if day_row else None

    if not day_cfg:
        day_cfg = {
            'start_time': '09:00',
            'end_time': '17:00',
            'slot_duration': 30,
            'break_start': '',
            'break_end': '',
        }

    start_h, start_m = [int(x) for x in day_cfg['start_time'].split(':')]
    end_h, end_m = [int(x) for x in day_cfg['end_time'].split(':')]
    slot_minutes = int(day_cfg.get('slot_duration', 30))

    start_dt = target.replace(hour=start_h, minute=start_m)
    end_dt = target.replace(hour=end_h, minute=end_m)

    taken = {
        a.scheduled_at.strftime('%H:%M')
        for a in Appointment.query.filter_by(doctor_id=doc_id, status='booked').all()
        if a.scheduled_at.date() == target.date()
    }

    slots = []
    cursor = start_dt
    while cursor < end_dt:
        t = cursor.strftime('%H:%M')
        in_break = False
        if day_cfg.get('break_start') and day_cfg.get('break_end'):
            in_break = day_cfg['break_start'] <= t < day_cfg['break_end']

        if t not in taken and not in_break:
            slots.append(t)
        cursor += timedelta(minutes=slot_minutes)

    return jsonify({
        'slots': slots,
        'availability': {
            'start_time': day_cfg.get('start_time', '09:00'),
            'end_time': day_cfg.get('end_time', '17:00'),
            'slot_duration': slot_minutes,
            'break_start': day_cfg.get('break_start', ''),
            'break_end': day_cfg.get('break_end', ''),
        },
    })


@app.route('/api/patient/appointments/create-payment-order', methods=['POST'])
@role_required('patient')
def patient_create_payment_order_compat():
    data = request.get_json() or {}
    doctor_id = data.get('doctor_id')
    date_str = data.get('appointment_date')
    time_str = data.get('appointment_time')
    if not doctor_id or not date_str or not time_str:
        return jsonify({'error': 'doctor_id, appointment_date and appointment_time are required'}), 400
    doc = Doctor.query.get(doctor_id)
    if not doc:
        return jsonify({'error': 'Doctor not found'}), 404

    def create_gateway_order(amount_cents: int, receipt: str, notes: dict):
        key_id = app.config.get('RAZORPAY_KEY_ID', '').strip()
        key_secret = app.config.get('RAZORPAY_KEY_SECRET', '').strip()
        if not key_id or not key_secret:
            return None, 'Razorpay key configuration missing'

        payload = json.dumps({
            'amount': amount_cents,
            'currency': 'INR',
            'receipt': receipt,
            'notes': notes,
        }).encode('utf-8')

        token = base64.b64encode(f"{key_id}:{key_secret}".encode('utf-8')).decode('utf-8')
        req = urllib_request.Request(
            'https://api.razorpay.com/v1/orders',
            data=payload,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Basic {token}',
            }
        )

        try:
            with urllib_request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data, None
        except urllib_error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode('utf-8'))
                reason = body.get('error', {}).get('description') or body.get('error', {}).get('reason')
                return None, reason or f'Razorpay API error ({exc.code})'
            except Exception:
                return None, f'Razorpay API error ({exc.code})'
        except Exception as exc:
            return None, f'Unable to create Razorpay order: {str(exc)}'

    profile = _doctor_profile_row(doc.id)
    amount = int(profile.dr_consultation_fee * 100)

    receipt = f"rcpt_{uuid.uuid4().hex[:12]}"
    rz_order, err = create_gateway_order(
        amount_cents=amount,
        receipt=receipt,
        notes={
            'doctor_id': str(doctor_id),
            'appointment_date': str(date_str),
            'appointment_time': str(time_str),
        }
    )
    if err or not rz_order or not rz_order.get('id'):
        return jsonify({'error': err or 'Failed to create payment order'}), 502

    order_id = rz_order['id']

    existing = PaymentOrderCompat.query.filter_by(
        patient_id=_current_patient_id(),
        doctor_id=doctor_id,
        appointment_date=date_str,
        appointment_time=time_str,
        status='created',
    ).first()
    if existing:
        return jsonify({'error': 'A pending payment order already exists for this slot'}), 409

    row = PaymentOrderCompat(
        order_id=order_id,
        patient_id=_current_patient_id(),
        doctor_id=doctor_id,
        appointment_date=date_str,
        appointment_time=time_str,
        reason=data.get('reason', ''),
        amount_cents=amount,
        status='created',
    )
    db.session.add(row)
    db.session.commit()

    write_audit(
        'payment_order_created',
        'payment_order',
        row.id,
        {
            'order_id': order_id,
            'doctor_id': doctor_id,
            'appointment_date': date_str,
            'appointment_time': time_str,
            'amount_cents': amount,
        },
        request.current_user.get('user_id'),
    )

    return jsonify({
        'order_id': order_id,
        'amount': amount,
        'razorpay_key': app.config.get('RAZORPAY_KEY_ID', ''),
    })


@app.route('/api/patient/appointments/verify-payment', methods=['POST'])
@role_required('patient')
def patient_verify_payment_compat():
    data = request.get_json() or {}
    order_id = data.get('razorpay_order_id')
    payment_id = data.get('razorpay_payment_id', '')
    signature = data.get('razorpay_signature', '')
    order = PaymentOrderCompat.query.filter_by(order_id=order_id).first() if order_id else None
    if not order:
        return jsonify({'error': 'Invalid order id'}), 400
    if order.status == 'verified':
        return jsonify({'error': 'Order already verified'}), 409
    if order.status != 'created':
        ui_error = 'Payment could not be verified for this order. Please create a new order.'
        return jsonify({'error': f'Invalid order status: {order.status}', 'ui_error': ui_error}), 409

    if (order.verification_attempts or 0) >= 5:
        order.status = 'failed'
        order.failure_reason = 'too_many_verification_attempts'
        db.session.commit()
        return jsonify({'error': 'Too many verification attempts. Create a new order.'}), 429
    pid = _current_patient_id()
    if not pid or pid != order.patient_id:
        return jsonify({'error': 'Forbidden'}), 403

    if not payment_id or not signature:
        order.verification_attempts = (order.verification_attempts or 0) + 1
        order.failure_reason = 'missing_payment_or_signature'
        db.session.commit()
        write_audit(
            'payment_verification_failed',
            'payment_order',
            order.id,
            {'reason': 'missing_payment_or_signature', 'order_id': order_id},
            request.current_user.get('user_id'),
        )
        return jsonify({
            'error': 'razorpay_payment_id and razorpay_signature are required',
            'ui_error': 'Payment verification details are missing. Please retry payment.',
        }), 400

    signature_required = app.config.get('RAZORPAY_SIGNATURE_REQUIRED', True)
    key_secret = app.config.get('RAZORPAY_KEY_SECRET', '')
    if signature_required:
        if not key_secret:
            return jsonify({'error': 'Payment gateway misconfigured: missing RAZORPAY_KEY_SECRET'}), 503
        message = f"{order_id}|{payment_id}".encode('utf-8')
        expected_signature = hmac.new(
            key_secret.encode('utf-8'),
            message,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            order.status = 'failed'
            order.failure_reason = 'signature_mismatch'
            order.verification_attempts = (order.verification_attempts or 0) + 1
            db.session.commit()
            write_audit(
                'payment_verification_failed',
                'payment_order',
                order.id,
                {'reason': 'signature_mismatch', 'order_id': order_id},
                request.current_user.get('user_id'),
            )
            return jsonify({
                'error': 'Invalid payment signature',
                'ui_error': 'Payment signature verification failed. Please retry payment.',
            }), 400

    doctor = Doctor.query.get(order.doctor_id)
    if not doctor:
        return jsonify({'error': 'Doctor not found'}), 404

    try:
        scheduled_at = datetime.fromisoformat(f"{order.appointment_date}T{order.appointment_time}:00")
    except ValueError:
        return jsonify({'error': 'Invalid appointment date/time'}), 400

    result = book_appointment(
        patient_id=pid,
        specialty=doctor.specialty,
        scheduled_at=scheduled_at,
        notes=order.reason,
        priority='normal',
        preferred_doctor_id=doctor.id,
    )
    if 'error' in result:
        order.status = 'failed'
        order.failure_reason = str(result.get('error', 'booking_failed'))
        order.verification_attempts = (order.verification_attempts or 0) + 1
        db.session.commit()
        write_audit(
            'payment_verification_failed',
            'payment_order',
            order.id,
            {'reason': order.failure_reason, 'order_id': order_id},
            request.current_user.get('user_id'),
        )
        return jsonify({
            'error': result['error'],
            'ui_error': 'Payment was captured, but booking failed. Please contact support.',
        }), 422

    order.status = 'verified'
    order.failure_reason = ''
    order.razorpay_payment_id = payment_id
    order.verification_attempts = (order.verification_attempts or 0) + 1
    db.session.commit()

    write_audit(
        'payment_verified',
        'payment_order',
        order.id,
        {
            'order_id': order.order_id,
            'payment_id': payment_id,
            'appointment_id': result['appointment'].id,
        },
        request.current_user.get('user_id'),
    )

    return jsonify({'status': 'ok', 'appointment_id': result['appointment'].id})


@app.route('/api/patient/appointments/reschedule', methods=['POST'])
@role_required('patient')
def patient_reschedule_compat():
    data = request.get_json() or {}
    appt_id = data.get('appointment_id')
    new_date = data.get('appointment_date')
    new_time = data.get('appointment_time')
    if not appt_id or not new_date or not new_time:
        return jsonify({'error': 'appointment_id, appointment_date and appointment_time are required'}), 400
    appt = Appointment.query.get_or_404(appt_id)
    pid = _current_patient_id()
    if appt.patient_id != pid:
        return jsonify({'error': 'Forbidden'}), 403
    try:
        new_dt = datetime.fromisoformat(f"{new_date}T{new_time}:00")
    except ValueError:
        return jsonify({'error': 'Invalid date/time'}), 400

    if new_dt <= datetime.utcnow():
        return jsonify({'error': 'Reschedule time must be in the future'}), 400

    if Appointment.query.filter(
        Appointment.id != appt.id,
        Appointment.doctor_id == appt.doctor_id,
        Appointment.scheduled_at == new_dt,
        Appointment.status == 'booked',
    ).first():
        return jsonify({'error': 'Requested slot is already booked for this doctor'}), 409

    old_value = appt.scheduled_at.isoformat()
    appt.scheduled_at = new_dt
    db.session.commit()
    write_audit(
        'appointment_rescheduled',
        'appointment',
        appt.id,
        {'old_time': old_value, 'new_time': new_dt.isoformat()},
        request.current_user.get('user_id'),
    )
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=app.config.get('DEBUG', False))

