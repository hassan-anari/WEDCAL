from celery import shared_task

from apps.base_tap.services import recover_pending_tap_batches


@shared_task(autoretry_for=(), max_retries=0)  # type: ignore[untyped-decorator]
def recover_pending_base_taps() -> int:
    return recover_pending_tap_batches()
