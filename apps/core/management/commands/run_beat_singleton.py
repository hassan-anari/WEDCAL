import subprocess

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Run Celery Beat while holding a PostgreSQL advisory singleton lock"

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(114211)")
            if not cursor.fetchone()[0]:
                raise CommandError("Another Beat owns the production singleton lock")
            try:
                completed = subprocess.run(  # noqa: S603
                    [
                        "celery",
                        "-A",
                        "dexo_land",
                        "beat",
                        "--loglevel=INFO",
                        "--schedule=/app/run/celerybeat-schedule",
                    ],
                    check=False,
                )
                if completed.returncode:
                    raise CommandError(f"Celery Beat exited {completed.returncode}")
            finally:
                cursor.execute("SELECT pg_advisory_unlock(114211)")
