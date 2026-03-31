import base64
import os


def _as_bool(name: str, default: str = 'False') -> bool:
    return os.environ.get(name, default).lower() in ('true', '1', 'yes', 'on')


def normalize_database_url(raw_url: str) -> str:
    value = (raw_url or '').strip()
    if not value:
        return 'sqlite:///hospital.db'
    if value.startswith('postgres://'):
        value = f"postgresql://{value[len('postgres://') :]}"
    if value.startswith('postgresql://'):
        value = f"postgresql+psycopg://{value[len('postgresql://') :]}"
    return value


def _is_placeholder(value: str) -> bool:
    candidate = (value or '').strip().lower()
    if not candidate:
        return True
    markers = (
        'replace-with',
        'change-me',
        'example',
        'placeholder',
        'dev-only',
    )
    return any(marker in candidate for marker in markers)


def _is_strong_secret(value: str) -> bool:
    return bool(value and len(value) >= 32 and not _is_placeholder(value))


def validate_runtime_config(config: dict) -> None:
    env_name = str(config.get('APP_ENV', 'development')).lower()
    if env_name not in ('production', 'staging'):
        return

    errors = []

    if not _is_strong_secret(config.get('SECRET_KEY', '')):
        errors.append('SECRET_KEY must be at least 32 chars and must not use placeholders')
    if not _is_strong_secret(config.get('JWT_SECRET_KEY', '')):
        errors.append('JWT_SECRET_KEY must be at least 32 chars and must not use placeholders')

    encryption_key = (config.get('DATA_ENCRYPTION_KEY') or '').strip()
    try:
        decoded = base64.b64decode(encryption_key)
        if len(decoded) != 32:
            errors.append('DATA_ENCRYPTION_KEY must decode to exactly 32 bytes (base64)')
    except Exception:
        errors.append('DATA_ENCRYPTION_KEY must be a valid base64-encoded key')

    db_url = str(config.get('SQLALCHEMY_DATABASE_URI', ''))
    if db_url.startswith('sqlite'):
        errors.append('Production APP_ENV requires PostgreSQL DATABASE_URL, not SQLite')

    cors_origins = str(config.get('CORS_ORIGINS', ''))
    if '*' in cors_origins:
        errors.append('CORS_ORIGINS cannot contain wildcard in production')
    if 'localhost' in cors_origins or '127.0.0.1' in cors_origins:
        errors.append('CORS_ORIGINS cannot include localhost/127.0.0.1 in production')

    if not config.get('ENFORCE_HTTPS', False):
        errors.append('ENFORCE_HTTPS must be True in production')

    prod_cors = str(config.get('CORS_ORIGINS_PRODUCTION', '')).strip()
    if not prod_cors:
        errors.append('CORS_ORIGINS_PRODUCTION must include at least one real origin in production')

    if errors:
        raise RuntimeError('Invalid production configuration: ' + '; '.join(errors))


def normalize_cors_origins(raw_value: str) -> str:
    value = (raw_value or '').strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split(',') if part.strip()]
    normalized = []
    seen = set()
    for part in parts:
        lowered = part.lower()
        if lowered in ('http://localhost:8080', 'https://localhost:8443', 'http://127.0.0.1', 'https://127.0.0.1'):
            continue
        if part not in seen:
            normalized.append(part)
            seen.add(part)
    return ','.join(normalized)


