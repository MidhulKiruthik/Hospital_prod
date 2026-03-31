"""
Real-Time Workload Forecasting Engine
=====================================
Supports a production-friendly baseline model and optional heavier models.
"""

import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

try:
    from sklearn.metrics import mean_absolute_error, mean_squared_error
except Exception:
    mean_absolute_error = None
    mean_squared_error = None

from flask import current_app, has_app_context

from models import Appointment, Doctor, WorkloadMetric


def _selected_forecast_model() -> str:
    if has_app_context():
        return current_app.config.get('FORECAST_MODEL', 'baseline')
    return os.environ.get('FORECAST_MODEL', 'baseline').strip().lower()


def _linear_regression(x: list, y: list):
    n = len(x)
    if n < 2:
        return 0.0, (y[0] if y else 0.0)
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    denom = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _ewma(values: list, alpha: float = 0.3) -> list:
    if not values:
        return []
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _poisson_forecast(historical_rates: list, horizon_minutes: int) -> float:
    if not historical_rates:
        return 0.0
    avg_rate = statistics.mean(historical_rates)
    return avg_rate * (horizon_minutes / 60.0)


def get_hourly_arrival_rates(target_date: date = None, lookback_days: int = 7) -> dict:
    if target_date is None:
        target_date = date.today()

    start = datetime.combine(target_date - timedelta(days=lookback_days), datetime.min.time())
    end = datetime.combine(target_date, datetime.max.time())

    appointments = Appointment.query.filter(
        Appointment.scheduled_at.between(start, end)
    ).all()

    by_hour = defaultdict(list)
    day_counts = defaultdict(set)
    for appt in appointments:
        hour = appt.scheduled_at.hour
        day_counts[hour].add(appt.scheduled_at.date())
        by_hour[hour].append(1)

    rates = {}
    for hour in range(24):
        total = len(by_hour[hour])
        days = len(day_counts[hour]) or 1
        rates[hour] = round(total / days, 2)
    return rates


def _build_metrics_series(doctor_id: int = None):
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=60)
    query = WorkloadMetric.query.filter(WorkloadMetric.timestamp >= window_start)
    if doctor_id:
        query = query.filter(WorkloadMetric.doctor_id == doctor_id)

    metrics = query.order_by(WorkloadMetric.timestamp.asc()).all()
    if len(metrics) < 2:
        return now, metrics, [], []

    t0 = metrics[0].timestamp
    xs = [(metric.timestamp - t0).total_seconds() / 60 for metric in metrics]
    ys = [metric.score for metric in metrics]
    return now, metrics, xs, ys


def _future_schedule(now: datetime, horizon_minutes: int, interval_minutes: int):
    steps = horizon_minutes // interval_minutes
    return [
        now + timedelta(minutes=index * interval_minutes)
        for index in range(1, steps + 1)
    ]


def _arrival_adjustment(arrival_rates: dict, future_time: datetime, raw_score: float) -> float:
    future_hour = future_time.hour
    arrival_rate = arrival_rates.get(future_hour, 5.0)
    peak_hours = {9, 10, 11, 14, 15, 16}
    arrival_factor = 1.2 if future_hour in peak_hours else 0.9
    adjusted = max(0.0, min(1.0, raw_score * arrival_factor))
    return adjusted, arrival_rate


def _confidence(step_index: int) -> str:
    if step_index == 1:
        return 'high'
    if step_index <= 3:
        return 'medium'
    return 'low'


def _baseline_forecast(now: datetime, xs: list, ys: list, horizon_minutes: int, interval_minutes: int) -> dict:
    forecast_points = []
    future_times = _future_schedule(now, horizon_minutes, interval_minutes)
    arrival_rates = get_hourly_arrival_rates()

    if len(ys) < 2:
        for index, future_time in enumerate(future_times, start=1):
            adjusted_score, arrival_rate = _arrival_adjustment(arrival_rates, future_time, 0.4)
            forecast_points.append({
                'timestamp': future_time.isoformat(),
                'minute_offset': index * interval_minutes,
                'predicted_score': round(adjusted_score, 4),
                'trend_component': 0.4,
                'arrival_rate': arrival_rate,
                'confidence': _confidence(index),
                'overload_risk': adjusted_score >= 0.85,
                'method': 'baseline-default',
            })
        return {
            'current_score': 0.4,
            'forecast': forecast_points,
            'method': 'baseline-default',
            'note': 'Insufficient history - using default estimate',
        }

    ys_smooth = _ewma(ys, alpha=0.35)
    slope, intercept = _linear_regression(xs, ys_smooth)
    last_x = xs[-1]
    last_y = ys_smooth[-1]

    for index, future_time in enumerate(future_times, start=1):
        future_x = last_x + index * interval_minutes
        trend_score = intercept + slope * future_x
        adjusted_score, arrival_rate = _arrival_adjustment(arrival_rates, future_time, trend_score)
        forecast_points.append({
            'timestamp': future_time.isoformat(),
            'minute_offset': index * interval_minutes,
            'predicted_score': round(adjusted_score, 4),
            'trend_component': round(trend_score, 4),
            'arrival_rate': arrival_rate,
            'confidence': _confidence(index),
            'overload_risk': adjusted_score >= 0.85,
            'method': 'ewma+linear+poisson',
        })

    return {
        'current_score': round(last_y, 4),
        'forecast': forecast_points,
        'method': 'ewma+linear+poisson',
    }


