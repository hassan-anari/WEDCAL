from celery import shared_task
from django.utils import timezone

from apps.network.models import CreatorGroupRegistration
from apps.network.services import (
    purge_activity_evidence,
    settle_completed_windows,
    verify_registration,
)


@shared_task(
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)  # type: ignore[untyped-decorator]
def verify_creator_registration(registration_id: int) -> str:
    return verify_registration(registration_id)


@shared_task  # type: ignore[untyped-decorator]
def reverify_active_creator_groups(limit: int = 200) -> int:
    ids = list(
        CreatorGroupRegistration.objects.filter(
            state__in=(
                CreatorGroupRegistration.State.ACTIVE,
                CreatorGroupRegistration.State.ADMIN_REQUIRED,
                CreatorGroupRegistration.State.REVIEW,
            ),
            next_verification_at__lte=timezone.now(),
        )
        .order_by("next_verification_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    for item in ids:
        verify_creator_registration.delay(item)
    return len(ids)


@shared_task  # type: ignore[untyped-decorator]
def purge_network_evidence() -> tuple[int, int]:
    return purge_activity_evidence()


@shared_task  # type: ignore[untyped-decorator]
def settle_creator_windows(limit: int = 200) -> int:
    return settle_completed_windows(limit=limit)
