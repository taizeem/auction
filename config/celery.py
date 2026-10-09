import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("live_auction")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic task schedule
app.conf.beat_schedule = {
    "check-auction-expiries-every-5-seconds": {
        "task": "auctions.tasks.check_and_close_expired_auctions",
        "schedule": 5.0,  # runs every 5 seconds
    },
}