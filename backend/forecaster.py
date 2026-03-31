"""
Real-Time Workload Forecasting Engine
======================================
Implements short-horizon demand forecasting with 30-60s update cadence.

Methods:
  - Rolling Moving Average (baseline)
  - Exponential Weighted Moving Average (EWMA)
  - Simple Linear Regression (trend detection)
  - Poisson arrival model for patient demand

Forecast horizon: 1-4 hours ahead, updated every 30s.
"""

import math
import statistics
from datetime import datetime, timedelta, date
from models import Appointment, WorkloadMetric, Doctor, db
from collections import defaultdict


def _linear_regression(x: list, y: list):
    """Simple OLS linear regression. Returns (slope, intercept)."""
    n = len(x)
    if n < 2:
        return 0.0, (y[0] if y else 0.0)
    sum_x  = sum(x)
    sum_y  = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    denom  = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.0, sum_y / n
    slope     = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _ewma(values: list, alpha: float = 0.3) -> list:
    """Exponentially weighted moving average."""
    if not values:
        return []
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def _poisson_forecast(historical_rates: list, horizon_minutes: int) -> float:
    """
    Use historical arrival rates (patients/hour) to forecast
    expected arrivals over horizon using Poisson model.
    """
    if not historical_rates:
        return 0.0
    avg_rate = statistics.mean(historical_rates)
    # Expected arrivals = rate * time
    return avg_rate * (horizon_minutes / 60.0)


def get_hourly_arrival_rates(target_date: date = None, lookback_days: int = 7) -> dict:
    """
    Compute historical patient arrival rates per hour of day.
    Returns {hour: avg_arrivals_per_hour}
    """
    if target_date is None:
        target_date = date.today()

    start = datetime.combine(target_date - timedelta(days=lookback_days), datetime.min.time())
    end   = datetime.combine(target_date, datetime.max.time())

    appointments = Appointment.query.filter(
        Appointment.scheduled_at.between(start, end)
    ).all()

    # Group by hour of day
    by_hour = defaultdict(list)
    day_counts = defaultdict(set)

    for appt in appointments:
        hour = appt.scheduled_at.hour
        day  = appt.scheduled_at.date()
        day_counts[hour].add(day)
        by_hour[hour].append(1)

    rates = {}
    for hour in range(24):
        total = len(by_hour[hour])
        days  = len(day_counts[hour]) or 1
        rates[hour] = round(total / days, 2)

    return rates


def forecast_workload(doctor_id: int = None,
                      horizon_minutes: int = 120,
                      interval_minutes: int = 30) -> dict:
    """
    Forecast workload score for the next `horizon_minutes`,
    returning predicted scores at each `interval_minutes` step.

    If doctor_id is None, returns aggregate hospital-level forecast.
    """
    now = datetime.utcnow()
    steps = horizon_minutes // interval_minutes
    forecast_points = []

    # Get recent workload metrics (rolling window = last 60 min)
    window_start = now - timedelta(minutes=60)

    query = WorkloadMetric.query.filter(
        WorkloadMetric.timestamp >= window_start
    )
    if doctor_id:
        query = query.filter(WorkloadMetric.doctor_id == doctor_id)

    metrics = query.order_by(WorkloadMetric.timestamp.asc()).all()

    if len(metrics) < 2:
        # Insufficient history — use static estimate
        base_score = 0.4
        for i in range(1, steps + 1):
            t = now + timedelta(minutes=i * interval_minutes)
            forecast_points.append({
                'timestamp': t.isoformat(),
                'predicted_score': base_score,
                'confidence': 'low',
                'method': 'default'
            })
        return {
            'doctor_id': doctor_id,
            'horizon_minutes': horizon_minutes,
            'generated_at': now.isoformat(),
            'forecast': forecast_points,
            'note': 'Insufficient history — using default estimate'
        }

    # Extract time series
    t0   = metrics[0].timestamp
    xs   = [(m.timestamp - t0).total_seconds() / 60 for m in metrics]
    ys   = [m.score for m in metrics]

    # EWMA smoothing
    ys_smooth = _ewma(ys, alpha=0.35)

    # Linear regression on smoothed values
    slope, intercept = _linear_regression(xs, ys_smooth)

    # Hourly arrival rates for Poisson adjustment
    arrival_rates = get_hourly_arrival_rates()

    last_x = xs[-1]
    last_y = ys_smooth[-1]

    for i in range(1, steps + 1):
        future_x       = last_x + i * interval_minutes
        future_time    = now + timedelta(minutes=i * interval_minutes)
        future_hour    = future_time.hour

        # Trend prediction
        trend_score = intercept + slope * future_x

        # Poisson adjustment based on arrival rate at that hour
        arrival_rate   = arrival_rates.get(future_hour, 5.0)
        peak_hours     = {9, 10, 11, 14, 15, 16}
        arrival_factor = 1.2 if future_hour in peak_hours else 0.9
        adjusted_score = trend_score * arrival_factor

        # Clamp 0–1
        adjusted_score = max(0.0, min(1.0, adjusted_score))

        # Confidence degrades with distance
        confidence = 'high' if i == 1 else ('medium' if i <= 3 else 'low')

        forecast_points.append({
            'timestamp': future_time.isoformat(),
            'minute_offset': i * interval_minutes,
            'predicted_score': round(adjusted_score, 4),
            'trend_component': round(trend_score, 4),
            'arrival_rate': arrival_rate,
            'confidence': confidence,
            'overload_risk': adjusted_score >= 0.85,
            'method': 'ewma+linear+poisson'
        })

    # Summary stats
    scores = [fp['predicted_score'] for fp in forecast_points]
    return {
        'doctor_id': doctor_id,
        'horizon_minutes': horizon_minutes,
        'generated_at': now.isoformat(),
        'current_score': round(last_y, 4),
        'peak_predicted': round(max(scores), 4),
        'avg_predicted': round(statistics.mean(scores), 4),
        'overload_expected': any(s >= 0.85 for s in scores),
        'forecast': forecast_points
    }