class Config:
    # Core
    APP_ENV = os.environ.get('APP_ENV', 'development').strip().lower()
    SECRET_KEY = os.environ.get('SECRET_KEY', '').strip()
    DEBUG = _as_bool('DEBUG', 'False')

    # Database
    DATABASE_URL = normalize_database_url(os.environ.get('DATABASE_URL', 'sqlite:///hospital.db'))
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }
    AUTO_RUN_MIGRATIONS = _as_bool('AUTO_RUN_MIGRATIONS', 'False')
    AUTO_CREATE_SCHEMA = _as_bool('AUTO_CREATE_SCHEMA', 'True')

    # JWT
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', '').strip()
    JWT_EXPIRATION_HOURS = 24
    REFRESH_TOKEN_EXPIRATION_DAYS = int(os.environ.get('REFRESH_TOKEN_EXPIRATION_DAYS', '7'))
    DATA_ENCRYPTION_KEY = os.environ.get('DATA_ENCRYPTION_KEY', '').strip()
    RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '').strip()
    RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '').strip()
    RAZORPAY_SIGNATURE_REQUIRED = _as_bool('RAZORPAY_SIGNATURE_REQUIRED', 'True')

    # API security and runtime hardening
    CORS_ORIGINS = os.environ.get(
        'CORS_ORIGINS',
        'http://localhost:8080,https://localhost:8443',
    )
    CORS_ORIGINS_PRODUCTION = normalize_cors_origins(
        os.environ.get('CORS_ORIGINS_PRODUCTION', CORS_ORIGINS)
    )
    AUTH_RATE_LIMIT = os.environ.get('AUTH_RATE_LIMIT', '5 per minute')
    DEFAULT_RATE_LIMIT = os.environ.get('DEFAULT_RATE_LIMIT', '300 per hour')
    ENFORCE_HTTPS = _as_bool('ENFORCE_HTTPS', 'False')
    ENABLE_PROMETHEUS_METRICS = _as_bool('ENABLE_PROMETHEUS_METRICS', 'True')
    ALLOW_PRIVILEGED_SELF_REGISTRATION = _as_bool(
        'ALLOW_PRIVILEGED_SELF_REGISTRATION',
        'False',
    )
    LOGIN_MAX_FAILED_ATTEMPTS = int(os.environ.get('LOGIN_MAX_FAILED_ATTEMPTS', '5'))
    LOGIN_LOCKOUT_MINUTES = int(os.environ.get('LOGIN_LOCKOUT_MINUTES', '15'))
    SECURITY_EVENT_WEBHOOK_URL = os.environ.get('SECURITY_EVENT_WEBHOOK_URL', '').strip()
    SECURITY_EVENT_WEBHOOK_TIMEOUT_SECONDS = int(
        os.environ.get('SECURITY_EVENT_WEBHOOK_TIMEOUT_SECONDS', '3')
    )
    SECURITY_EVENT_WEBHOOK_TOKEN = os.environ.get('SECURITY_EVENT_WEBHOOK_TOKEN', '').strip()
    SECURITY_EVENT_EXPORT_ENABLED = _as_bool('SECURITY_EVENT_EXPORT_ENABLED', 'False')
    SEED_DEMO_DATA = _as_bool('SEED_DEMO_DATA', 'False')
    BOOTSTRAP_ADMIN_USERNAME = os.environ.get('BOOTSTRAP_ADMIN_USERNAME', '').strip()
    BOOTSTRAP_ADMIN_PASSWORD = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD', '').strip()
    BOOTSTRAP_ADMIN_EMAIL = os.environ.get('BOOTSTRAP_ADMIN_EMAIL', 'admin@hospital.local').strip()

    # Redis / Celery
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    RATE_LIMIT_STORAGE_URI = os.environ.get('RATE_LIMIT_STORAGE_URI', REDIS_URL)
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL

    # Model selection
    FORECAST_MODEL = os.environ.get('FORECAST_MODEL', 'baseline').strip().lower()
    ENABLE_TRANSFORMER_SUMMARIZATION = _as_bool('ENABLE_TRANSFORMER_SUMMARIZATION', 'False')
    TRANSFORMER_MODEL_NAME = os.environ.get(
        'TRANSFORMER_MODEL_NAME',
        'sshleifer/distilbart-cnn-12-6',
    ).strip()

    # Scheduling
    MAX_APPOINTMENTS_PER_DOCTOR_PER_DAY = 40
    OVERLOAD_THRESHOLD = 0.85
    FORECAST_UPDATE_INTERVAL = 30
    FORECAST_WINDOW_MINUTES = 60

    # Backup / DR
    BACKUP_INTERVAL_MINUTES = int(os.environ.get('BACKUP_INTERVAL_MINUTES', '15'))
    BACKUP_RETENTION_HOURS = int(os.environ.get('BACKUP_RETENTION_HOURS', '48'))
    BACKUP_DIR = os.environ.get('BACKUP_DIR', '/data/backups')
    BACKUP_HEALTH_MAX_AGE_MINUTES = int(os.environ.get('BACKUP_HEALTH_MAX_AGE_MINUTES', '30'))
    BACKUP_HEALTH_CHECK_ENABLED = _as_bool('BACKUP_HEALTH_CHECK_ENABLED', 'True')

    # Simulation defaults
    AVG_CONSULT_MINUTES = 10
    DAILY_PATIENT_TARGET = 500
