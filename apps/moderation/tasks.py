# mypy: disable-error-code="untyped-decorator"
from celery import shared_task

from apps.moderation.services import expire_suspensions


@shared_task(name="apps.moderation.tasks.expire_temporary_suspensions")
def expire_temporary_suspensions() -> int:
    return expire_suspensions()
