from contextlib import suppress
from functools import partial

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


def dispatch_verification(registration_id: int) -> None:
    with suppress(Exception):
        verify_creator_registration.delay(registration_id)


def refresh_due_owned_groups(player_id: int) -> None:
    from datetime import timedelta

    from django.db import transaction

    now = timezone.now()
    with transaction.atomic():
        rows = list(
            CreatorGroupRegistration.objects.select_for_update(skip_locked=True)
            .filter(
                creator_id=player_id,
                next_verification_at__lte=now,
                state__in=["REVIEW", "ADMIN_REQUIRED", "ACTIVE"],
            )
            .order_by("next_verification_at")[:20]
        )
        for row in rows:
            # A short reservation bounds polling dispatch; scanner recovers broker loss.
            row.next_verification_at = now + timedelta(seconds=30)
            row.save(update_fields=["next_verification_at"])
            transaction.on_commit(partial(dispatch_verification, row.pk))
