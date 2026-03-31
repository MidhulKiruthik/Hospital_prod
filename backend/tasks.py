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
import subprocess
import hashlib
from security_utils import encrypt_text


def _record_forecast_history_row(scope: str, scope_id, payload: dict):
    from models import db, ForecastHistory

    row = ForecastHistory(
        scope=scope,
        scope_id=scope_id,
        selected_model=str(payload.get('selected_model', 'baseline')),
        effective_model=str(payload.get('effective_model', '')),
        horizon_minutes=int(payload.get('horizon_minutes', 120)),
        peak_predicted=float(payload.get('peak_predicted', 0.0)),
        avg_predicted=float(payload.get('avg_predicted', 0.0)),
        overload_expected=bool(payload.get('overload_expected', False)),
        payload_json=json.dumps(payload, sort_keys=True),
    )
    db.session.add(row)


def _log_task_event(task_name: str, status: str, details: dict = None, retry_count: int = 0):
    try:
        from models import db, AsyncTaskEvent

        row = AsyncTaskEvent(
            task_name=task_name,
            status=status,
            retry_count=retry_count,
            details_json=json.dumps(details or {}, sort_keys=True),
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        return

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
        _log_task_event('generate_clinical_summary', 'started', {'appointment_id': appointment_id}, self.request.retries)
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
            summary.generation_method = result.get('method', 'rule-based-nlp')
            summary.generation_model = result.get('model_name', '')
            summary.processing_time_s = result['processing_time_s']
            summary.source_notes_hash = hashlib.sha256((notes or '').encode('utf-8')).hexdigest()
            summary.is_reviewed = False
            summary.reviewed_by_user_id = None
            summary.reviewed_at = None
            summary.review_notes = ''
            summary.generated_at    = datetime.utcnow()
            db.session.commit()

            try:
                from app import NLP_DURATION_SECONDS
                NLP_DURATION_SECONDS.labels(mode='async').observe(max(float(result['processing_time_s']), 0.0))
            except Exception:
                pass

            # Send notification
            notify_summary_ready.delay(appointment_id)

            _log_task_event(
                'generate_clinical_summary',
                'success',
                {'appointment_id': appointment_id, 'processing_time_s': result['processing_time_s']},
                self.request.retries,
            )

            return {
                'status': 'ready',
                'appointment_id': appointment_id,
                'processing_time_s': result['processing_time_s']
            }

    except Exception as exc:
        _log_task_event(
            'generate_clinical_summary',
            'retry',
            {'appointment_id': appointment_id, 'error': str(exc)},
            self.request.retries,
        )
        raise self.retry(exc=exc)


# ── Task: Send Notification ───────────────────────────────────────────────────

@celery.task(name='tasks.send_notification')
def send_notification_task(user_id: int, message: str, notif_type: str = 'info'):
    """Async notification dispatch."""
    try:
        _log_task_event('send_notification', 'started', {'user_id': user_id})
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
            _log_task_event('send_notification', 'success', {'user_id': user_id})
            return {'status': 'sent', 'user_id': user_id}
    except Exception as e:
        _log_task_event('send_notification', 'error', {'user_id': user_id, 'error': str(e)})
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
        _log_task_event('snapshot_workload_metrics', 'started')
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
            _log_task_event('snapshot_workload_metrics', 'success', {'snapshots': count})
            return {'status': 'ok', 'snapshots': count, 'at': datetime.utcnow().isoformat()}
    except Exception as e:
        _log_task_event('snapshot_workload_metrics', 'error', {'error': str(e)})
        return {'status': 'error', 'message': str(e)}


# ── Task: Update All Forecasts ───────────────────────────────────────────────

@celery.task(name='tasks.update_all_forecasts')
def update_all_forecasts():
    """
    Periodic task (every 60s): recompute forecasts and cache in Redis.
    """
    try:
        _log_task_event('update_all_forecasts', 'started')
        import redis as redis_lib
        r = redis_lib.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

        from app import create_app
        flask_app = create_app()
        with flask_app.app_context():
            from models import db, Doctor
            from forecaster import forecast_workload, forecast_patient_demand

            # Hospital-level demand forecast
            demand = forecast_patient_demand(horizon_hours=4)
            r.setex('forecast:demand', 120, json.dumps(demand))
            hospital_forecast = forecast_workload(doctor_id=None, horizon_minutes=120)
            r.setex('forecast:hospital', 120, json.dumps(hospital_forecast))
            _record_forecast_history_row('hospital', None, hospital_forecast)

            # Per-doctor workload forecast
            doctors = Doctor.query.filter_by(is_available=True).all()
            for doc in doctors:
                wf = forecast_workload(doc.id, horizon_minutes=120)
                r.setex(f'forecast:doctor:{doc.id}', 120, json.dumps(wf))
                _record_forecast_history_row('doctor', doc.id, wf)

            db.session.commit()

        _log_task_event('update_all_forecasts', 'success', {'doctors_updated': len(doctors)})
        return {'status': 'ok', 'doctors_updated': len(doctors)}
    except Exception as e:
        _log_task_event('update_all_forecasts', 'error', {'error': str(e)})
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


def _cleanup_old_backups(backup_dir: str, retention_hours: int, extension: str) -> int:
    removed = 0
    if retention_hours <= 0:
        return removed

    cutoff = datetime.utcnow().timestamp() - (retention_hours * 3600)
    for name in os.listdir(backup_dir):
        if not name.endswith(extension):
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
        _log_task_event('run_backup', 'started')
        import shutil
        import hashlib

        db_url = os.environ.get('DATABASE_URL', 'sqlite:///hospital.db').strip()
        parsed = urlparse(db_url)
        src = _resolve_sqlite_path()
        backup_dir = os.environ.get('BACKUP_DIR', '/data/backups')
        retention_hours = int(os.environ.get('BACKUP_RETENTION_HOURS', '48'))

        os.makedirs(backup_dir, exist_ok=True)

        if parsed.scheme == 'sqlite':
            backup_name = f"hospital_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
            if not src:
                return {'status': 'error', 'message': 'Unable to resolve SQLite path'}
            if not os.path.exists(src):
                return {'status': 'error', 'message': f'Database source not found: {src}'}

            target = os.path.join(backup_dir, backup_name)
            shutil.copy2(src, target)

            digest = hashlib.sha256()
            with open(target, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)

            removed = _cleanup_old_backups(backup_dir, retention_hours, '.db')
            _log_task_event('run_backup', 'success', {'backup': backup_name, 'source': src})
            return {
                'status': 'ok',
                'backup': backup_name,
                'sha256': digest.hexdigest(),
                'source': src,
                'backup_dir': backup_dir,
                'retention_hours': retention_hours,
                'removed_old_backups': removed,
                'at': datetime.utcnow().isoformat(),
            }

        if parsed.scheme.startswith('postgres'):
            backup_name = f"hospital_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.sql"
            target = os.path.join(backup_dir, backup_name)
            command = [
                'pg_dump',
                db_url,
                '--no-owner',
                '--no-privileges',
                '--format=plain',
                '--file',
                target,
            ]
            try:
                subprocess.run(command, check=True, capture_output=True)
            except FileNotFoundError:
                _log_task_event('run_backup', 'error', {'error': 'pg_dump not found'})
                return {
                    'status': 'error',
                    'message': 'pg_dump not found. Install PostgreSQL client tools in worker image.',
                }
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b'').decode('utf-8', errors='ignore')
                _log_task_event('run_backup', 'error', {'error': stderr.strip() or str(exc)})
                return {'status': 'error', 'message': f'pg_dump failed: {stderr.strip() or exc}'}

            digest = hashlib.sha256()
            with open(target, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)

            removed = _cleanup_old_backups(backup_dir, retention_hours, '.sql')
            _log_task_event('run_backup', 'success', {'backup': backup_name, 'source': 'postgres'})
            return {
                'status': 'ok',
                'backup': backup_name,
                'sha256': digest.hexdigest(),
                'source': 'postgres',
                'backup_dir': backup_dir,
                'retention_hours': retention_hours,
                'removed_old_backups': removed,
                'at': datetime.utcnow().isoformat(),
            }

        _log_task_event('run_backup', 'skipped', {'reason': f'Unsupported scheme {parsed.scheme}'})
        return {'status': 'error', 'message': f'Unsupported DATABASE_URL scheme: {parsed.scheme}'}
    except Exception as e:
        _log_task_event('run_backup', 'error', {'error': str(e)})
        return {'status': 'error', 'message': str(e)}
