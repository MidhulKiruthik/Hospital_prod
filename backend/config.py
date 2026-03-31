import os

class Config:
    # Core
    SECRET_KEY = os.environ.get('SECRET_KEY', 'hospital-ops-secret-key-2026')
    DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')

    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///hospital.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # JWT
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'jwt-hospital-secret-2026')
    JWT_EXPIRATION_HOURS = 24
    DATA_ENCRYPTION_KEY = os.environ.get('DATA_ENCRYPTION_KEY', '')
    RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', 'rzp_test_SXY57KDTSDiRGZ')
    RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', 'wh6TKP4Pe1Qi5Yj5mOKHx3ZD')
    RAZORPAY_SIGNATURE_REQUIRED = os.environ.get('RAZORPAY_SIGNATURE_REQUIRED', 'True').lower() in ('true', '1', 'yes')

    # API security and runtime hardening
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*')
    AUTH_RATE_LIMIT = os.environ.get('AUTH_RATE_LIMIT', '5 per minute')
    DEFAULT_RATE_LIMIT = os.environ.get('DEFAULT_RATE_LIMIT', '300 per hour')
    ENFORCE_HTTPS = os.environ.get('ENFORCE_HTTPS', 'False').lower() in ('true', '1', 'yes')
    ENABLE_PROMETHEUS_METRICS = os.environ.get('ENABLE_PROMETHEUS_METRICS', 'True').lower() in ('true', '1', 'yes')

    # Redis / Celery
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL

    # Scheduling
    MAX_APPOINTMENTS_PER_DOCTOR_PER_DAY = 40
    OVERLOAD_THRESHOLD = 0.85          # above this → reassign
    FORECAST_UPDATE_INTERVAL = 30      # seconds
    FORECAST_WINDOW_MINUTES = 60       # rolling window

    # Backup / DR
    BACKUP_INTERVAL_MINUTES = int(os.environ.get('BACKUP_INTERVAL_MINUTES', '15'))
    BACKUP_RETENTION_HOURS = int(os.environ.get('BACKUP_RETENTION_HOURS', '48'))
    BACKUP_DIR = os.environ.get('BACKUP_DIR', '/data/backups')

    # Simulation defaults
    AVG_CONSULT_MINUTES = 10
    DAILY_PATIENT_TARGET = 500
