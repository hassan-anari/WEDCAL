from celery import shared_task
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.clans.models import ClanInvite
from apps.clans.services import recover_invite_creations, revoke_invite_remote


@shared_task  # type: ignore[untyped-decorator]
def finalize_clan_activity_windows(limit: int = 100) -> int:
    """Bounded retry-safe orchestration; source rows remain durable on failures."""
    from apps.clans.windowing import finalize_due_windows

    return finalize_due_windows(limit=min(max(limit, 1), 500))


@shared_task  # type: ignore[untyped-decorator]
def revoke_clan_invite(invite_id: int) -> None:
    revoke_invite_remote(invite_id)


@shared_task  # type: ignore[untyped-decorator]
def recover_invite_revocations() -> int:
    ids = list(
        ClanInvite.objects.filter(
            state__in=[ClanInvite.State.REVOKE_PENDING, ClanInvite.State.REVOKE_FAILED],
        )
        .filter(
            models.Q(next_revoke_retry_at__isnull=True)
            | models.Q(next_revoke_retry_at__lte=timezone.now())
        )
        .values_list("pk", flat=True)[: settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE]
    )
    for invite_id in ids:
        revoke_clan_invite.delay(invite_id)
    return len(ids)


@shared_task  # type: ignore[untyped-decorator]
def recover_invite_creation() -> int:
    return recover_invite_creations()


@shared_task  # type: ignore[untyped-decorator]
def sweep_clan_inactivity(limit: int = 100) -> int:
    from apps.clans.governance import sweep_inactivity

    return sweep_inactivity(limit=limit)


@shared_task  # type: ignore[untyped-decorator]
def sweep_clan_bot_admin_deadlines(limit: int = 100) -> int:
    from apps.clans.governance import sweep_bot_admin_deadlines

    return sweep_bot_admin_deadlines(limit=limit)


@shared_task  # type: ignore[untyped-decorator]
def finalize_clan_governance(limit: int = 100) -> int:
    from apps.clans.governance import finalize_case
    from apps.clans.models import ClanGovernanceCase

    ids = list(
        ClanGovernanceCase.objects.filter(
            status=ClanGovernanceCase.Status.OPEN, voting_deadline__lte=timezone.now()
        )
        .order_by("voting_deadline", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    completed = 0
    for case_id in ids:
        try:
            finalize_case(case_id)
        except Exception:
            continue
        completed += 1
    return completed
