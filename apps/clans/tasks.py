from celery import shared_task
from django.conf import settings

from apps.clans.models import ClanInvite
from apps.clans.services import recover_invite_creations, revoke_invite_remote


@shared_task  # type: ignore[untyped-decorator]
def revoke_clan_invite(invite_id: int) -> None:
    revoke_invite_remote(invite_id)


@shared_task  # type: ignore[untyped-decorator]
def recover_invite_revocations() -> int:
    ids = list(
        ClanInvite.objects.filter(
            state__in=[ClanInvite.State.REVOKE_PENDING, ClanInvite.State.REVOKE_FAILED],
            revoke_attempts__lt=settings.CLAN_INVITE_MAX_REVOKE_ATTEMPTS,
        ).values_list("pk", flat=True)[: settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE]
    )
    for invite_id in ids:
        revoke_clan_invite.delay(invite_id)
    return len(ids)


@shared_task  # type: ignore[untyped-decorator]
def recover_invite_creation() -> int:
    return recover_invite_creations()