def _arima_forecast(now: datetime, ys: list, horizon_minutes: int, interval_minutes: int):
    try:
        from statsmodels.tsa.arima.model import ARIMA
    except Exception:
        return None, 'statsmodels not installed'

    if len(ys) < 6:
        return None, 'not enough history for ARIMA'

    steps = horizon_minutes // interval_minutes
    future_times = _future_schedule(now, horizon_minutes, interval_minutes)
    arrival_rates = get_hourly_arrival_rates()

    try:
        order = (1, 1, 1) if len(ys) >= 8 else (1, 0, 0)
        model = ARIMA(ys, order=order)
        fitted = model.fit()
        raw_predictions = fitted.forecast(steps=steps)
    except Exception as exc:
        return None, f'arima failed: {exc}'

    forecast_points = []
    current_score = ys[-1]
    for index, (future_time, raw_score) in enumerate(zip(future_times, raw_predictions), start=1):
        adjusted_score, arrival_rate = _arrival_adjustment(arrival_rates, future_time, float(raw_score))
        forecast_points.append({
            'timestamp': future_time.isoformat(),
            'minute_offset': index * interval_minutes,
            'predicted_score': round(adjusted_score, 4),
            'trend_component': round(float(raw_score), 4),
            'arrival_rate': arrival_rate,
            'confidence': _confidence(index),
            'overload_risk': adjusted_score >= 0.85,
            'method': 'arima+poisson',
        })

    return {
        'current_score': round(current_score, 4),
        'forecast': forecast_points,
        'method': 'arima+poisson',
    }, None


def _lightgbm_forecast(now: datetime, ys: list, horizon_minutes: int, interval_minutes: int):
    try:
        import lightgbm as lgb
    except Exception:
        return None, 'lightgbm not installed'

    if len(ys) < 8:
        return None, 'not enough history for LightGBM'

    lag = 3
    rows = []
    targets = []
    for idx in range(lag, len(ys)):
        rows.append([ys[idx - 3], ys[idx - 2], ys[idx - 1], idx])
        targets.append(ys[idx])
    if len(rows) < 4:
        return None, 'insufficient supervised rows for LightGBM'

    try:
        model = lgb.LGBMRegressor(
            n_estimators=60,
            learning_rate=0.08,
            max_depth=3,
            random_state=42,
            verbose=-1,
        )
        model.fit(rows, targets)
    except Exception as exc:
        return None, f'lightgbm failed: {exc}'

    steps = horizon_minutes // interval_minutes
    future_times = _future_schedule(now, horizon_minutes, interval_minutes)
    arrival_rates = get_hourly_arrival_rates()
    history = list(ys[-lag:])
    forecast_points = []

    for index, future_time in enumerate(future_times, start=1):
        features = [history[-3], history[-2], history[-1], len(ys) + index - 1]
        raw_score = float(model.predict([features])[0])
        history.append(raw_score)
        adjusted_score, arrival_rate = _arrival_adjustment(arrival_rates, future_time, raw_score)
        forecast_points.append({
            'timestamp': future_time.isoformat(),
            'minute_offset': index * interval_minutes,
            'predicted_score': round(adjusted_score, 4),
            'trend_component': round(raw_score, 4),
            'arrival_rate': arrival_rate,
            'confidence': _confidence(index),
            'overload_risk': adjusted_score >= 0.85,
            'method': 'lightgbm+poisson',
        })

    return {
        'current_score': round(ys[-1], 4),
        'forecast': forecast_points,
        'method': 'lightgbm+poisson',
    }, None


