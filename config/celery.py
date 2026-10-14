import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("live_auction")

# Reads all settings with CELERY_ prefix from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Automatically discovers tasks.py in all installed apps (e.g. auctions/tasks.py)
app.autodiscover_tasks()