"""
Celery Worker Entry Point
=========================
Run with:
  celery -A celery_worker.celery worker --loglevel=info
  celery -A celery_worker.celery beat  --loglevel=info   (for periodic tasks)
"""

from app import create_app
from tasks import make_celery

flask_app = create_app()
celery    = make_celery(flask_app)