def forecast_patient_demand(horizon_hours: int = 4) -> dict:
    """
    Forecast total patient arrivals for the hospital over the next N hours.
    Uses Poisson model with historical rates.
    """
    now = datetime.utcnow()
    arrival_rates = get_hourly_arrival_rates()
    demand_points = []
    total_expected = 0.0

    for h in range(1, horizon_hours + 1):
        future_time = now + timedelta(hours=h)
        hour        = future_time.hour
        rate        = arrival_rates.get(hour, 5.0)
        expected    = _poisson_forecast([rate], 60)
        total_expected += expected
        # Poisson std dev = sqrt(lambda)
        std_dev = math.sqrt(expected) if expected > 0 else 0

        demand_points.append({
            'hour': future_time.strftime('%H:00'),
            'timestamp': future_time.isoformat(),
            'expected_patients': round(expected, 1),
            'lower_bound': round(max(0, expected - std_dev), 1),
            'upper_bound': round(expected + std_dev, 1),
            'historical_rate': rate
        })

    return {
        'generated_at': now.isoformat(),
        'horizon_hours': horizon_hours,
        'total_expected': round(total_expected, 1),
        'demand_by_hour': demand_points
    }


def get_dashboard_metrics() -> dict:
    """Aggregate dashboard metrics for real-time display."""
    today = date.today()
    day_start = datetime.combine(today, datetime.min.time())
    day_end   = datetime.combine(today, datetime.max.time())

    total_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end)
    ).count()

    completed_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'completed'
    ).count()

    cancelled_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'cancelled'
    ).count()

    booked_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'booked'
    ).count()

    active_doctors = Doctor.query.filter_by(is_available=True).count()
    total_doctors  = Doctor.query.count()

    # Overloaded doctors right now
    from scheduler import compute_workload_score
    overloaded = 0
    all_doctors = Doctor.query.filter_by(is_available=True).all()
    workload_scores = []
    for doc in all_doctors:
        ws = compute_workload_score(doc.id, today)
        workload_scores.append(ws['score'])
        if ws['overloaded']:
            overloaded += 1

    avg_workload = round(statistics.mean(workload_scores), 3) if workload_scores else 0.0

    # Hourly trend (last 12 hours)
    hourly = []
    for h in range(12, 0, -1):
        t_start = datetime.utcnow() - timedelta(hours=h)
        t_end   = datetime.utcnow() - timedelta(hours=h - 1)
        count   = Appointment.query.filter(
            Appointment.created_at.between(t_start, t_end)
        ).count()
        hourly.append({'hour': t_start.strftime('%H:00'), 'count': count})

    return {
        'timestamp': datetime.utcnow().isoformat(),
        'appointments': {
            'total_today': total_today,
            'booked': booked_today,
            'completed': completed_today,
            'cancelled': cancelled_today,
            'completion_rate': round(completed_today / max(total_today, 1) * 100, 1)
        },
        'doctors': {
            'active': active_doctors,
            'total': total_doctors,
            'overloaded': overloaded,
            'avg_workload': avg_workload
        },
        'hourly_trend': hourly
    }
