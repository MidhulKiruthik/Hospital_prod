"""
Smart Appointment Load Balancer
================================
Implements compute-driven scheduling using workload scoring.

Workload Score Formula:
  score = 0.40 * appointment_ratio
        + 0.30 * avg_consult_pressure
        + 0.20 * cancellation_penalty
        + 0.10 * queue_pressure

Score range: 0.0 (idle) → 1.0+ (overloaded)
Overload threshold: 0.85
"""

from datetime import datetime, timedelta, date
from models import Doctor, Appointment, WorkloadMetric, Notification, db
from sqlalchemy import func
import json


MAX_PER_DAY = 40
OVERLOAD_THRESHOLD = 0.85
EMERGENCY_MAX_OVERRIDE = True   # emergency always gets slot


def compute_workload_score(doctor_id: int, target_date: date = None) -> dict:
    """
    Compute composite workload score for a doctor on a given date.
    Returns dict with score and components.
    """
    if target_date is None:
        target_date = date.today()

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end   = datetime.combine(target_date, datetime.max.time())

    doctor = Doctor.query.get(doctor_id)
    if not doctor or not doctor.is_available:
        return {'score': 1.0, 'available': False, 'components': {}}

    max_appts = doctor.max_per_day or MAX_PER_DAY

    # Count today's appointments by status
    booked_count = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'booked'
    ).count()

    completed_count = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'completed'
    ).count()

    cancelled_count = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'cancelled'
    ).count()

    total_created = booked_count + completed_count + cancelled_count

    # Component 1: Appointment ratio (booked + completed vs max)
    active = booked_count + completed_count
    appointment_ratio = min(active / max_appts, 1.0)

    # Component 2: Consult pressure (how busy is current queue)
    # Queue = currently booked within next 2 hours
    now = datetime.utcnow()
    near_future = now + timedelta(hours=2)
    queue_now = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.scheduled_at.between(now, near_future),
        Appointment.status == 'booked'
    ).count()
    avg_consult_pressure = min(queue_now / 12, 1.0)   # 12 = 2hrs / 10min avg

    # Component 3: Cancellation penalty (high cancellation = scheduling instability)
    cancel_rate = (cancelled_count / max(total_created, 1))
    cancellation_penalty = min(cancel_rate * 2, 1.0)

    # Component 4: Queue pressure vs capacity
    queue_pressure = min(booked_count / max_appts, 1.0)

    score = (
        0.40 * appointment_ratio +
        0.30 * avg_consult_pressure +
        0.20 * cancellation_penalty +
        0.10 * queue_pressure
    )

    return {
        'score': round(score, 4),
        'available': True,
        'overloaded': score >= OVERLOAD_THRESHOLD,
        'booked_today': booked_count,
        'completed_today': completed_count,
        'cancelled_today': cancelled_count,
        'queue_now': queue_now,
        'max_per_day': max_appts,
        'components': {
            'appointment_ratio': round(appointment_ratio, 4),
            'consult_pressure': round(avg_consult_pressure, 4),
            'cancellation_penalty': round(cancellation_penalty, 4),
            'queue_pressure': round(queue_pressure, 4),
        }
    }


