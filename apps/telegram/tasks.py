from celery import shared_task

from apps.telegram.services import (
    process_inbound_update,
    purge_expired_ingress_payloads,
    redrive_retryable_inbound_updates,
)


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)  # type: ignore[untyped-decorator]
def process_telegram_update(inbound_id: int) -> None:
    process_inbound_update(inbound_id)


@shared_task  # type: ignore[untyped-decorator]
def purge_expired_telegram_payloads() -> int:
    return purge_expired_ingress_payloads()


@shared_task  # type: ignore[untyped-decorator]
def redrive_telegram_inbound_updates() -> int:
    return redrive_retryable_inbound_updates()
