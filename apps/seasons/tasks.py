from celery import shared_task
from django.db import DatabaseError


@shared_task(
    autoretry_for=(ConnectionError, DatabaseError),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    acks_late=True,
)  # type: ignore[untyped-decorator]
def advance_season_close(run_id: int) -> str:
    from apps.seasons.close import advance_close

    return advance_close(run_id)
