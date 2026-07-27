"""Configuration Celery.

Sans broker configure, `CELERY_TASK_ALWAYS_EAGER` execute les taches en
synchrone : le projet tourne alors sans Redis, utile en developpement.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("recruit")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
