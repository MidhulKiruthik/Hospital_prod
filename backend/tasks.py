"""
Celery Background Tasks
========================
Async workers for:
  - Clinical summary generation (NLP pipeline)
  - Workload forecast computation
  - Notification dispatch
  - Periodic metric snapshots
  - Backup simulation
"""

from celery import Celery
from celery.schedules import crontab
import os
import time
import json
from datetime import datetime, date
from urllib.parse import urlparse
from security_utils import encrypt_text

# ── Celery app factory ────────────────────────────────────────────────────────

def make_celery(app=None):
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    celery = Celery(
        'hospital_ops',
        broker=REDIS_URL,
        backend=REDIS_URL,
        include=['tasks']
    )
    celery.conf.update(
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        beat_schedule={
            'snapshot-workloads-every-30s': {
                'task': 'tasks.snapshot_workload_metrics',
                'schedule': 30.0,
            },
            'update-forecasts-every-60s': {
                'task': 'tasks.update_all_forecasts',
                'schedule': 60.0,
            },
            'backup-every-15-minutes': {
                'task': 'tasks.run_backup',
                'schedule': crontab(minute='*/15'),
            },
        }
    )
    if app:
        # Only map the Celery-specific values we need from Flask config.
        # Importing the full app config introduces legacy uppercase keys
        # (e.g. CELERY_RESULT_BACKEND) that Celery 5 rejects when mixed
        # with new-style keys.
        celery.conf.update(
            broker_url=app.config.get('CELERY_BROKER_URL', REDIS_URL),
            result_backend=app.config.get('CELERY_RESULT_BACKEND', REDIS_URL),
        )
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        celery.Task = ContextTask
    return celery


# ── Standalone celery instance (used by worker) ───────────────────────────────
celery = make_celery()


# ── Task: Generate Clinical Summary ──────────────────────────────────────────

@celery.task(bind=True, max_retries=3, default_retry_delay=5,
             name='tasks.generate_clinical_summary')
def generate_clinical_summary_task(self, appointment_id: int, notes: str):
    """
    Async NLP task: generate clinical summary for a completed appointment.
    Triggered OUTSIDE the API critical path after appointment completion.
    """
    try:
        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import db, Appointment, ClinicalSummary, Doctor, Patient
            try:
                from nlp import generate_clinical_summary
            except ModuleNotFoundError:
                import sys
                base_dir = os.path.dirname(os.path.abspath(__file__))
                if base_dir not in sys.path:
                    sys.path.append(base_dir)
                from nlp import generate_clinical_summary

            appt = Appointment.query.get(appointment_id)
            if not appt:
                return {'status': 'error', 'message': 'Appointment not found'}

            # Build context for richer summary
            appointment_data = {
                'patient_name': appt.patient.name if appt.patient else 'Unknown',
                'doctor_name':  appt.doctor.name if appt.doctor else 'Unknown',
                'specialty':    appt.doctor.specialty if appt.doctor else 'General',
                'date':         appt.scheduled_at.strftime('%Y-%m-%d'),
            }

            result = generate_clinical_summary(notes, appointment_data)

            # Upsert summary record
            summary = ClinicalSummary.query.filter_by(
                appointment_id=appointment_id
            ).first()
            if not summary:
                summary = ClinicalSummary(appointment_id=appointment_id)
                db.session.add(summary)

            summary.summary_text    = encrypt_text(result['summary_text'])
            summary.chief_complaint = encrypt_text(result['chief_complaint'])
            summary.findings        = encrypt_text(result['findings'])
            summary.assessment      = encrypt_text(result['assessment'])
            summary.plan            = encrypt_text(result['plan'])
            summary.status          = result['status']
            summary.processing_time_s = result['processing_time_s']
            summary.generated_at    = datetime.utcnow()
            db.session.commit()

            # Send notification
            notify_summary_ready.delay(appointment_id)

            return {
                'status': 'ready',
                'appointment_id': appointment_id,
                'processing_time_s': result['processing_time_s']
            }

    except Exception as exc:
        raise self.retry(exc=exc)


# ── Task: Send Notification ───────────────────────────────────────────────────

@celery.task(name='tasks.send_notification')
def send_notification_task(user_id: int, message: str, notif_type: str = 'info'):
    """Async notification dispatch."""
    try:
        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import db, Notification
            notif = Notification(
                user_id=user_id,
                message=message,
                type=notif_type
            )
            db.session.add(notif)
            db.session.commit()
            return {'status': 'sent', 'user_id': user_id}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@celery.task(name='tasks.notify_summary_ready')