def forecast_workload(
    doctor_id: int = None,
    horizon_minutes: int = 120,
    interval_minutes: int = 30,
) -> dict:
    now, metrics, xs, ys = _build_metrics_series(doctor_id)
    selected_model = _selected_forecast_model()

    baseline = _baseline_forecast(now, xs, ys, horizon_minutes, interval_minutes)
    result = None
    fallback_reason = ''

    if selected_model == 'arima':
        result, fallback_reason = _arima_forecast(now, ys, horizon_minutes, interval_minutes)
    elif selected_model == 'lightgbm':
        result, fallback_reason = _lightgbm_forecast(now, ys, horizon_minutes, interval_minutes)
    elif selected_model in ('auto', 'advanced'):
        result, fallback_reason = _arima_forecast(now, ys, horizon_minutes, interval_minutes)
        if result is None:
            result, fallback_reason = _lightgbm_forecast(now, ys, horizon_minutes, interval_minutes)

    if result is None:
        result = baseline
        if selected_model not in ('baseline', '') and fallback_reason:
            result['fallback_reason'] = fallback_reason

    scores = [point['predicted_score'] for point in result['forecast']]

    mae = None
    rmse = None
    backtest_samples = []
    if mean_absolute_error and mean_squared_error and len(ys) >= 6:
        test_size = min(3, max(len(ys) // 3, 1))
        actual = ys[-test_size:]
        predicted = ys[-test_size - 1:-1] if len(ys) > test_size else ys[-test_size:]
        if len(predicted) == len(actual):
            mae = float(mean_absolute_error(actual, predicted))
            rmse = float(mean_squared_error(actual, predicted) ** 0.5)
            for index, (a, p) in enumerate(zip(actual, predicted), start=1):
                backtest_samples.append({
                    'index': index,
                    'actual_score': round(float(a), 4),
                    'predicted_score': round(float(p), 4),
                    'absolute_error': round(abs(float(a) - float(p)), 4),
                })

    model_comparison = {'baseline': baseline.get('method', 'ewma+linear+poisson')}
    if selected_model in ('arima', 'auto', 'advanced'):
        model_comparison['arima'] = 'available'
    if selected_model in ('lightgbm', 'auto', 'advanced'):
        model_comparison['lightgbm'] = 'available'

    return {
        'doctor_id': doctor_id,
        'horizon_minutes': horizon_minutes,
        'generated_at': now.isoformat(),
        'selected_model': selected_model,
        'effective_model': result.get('method', 'ewma+linear+poisson'),
        'current_score': round(result.get('current_score', 0.4), 4),
        'peak_predicted': round(max(scores), 4) if scores else 0.0,
        'avg_predicted': round(statistics.mean(scores), 4) if scores else 0.0,
        'overload_expected': any(score >= 0.85 for score in scores),
        'forecast': result['forecast'],
        'model_comparison': model_comparison,
        'mae': round(mae, 4) if mae is not None else None,
        'rmse': round(rmse, 4) if rmse is not None else None,
        'backtest': backtest_samples,
        **({'note': result['note']} if result.get('note') else {}),
        **({'fallback_reason': result['fallback_reason']} if result.get('fallback_reason') else {}),
    }


def forecast_patient_demand(horizon_hours: int = 4) -> dict:
    now = datetime.utcnow()
    arrival_rates = get_hourly_arrival_rates()
    demand_points = []
    total_expected = 0.0

    for hour_offset in range(1, horizon_hours + 1):
        future_time = now + timedelta(hours=hour_offset)
        rate = arrival_rates.get(future_time.hour, 5.0)
        expected = _poisson_forecast([rate], 60)
        total_expected += expected
        std_dev = math.sqrt(expected) if expected > 0 else 0

        demand_points.append({
            'hour': future_time.strftime('%H:00'),
            'timestamp': future_time.isoformat(),
            'expected_patients': round(expected, 1),
            'lower_bound': round(max(0, expected - std_dev), 1),
            'upper_bound': round(expected + std_dev, 1),
            'historical_rate': rate,
        })

    return {
        'generated_at': now.isoformat(),
        'horizon_hours': horizon_hours,
        'total_expected': round(total_expected, 1),
        'demand_by_hour': demand_points,
    }


def get_dashboard_metrics() -> dict:
    today = date.today()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = datetime.combine(today, datetime.max.time())

    total_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end)
    ).count()
    completed_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'completed',
    ).count()
    cancelled_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'cancelled',
    ).count()
    booked_today = Appointment.query.filter(
        Appointment.scheduled_at.between(day_start, day_end),
        Appointment.status == 'booked',
    ).count()

    active_doctors = Doctor.query.filter_by(is_available=True).count()
    total_doctors = Doctor.query.count()

    from scheduler import compute_workload_score

    overloaded = 0
    workload_scores = []
    for doctor in Doctor.query.filter_by(is_available=True).all():
        workload = compute_workload_score(doctor.id, today)
        workload_scores.append(workload['score'])
        if workload['overloaded']:
            overloaded += 1

    avg_workload = round(statistics.mean(workload_scores), 3) if workload_scores else 0.0

    hourly = []
    for hour_offset in range(12, 0, -1):
        t_start = datetime.utcnow() - timedelta(hours=hour_offset)
        t_end = datetime.utcnow() - timedelta(hours=hour_offset - 1)
        count = Appointment.query.filter(
            Appointment.created_at.between(t_start, t_end)
        ).count()
        hourly.append({'hour': t_start.strftime('%H:00'), 'count': count})

    return {
        'timestamp': datetime.utcnow().isoformat(),
        'forecast_model': _selected_forecast_model(),
        'appointments': {
            'total_today': total_today,
            'booked': booked_today,
            'completed': completed_today,
            'cancelled': cancelled_today,
            'completion_rate': round(completed_today / max(total_today, 1) * 100, 1),
        },
        'doctors': {
            'active': active_doctors,
            'total': total_doctors,
            'overloaded': overloaded,
            'avg_workload': avg_workload,
        },
        'hourly_trend': hourly,
    }
