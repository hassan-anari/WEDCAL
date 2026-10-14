from celery import shared_task
from django.utils import timezone

from apps.players.avatars import purge_expired_avatars, refresh_player_avatar


@shared_task(autoretry_for=(), max_retries=0)  # type: ignore[untyped-decorator]
def refresh_avatar(player_id: int) -> None:
    refresh_player_avatar(player_id)


@shared_task(autoretry_for=(), max_retries=0)  # type: ignore[untyped-decorator]
def purge_avatars() -> int:
    return purge_expired_avatars(at=timezone.now())


@shared_task(autoretry_for=(), max_retries=0)  # type: ignore[untyped-decorator]
def refresh_due_avatars() -> int:
    from apps.players.avatars import configured
    from apps.players.models import PlayerAvatar

    if not configured():
        return 0
    ids = list(
        PlayerAvatar.objects.filter(refresh_after__lte=timezone.now())
        .order_by("refresh_after")
        .values_list("player_id", flat=True)[:20]
    )
    for player_id in ids:
        refresh_player_avatar(player_id)
    return len(ids)
