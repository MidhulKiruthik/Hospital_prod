from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json
from sqlalchemy import event, UniqueConstraint, Index

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id          = db.Column(db.Integer, primary_key=True)
    username    = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role        = db.Column(db.String(32), nullable=False)   # admin|doctor|receptionist|patient
    email       = db.Column(db.String(128), unique=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    lockout_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    token_version = db.Column(db.Integer, default=0, nullable=False)

    def to_dict(self):
        return {'id': self.id, 'username': self.username,
                'role': self.role, 'email': self.email}


class Doctor(db.Model):
    __tablename__ = 'doctors'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'))
    name        = db.Column(db.String(128), nullable=False)
    specialty   = db.Column(db.String(64), nullable=False)
    max_per_day = db.Column(db.Integer, default=40)
    is_available = db.Column(db.Boolean, default=True)
    appointments = db.relationship('Appointment', backref='doctor', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'specialty': self.specialty,
            'max_per_day': self.max_per_day,
            'is_available': self.is_available
        }


class Patient(db.Model):
    __tablename__ = 'patients'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    name        = db.Column(db.String(128), nullable=False)
    dob         = db.Column(db.String(16))
    phone       = db.Column(db.String(20))
    email       = db.Column(db.String(128))
    appointments = db.relationship('Appointment', backref='patient', lazy='dynamic')

    def to_dict(self):
        return {'id': self.id, 'name': self.name,
                'dob': self.dob, 'phone': self.phone, 'email': self.email}


class Appointment(db.Model):
    __tablename__ = 'appointments'
    __table_args__ = (
        Index('ix_appointments_doctor_time_status', 'doctor_id', 'scheduled_at', 'status'),
        Index('ix_appointments_patient_time', 'patient_id', 'scheduled_at'),
    )
    id          = db.Column(db.Integer, primary_key=True)
    patient_id  = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    doctor_id   = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    status      = db.Column(db.String(32), default='booked')   # booked|completed|cancelled
    notes       = db.Column(db.Text, default='')
    priority    = db.Column(db.String(16), default='normal')   # normal|urgent|emergency
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    workload_score_at_booking = db.Column(db.Float, default=0.0)
    summary     = db.relationship('ClinicalSummary', backref='appointment', uselist=False)

    def to_dict(self):
        return {
            'id': self.id,
            'patient_id': self.patient_id,
            'patient_name': self.patient.name if self.patient else '',
            'doctor_id': self.doctor_id,
            'doctor_name': self.doctor.name if self.doctor else '',
            'doctor_specialty': self.doctor.specialty if self.doctor else '',
            'scheduled_at': self.scheduled_at.isoformat(),
            'status': self.status,
            'notes': self.notes,
            'priority': self.priority,
            'created_at': self.created_at.isoformat(),
            'workload_score': self.workload_score_at_booking,
        }


class ClinicalSummary(db.Model):
    __tablename__ = 'clinical_summaries'
    id              = db.Column(db.Integer, primary_key=True)
    appointment_id  = db.Column(db.Integer, db.ForeignKey('appointments.id'), unique=True)
    summary_text    = db.Column(db.Text)
    chief_complaint = db.Column(db.Text, default='')
    findings        = db.Column(db.Text, default='')
    assessment      = db.Column(db.Text, default='')
    plan            = db.Column(db.Text, default='')
    status          = db.Column(db.String(16), default='pending')  # pending|ready|error
    generation_method = db.Column(db.String(64), default='rule-based-nlp')
    generation_model  = db.Column(db.String(128), default='')
    source_notes_hash = db.Column(db.String(64), default='')
    is_reviewed = db.Column(db.Boolean, default=False)
    reviewed_by_user_id = db.Column(db.Integer, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_notes = db.Column(db.Text, default='')
    generated_at    = db.Column(db.DateTime, default=datetime.utcnow)
    processing_time_s = db.Column(db.Float, default=0.0)

    def to_dict(self):
        return {
            'id': self.id,
            'appointment_id': self.appointment_id,
            'summary_text': self.summary_text,
            'chief_complaint': self.chief_complaint,
            'findings': self.findings,
            'assessment': self.assessment,
            'plan': self.plan,
            'status': self.status,
            'generation_method': self.generation_method,
            'generation_model': self.generation_model,
            'source_notes_hash': self.source_notes_hash,
            'is_reviewed': self.is_reviewed,
            'reviewed_by_user_id': self.reviewed_by_user_id,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'review_notes': self.review_notes,
            'generated_at': self.generated_at.isoformat() if self.generated_at else None,
            'processing_time_s': self.processing_time_s,
        }


class ClinicalSummaryRevision(db.Model):
    __tablename__ = 'clinical_summary_revisions'
    id = db.Column(db.Integer, primary_key=True)
    summary_id = db.Column(db.Integer, db.ForeignKey('clinical_summaries.id'), nullable=False)
    edited_by_user_id = db.Column(db.Integer, nullable=True)
    previous_summary_text = db.Column(db.Text, default='')
    new_summary_text = db.Column(db.Text, default='')
    edit_note = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'summary_id': self.summary_id,
            'edited_by_user_id': self.edited_by_user_id,
            'previous_summary_text': self.previous_summary_text,
            'new_summary_text': self.new_summary_text,
            'edit_note': self.edit_note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class WorkloadMetric(db.Model):
    __tablename__ = 'workload_metrics'
    id          = db.Column(db.Integer, primary_key=True)
    doctor_id   = db.Column(db.Integer, db.ForeignKey('doctors.id'))
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)
    score       = db.Column(db.Float)
    queue_length = db.Column(db.Integer, default=0)
    completed_today = db.Column(db.Integer, default=0)
    cancelled_today = db.Column(db.Integer, default=0)


class ForecastHistory(db.Model):
    __tablename__ = 'forecast_history'
    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(16), nullable=False)  # hospital|doctor
    scope_id = db.Column(db.Integer, nullable=True)
    selected_model = db.Column(db.String(32), default='baseline')
    effective_model = db.Column(db.String(64), default='')
    horizon_minutes = db.Column(db.Integer, default=120)
    peak_predicted = db.Column(db.Float, default=0.0)
    avg_predicted = db.Column(db.Float, default=0.0)
    overload_expected = db.Column(db.Boolean, default=False)
    mae = db.Column(db.Float, nullable=True)
    rmse = db.Column(db.Float, nullable=True)
    payload_json = db.Column(db.Text, default='{}')
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'scope': self.scope,
            'scope_id': self.scope_id,
            'selected_model': self.selected_model,
            'effective_model': self.effective_model,
            'horizon_minutes': self.horizon_minutes,
            'peak_predicted': self.peak_predicted,
            'avg_predicted': self.avg_predicted,
            'overload_expected': self.overload_expected,
            'mae': self.mae,
            'rmse': self.rmse,
            'payload': json.loads(self.payload_json or '{}'),
            'generated_at': self.generated_at.isoformat() if self.generated_at else None,
        }

    def to_dict(self):
        return {
            'doctor_id': self.doctor_id,
            'timestamp': self.timestamp.isoformat(),
            'score': self.score,
            'queue_length': self.queue_length,
        }


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)
    user_id     = db.Column(db.Integer, nullable=True)
    action      = db.Column(db.String(128))
    entity_type = db.Column(db.String(64))
    entity_id   = db.Column(db.Integer, nullable=True)
    details     = db.Column(db.Text, default='{}')
    ip_address  = db.Column(db.String(64), default='')
    previous_hash = db.Column(db.String(64), default='')
    entry_hash = db.Column(db.String(64), default='', nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'action': self.action,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'details': json.loads(self.details) if self.details else {},
            'previous_hash': self.previous_hash,
            'entry_hash': self.entry_hash,
        }


