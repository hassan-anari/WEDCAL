from celery import shared_task

from apps.base_tap.services import purge_expired_tap_receipts, recover_pending_tap_accumulators


@shared_task(autoretry_for=(), max_retries=0)  # type: ignore[untyped-decorator]
def recover_pending_base_taps() -> int:
    processed = recover_pending_tap_accumulators()
    purge_expired_tap_receipts()
    from django.utils import timezone

    from apps.base_tap.v2 import purge_expired_leases

    purge_expired_leases(at=timezone.now())
    return processed
