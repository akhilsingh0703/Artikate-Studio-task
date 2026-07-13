"""Celery application for the Artikate backend.

Importing ``celery_app`` in ``config/__init__.py`` guarantees the app is loaded
when Django starts, so ``@shared_task`` decorators bind to the right instance.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

celery_app = Celery("artikate")

# All ``CELERY_*`` keys in Django settings become Celery config.
celery_app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks.py in every installed app.
celery_app.autodiscover_tasks()