@event.listens_for(AuditLog, 'before_update')
def _prevent_audit_update(mapper, connection, target):
    raise ValueError('Audit logs are append-only and cannot be updated')


@event.listens_for(AuditLog, 'before_delete')
def _prevent_audit_delete(mapper, connection, target):
    raise ValueError('Audit logs are append-only and cannot be deleted')


class Notification(db.Model):
    __tablename__ = 'notifications'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    message     = db.Column(db.Text)
    type        = db.Column(db.String(32), default='info')   # info|warning|alert
    is_read     = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'message': self.message,
            'type': self.type, 'is_read': self.is_read,
            'created_at': self.created_at.isoformat()
        }


class Department(db.Model):
    __tablename__ = 'departments'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(128), unique=True, nullable=False)
    phone_number  = db.Column(db.String(32), default='')
    email         = db.Column(db.String(128), default='')
    description   = db.Column(db.Text, default='')
    head_doctor_id = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'phone_number': self.phone_number,
            'email': self.email,
            'description': self.description,
            'head_id': self.head_doctor_id or '',
        }


class DoctorProfileCompat(db.Model):
    __tablename__ = 'doctor_profile_compat'
    id                 = db.Column(db.Integer, primary_key=True)
    doctor_id          = db.Column(db.Integer, db.ForeignKey('doctors.id'), unique=True, nullable=False)
    phone              = db.Column(db.String(32), default='')
    experience         = db.Column(db.Integer, default=0)
    dr_consultation_fee = db.Column(db.Integer, default=500)
    is_blacklisted     = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'doctor_id': self.doctor_id,
            'phone': self.phone,
            'experience': self.experience,
            'dr_consultation_fee': self.dr_consultation_fee,
            'is_blacklisted': self.is_blacklisted,
        }