def notify_summary_ready(appointment_id: int):
    """Notify doctor and patient that clinical summary is ready."""
    try:
        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import db, Appointment, Notification
            appt = Appointment.query.get(appointment_id)
            if not appt:
                return
            # Notify doctor
            if appt.doctor and appt.doctor.user_id:
                n = Notification(
                    user_id=appt.doctor.user_id,
                    message=f"Clinical summary ready for appointment #{appointment_id} "
                            f"({appt.patient.name if appt.patient else 'Patient'})",
                    type='info'
                )
                db.session.add(n)
            db.session.commit()
    except Exception as e:
        pass


# ── Task: Snapshot Workload Metrics ──────────────────────────────────────────

@celery.task(name='tasks.snapshot_workload_metrics')
def snapshot_workload_metrics():
    """
    Periodic task (every 30s): snapshot workload scores for all active doctors.
    Feeds the forecasting engine's rolling window.
    """
    try:
        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import db, Doctor, WorkloadMetric
            from scheduler import compute_workload_score
            doctors = Doctor.query.filter_by(is_available=True).all()
            count = 0
            for doc in doctors:
                ws = compute_workload_score(doc.id)
                metric = WorkloadMetric(
                    doctor_id=doc.id,
                    score=ws['score'],
                    queue_length=ws.get('queue_now', 0),
                    completed_today=ws.get('completed_today', 0),
                    cancelled_today=ws.get('cancelled_today', 0)
                )
                db.session.add(metric)
                count += 1
            db.session.commit()
            return {'status': 'ok', 'snapshots': count, 'at': datetime.utcnow().isoformat()}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ── Task: Update All Forecasts ───────────────────────────────────────────────

@celery.task(name='tasks.update_all_forecasts')
def update_all_forecasts():
    """
    Periodic task (every 60s): recompute forecasts and cache in Redis.
    """
    try:
        import redis as redis_lib
        r = redis_lib.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import Doctor
            from forecaster import forecast_workload, forecast_patient_demand

            # Hospital-level demand forecast
            demand = forecast_patient_demand(horizon_hours=4)
            r.setex('forecast:demand', 120, json.dumps(demand))

            # Per-doctor workload forecast
            doctors = Doctor.query.filter_by(is_available=True).all()
            for doc in doctors:
                wf = forecast_workload(doc.id, horizon_minutes=120)
                r.setex(f'forecast:doctor:{doc.id}', 120, json.dumps(wf))

        return {'status': 'ok', 'doctors_updated': len(doctors)}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ── Task: Daily Backup Simulation ────────────────────────────────────────────

def _resolve_sqlite_path() -> str:
    db_url = os.environ.get('DATABASE_URL', 'sqlite:///hospital.db')
    parsed = urlparse(db_url)
    if parsed.scheme != 'sqlite':
        return ''

    # sqlite:////data/hospital.db -> /data/hospital.db
    if parsed.path:
        return parsed.path
    # sqlite:///hospital.db fallback
    return db_url.replace('sqlite:///', '', 1)


def _cleanup_old_backups(backup_dir: str, retention_hours: int) -> int:
    removed = 0
    if retention_hours <= 0:
        return removed

    cutoff = datetime.utcnow().timestamp() - (retention_hours * 3600)
    for name in os.listdir(backup_dir):
        if not name.endswith('.db'):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed

@celery.task(name='tasks.run_backup')
def run_backup():
    """Periodic backup task aligned to 15-minute RPO target."""
    try:
        import shutil

        src = _resolve_sqlite_path()
        backup_dir = os.environ.get('BACKUP_DIR', '/data/backups')
        retention_hours = int(os.environ.get('BACKUP_RETENTION_HOURS', '48'))

        if not src:
            return {'status': 'error', 'message': 'Backup currently supports SQLite only'}

        os.makedirs(backup_dir, exist_ok=True)
        backup_name = f"hospital_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"

        if os.path.exists(src):
            target = os.path.join(backup_dir, backup_name)
            shutil.copy2(src, target)
            removed = _cleanup_old_backups(backup_dir, retention_hours)
            return {
                'status': 'ok',
                'backup': backup_name,
                'source': src,
                'backup_dir': backup_dir,
                'retention_hours': retention_hours,
                'removed_old_backups': removed,
                'at': datetime.utcnow().isoformat(),
            }

        return {'status': 'error', 'message': f'Database source not found: {src}'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
