"""
Celery configuration for CampusPlayer background task processing.
Run Celery worker with: celery -A celery_config.celery worker --loglevel=info
"""
import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

def make_celery(app=None):
    """Create Celery instance."""
    celery = Celery(
        'campusplayer',
        broker=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
        backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0'),
        include=['celery_tasks']
    )
    
    celery.conf.update(
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        task_track_started=True,
        # For videos of ANY duration (including 30+ hours):
        # - Hard limit: None (allow infinite runtime)
        # - Soft limit: None (no warning/timeout)
        # - Sending acks late lets the task re-queue on worker restart instead of being lost
        task_time_limit=None,       # No hard limit - prevent worker kill for long transcodes
        task_soft_time_limit=None,  # No soft limit - prevent SoftTimeLimitExceeded
        worker_max_tasks_per_child=1000,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
    )
    
    if app:
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        celery.Task = ContextTask
    
    return celery

# Create default celery instance (will be configured with app later)
celery = make_celery()