class PatientProfileCompat(db.Model):
    __tablename__ = 'patient_profile_compat'
    id              = db.Column(db.Integer, primary_key=True)
    patient_id       = db.Column(db.Integer, db.ForeignKey('patients.id'), unique=True, nullable=False)
    gender          = db.Column(db.String(16), default='')
    address         = db.Column(db.String(255), default='')
    city            = db.Column(db.String(64), default='')
    state           = db.Column(db.String(64), default='')
    zipcode         = db.Column(db.String(16), default='')
    blood_type      = db.Column(db.String(8), default='')
    allergies       = db.Column(db.Text, default='')
    medical_summary = db.Column(db.Text, default='')

    def to_dict(self):
        return {
            'patient_id': self.patient_id,
            'gender': self.gender,
            'address': self.address,
            'city': self.city,
            'state': self.state,
            'zipcode': self.zipcode,
            'blood_type': self.blood_type,
            'allergies': self.allergies,
            'medical_summary': self.medical_summary,
        }


class DoctorAvailabilityCompat(db.Model):
    __tablename__ = 'doctor_availability_compat'
    __table_args__ = (
        UniqueConstraint('doctor_id', 'day_of_week', name='uq_doctor_day_availability'),
    )
    id            = db.Column(db.Integer, primary_key=True)
    doctor_id      = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    day_of_week   = db.Column(db.String(16), nullable=False)
    start_time    = db.Column(db.String(8), default='09:00')
    end_time      = db.Column(db.String(8), default='17:00')
    slot_duration = db.Column(db.Integer, default=30)
    break_start   = db.Column(db.String(8), default='')
    break_end     = db.Column(db.String(8), default='')

    def to_dict(self):
        return {
            'doctor_id': self.doctor_id,
            'day_of_week': self.day_of_week,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'slot_duration': self.slot_duration,
            'break_start': self.break_start,
            'break_end': self.break_end,
        }


class AppointmentDiagnosisCompat(db.Model):
    __tablename__ = 'appointment_diagnosis_compat'
    id            = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointments.id'), unique=True, nullable=False)
    diagnosis     = db.Column(db.Text, default='')
    symptoms      = db.Column(db.Text, default='')
    severity      = db.Column(db.String(32), default='')
    treatment_plan = db.Column(db.Text, default='')
    follow_up     = db.Column(db.String(32), default='no')
    notes         = db.Column(db.Text, default='')
    prescription_json = db.Column(db.Text, default='[]')
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'appointment_id': self.appointment_id,
            'diagnosis': self.diagnosis,
            'symptoms': self.symptoms,
            'severity': self.severity,
            'treatment_plan': self.treatment_plan,
            'follow_up': self.follow_up,
            'notes': self.notes,
            'prescription': json.loads(self.prescription_json or '[]'),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class PaymentOrderCompat(db.Model):
    __tablename__ = 'payment_order_compat'
    id            = db.Column(db.Integer, primary_key=True)
    order_id      = db.Column(db.String(64), unique=True, nullable=False)
    patient_id     = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    doctor_id      = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    appointment_date = db.Column(db.String(16), nullable=False)
    appointment_time = db.Column(db.String(8), nullable=False)
    reason        = db.Column(db.Text, default='')
    amount_cents  = db.Column(db.Integer, default=0)
    status        = db.Column(db.String(16), default='created')
    failure_reason = db.Column(db.String(255), default='')
    razorpay_payment_id = db.Column(db.String(64), default='')
    verification_attempts = db.Column(db.Integer, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'order_id': self.order_id,
            'patient_id': self.patient_id,
            'doctor_id': self.doctor_id,
            'appointment_date': self.appointment_date,
            'appointment_time': self.appointment_time,
            'reason': self.reason,
            'amount_cents': self.amount_cents,
            'status': self.status,
            'failure_reason': self.failure_reason,
            'razorpay_payment_id': self.razorpay_payment_id,
            'verification_attempts': self.verification_attempts,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AuthSession(db.Model):
    __tablename__ = 'auth_sessions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    refresh_token_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(64), default='')
    user_agent = db.Column(db.String(255), default='')

    def is_active(self):
        return self.revoked_at is None and self.expires_at > datetime.utcnow()


class SecurityEvent(db.Model):
    __tablename__ = 'security_events'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    event_type = db.Column(db.String(64), nullable=False)
    severity = db.Column(db.String(16), default='medium')
    user_id = db.Column(db.Integer, nullable=True)
    source_ip = db.Column(db.String(64), default='')
    request_path = db.Column(db.String(255), default='')
    details_json = db.Column(db.Text, default='{}')

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'event_type': self.event_type,
            'severity': self.severity,
            'user_id': self.user_id,
            'source_ip': self.source_ip,
            'request_path': self.request_path,
            'details': json.loads(self.details_json or '{}'),
        }


class AsyncTaskEvent(db.Model):
    __tablename__ = 'async_task_events'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    task_name = db.Column(db.String(128), nullable=False)
    status = db.Column(db.String(32), nullable=False)  # started|success|retry|error|skipped
    retry_count = db.Column(db.Integer, default=0)
    details_json = db.Column(db.Text, default='{}')

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'task_name': self.task_name,
            'status': self.status,
            'retry_count': self.retry_count,
            'details': json.loads(self.details_json or '{}'),
        }
