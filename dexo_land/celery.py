import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dexo_land.settings.local")

app = Celery("dexo_land")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
