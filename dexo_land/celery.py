import os

from celery import Celery

os.environ["DJANGO_SETTINGS_MODULE"]

app = Celery("dexo_land")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
