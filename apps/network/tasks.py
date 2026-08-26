from celery import shared_task

from apps.network.models import CreatorGroupRegistration
from apps.network.services import purge_activity_evidence, verify_registration


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
            )
        )
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )
    for item in ids:
        verify_creator_registration.delay(item)
    return len(ids)


@shared_task  # type: ignore[untyped-decorator]
def purge_network_evidence() -> tuple[int, int]:
    return purge_activity_evidence()