def find_best_doctor(specialty: str, scheduled_at: datetime,
                     priority: str = 'normal', exclude_doctor_id: int = None) -> dict:
    """
    Find the doctor with lowest workload score for a given specialty and time.
    Returns: {'doctor_id': int, 'score': float, 'reason': str}
    """
    doctors = Doctor.query.filter(
        Doctor.specialty == specialty,
        Doctor.is_available == True
    ).all()

    if exclude_doctor_id:
        doctors = [d for d in doctors if d.id != exclude_doctor_id]

    if not doctors:
        return {'doctor_id': None, 'score': None, 'reason': 'No available doctors in specialty'}

    scored = []
    target_date = scheduled_at.date()
    for doc in doctors:
        ws = compute_workload_score(doc.id, target_date)
        if not ws['available']:
            continue
        # Emergency always gets through even if overloaded
        if ws['overloaded'] and priority != 'emergency':
            continue
        scored.append({'doctor': doc, 'ws': ws})

    if not scored:
        # All overloaded: pick least loaded for emergency, else reject
        if priority == 'emergency':
            all_scored = []
            for doc in doctors:
                ws = compute_workload_score(doc.id, target_date)
                if ws['available']:
                    all_scored.append({'doctor': doc, 'ws': ws})
            if not all_scored:
                return {'doctor_id': None, 'score': None, 'reason': 'No available doctors'}
            best = min(all_scored, key=lambda x: x['ws']['score'])
            return {
                'doctor_id': best['doctor'].id,
                'doctor_name': best['doctor'].name,
                'score': best['ws']['score'],
                'reason': 'Emergency override — all doctors at capacity'
            }
        return {
            'doctor_id': None, 'score': None,
            'reason': 'All doctors at capacity. Please try a different time slot.'
        }

    best = min(scored, key=lambda x: x['ws']['score'])
    return {
        'doctor_id': best['doctor'].id,
        'doctor_name': best['doctor'].name,
        'score': best['ws']['score'],
        'workload_details': best['ws'],
        'reason': 'Optimal assignment by workload score'
    }


def book_appointment(patient_id: int, specialty: str, scheduled_at: datetime,
                     notes: str = '', priority: str = 'normal',
                     preferred_doctor_id: int = None) -> dict:
    """
    Full appointment booking flow with load-balancing.
    Steps:
      1. If preferred doctor given, check their score
      2. If overloaded, reassign to best alternative
      3. Create appointment, emit event, write audit
    Returns: {'appointment': Appointment, 'reassigned': bool, 'message': str}
    """
    result = {'reassigned': False, 'overload_triggered': False}
    doctor_id = preferred_doctor_id
    ws = None

    if preferred_doctor_id:
        ws = compute_workload_score(preferred_doctor_id, scheduled_at.date())
        if ws['overloaded'] and priority != 'emergency':
            # Trigger reassignment
            alt = find_best_doctor(specialty, scheduled_at, priority,
                                   exclude_doctor_id=preferred_doctor_id)
            if alt['doctor_id']:
                doctor_id = alt['doctor_id']
                result['reassigned'] = True
                result['overload_triggered'] = True
                result['original_doctor_id'] = preferred_doctor_id
                result['message'] = (
                    f"Preferred doctor at capacity (score={ws['score']:.2f}). "
                    f"Reassigned to {alt['doctor_name']}."
                )
            else:
                return {'error': alt['reason']}
    else:
        alt = find_best_doctor(specialty, scheduled_at, priority)
        if not alt['doctor_id']:
            return {'error': alt.get('reason', 'No available doctor')}
        doctor_id = alt['doctor_id']
        ws = alt.get('workload_details', {})

    # Create the appointment
    appt = Appointment(
        patient_id=patient_id,
        doctor_id=doctor_id,
        scheduled_at=scheduled_at,
        notes=notes,
        priority=priority,
        status='booked',
        workload_score_at_booking=ws.get('score', 0.0) if ws else 0.0
    )
    db.session.add(appt)
    db.session.flush()   # get ID before commit

    # Write workload metric snapshot
    if ws:
        metric = WorkloadMetric(
            doctor_id=doctor_id,
            score=ws.get('score', 0.0),
            queue_length=ws.get('queue_now', 0),
            completed_today=ws.get('completed_today', 0),
            cancelled_today=ws.get('cancelled_today', 0)
        )
        db.session.add(metric)

    db.session.commit()

    result['appointment'] = appt
    result['doctor_id'] = doctor_id
    if 'message' not in result:
        result['message'] = 'Appointment booked successfully.'

    return result


def get_all_workloads(target_date: date = None) -> list:
    """Get workload scores for all available doctors."""
    doctors = Doctor.query.filter_by(is_available=True).all()
    results = []
    for doc in doctors:
        ws = compute_workload_score(doc.id, target_date)
        results.append({
            'doctor_id': doc.id,
            'doctor_name': doc.name,
            'specialty': doc.specialty,
            **ws
        })
    return sorted(results, key=lambda x: x['score'], reverse=True)
