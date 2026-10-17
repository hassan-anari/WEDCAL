"""Independent recovery causes; authoritative Telegram evidence never changes identity."""

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.clans.models import ClanSeason
from apps.clans.services import ClanConflict
from apps.telegram import bot_api


def preserve_recovery_causes(cs: ClanSeason) -> None:
    """Read pre-B5 single-cause episodes without changing their resolved semantics."""
    if cs.state == ClanSeason.State.RECOVERY:
        if cs.recovery_reason == "CAPTAIN_INACTIVITY" and cs.captain_inactivity_recovery_at is None:
            cs.captain_inactivity_recovery_at = cs.recovery_started_at or cs.updated_at
        if cs.recovery_reason == "BOT_ADMIN_LOSS" and cs.bot_admin_recovery_at is None:
            cs.bot_admin_recovery_at = cs.recovery_started_at or cs.updated_at


def recovery_reasons(cs: ClanSeason) -> list[str]:
    reasons = []
    if cs.captain_authority_lost_at is not None:
        reasons.append("CAPTAIN_AUTHORITY_LOSS")
    if cs.captain_inactivity_recovery_at is not None:
        reasons.append("CAPTAIN_INACTIVITY")
    if cs.bot_admin_lost_at is not None or cs.bot_admin_recovery_at is not None:
        reasons.append("BOT_ADMIN_LOSS")
    if cs.recovery_reason and cs.recovery_reason not in reasons:
        # Preserve legacy/governance episodes until their authorized resolution.
        reasons.append(cs.recovery_reason)
    return reasons


def sync_recovery(cs: ClanSeason, at: datetime) -> None:
    """Called with the ClanSeason locked; retained governance never auto-cancels."""
    if cs.state == ClanSeason.State.ENDED:
        return
    recovering = bool(
        cs.captain_authority_lost_at
        or cs.captain_inactivity_recovery_at
        or cs.bot_admin_recovery_at
        or cs.recovery_governance_opened_at
        or (cs.state == ClanSeason.State.RECOVERY and not cs.recovery_reason)
    )
    reasons = []
    if cs.captain_authority_lost_at:
        reasons.append("CAPTAIN_AUTHORITY_LOSS")
    if cs.captain_inactivity_recovery_at:
        reasons.append("CAPTAIN_INACTIVITY")
    if cs.bot_admin_lost_at or cs.bot_admin_recovery_at:
        reasons.append("BOT_ADMIN_LOSS")
    cs.recovery_reason = (
        reasons[0] if reasons else (cs.recovery_reason if cs.recovery_governance_opened_at else "")
    )
    cs.state = ClanSeason.State.RECOVERY if recovering else ClanSeason.State.ACTIVE
    cs.recovery_started_at = (cs.recovery_started_at or at) if recovering else None


def observe_captain_authority(
    clan_season_id: int,
    *,
    has_authority: bool,
    authoritative: bool,
    observed_at: datetime,
    fresh_verification: bool = False,
    bot_authorized: bool = False,
    whole_second_update: bool = False,
) -> ClanSeason:
    from apps.clans.governance import _event
    from apps.notifications.services import emit_notification

    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().get(pk=clan_season_id)
        if (
            not authoritative
            or cs.state == ClanSeason.State.ENDED
            or (
                cs.captain_authority_observed_at
                and observed_at < cs.captain_authority_observed_at
                and (
                    has_authority
                    or not whole_second_update
                    or observed_at.replace(microsecond=0)
                    < cs.captain_authority_observed_at.replace(microsecond=0)
                )
            )
            or (has_authority and cs.captain_authority_observed_at == observed_at)
        ):
            return cs
        preserve_recovery_causes(cs)
        cs.captain_authority_observed_at = max(
            observed_at, cs.captain_authority_observed_at or observed_at
        )
        if not has_authority and cs.captain_authority_lost_at is None:
            # A whole-second loss tied with a more precise verification is
            # conservatively ordered after that verification, never before it.
            effective_at = cs.captain_authority_observed_at
            cs.captain_authority_lost_at = effective_at
            _event(
                cs,
                "RECOVERY_ENTERED",
                effective_at,
                source_type="CAPTAIN_AUTHORITY",
                source_id=effective_at.isoformat(),
                metadata={"reason": "CAPTAIN_AUTHORITY_LOSS"},
            )
            _event(
                cs,
                "CAPTAIN_AUTHORITY_LOST",
                effective_at,
                source_type="TELEGRAM",
                source_id=effective_at.isoformat(),
            )
            # Durable In-App notification commits with the transition; Telegram is
            # queued only through the existing configured notification machinery.
            emit_notification(
                recipient=cs.captain,
                event_key="CLAN_CAPTAIN_AUTHORITY_LOST",
                source_domain="CLAN",
                source_type="CLAN_SEASON",
                source_id=str(cs.pk),
                idempotency_key=f"clan-authority-loss:{cs.pk}:{effective_at.isoformat()}",
                title="Clan recovery",
                body=(
                    "Restore your Telegram group administrator authority "
                    "to recover Clan management."
                ),
                payload={"clan_season_id": cs.pk, "reason": "CAPTAIN_AUTHORITY_LOSS"},
                telegram=True,
            )
        elif (
            has_authority and fresh_verification and bot_authorized and cs.bot_admin_lost_at is None
        ):
            if cs.captain_authority_lost_at is not None:
                cs.captain_authority_lost_at = None
                _event(
                    cs,
                    "CAPTAIN_AUTHORITY_RESTORED",
                    observed_at,
                    source_type="TELEGRAM_VERIFICATION",
                    source_id=observed_at.isoformat(),
                )
        sync_recovery(cs, observed_at)
        cs.save()
        return cs


def verify_captain_authority(clan_season_id: int) -> ClanSeason:
    """No remote calls under business locks; request-start time fences stale results."""
    from apps.clans.governance import observe_bot_admin_state

    cs = ClanSeason.objects.select_related("clan__telegram_group", "captain").get(pk=clan_season_id)
    if cs.state == ClanSeason.State.ENDED:
        raise ClanConflict("Clan Season is ended")
    observed_at = timezone.now()
    chat_id = cs.clan.telegram_group.telegram_chat_id
    captain = bot_api.get_chat_member(chat_id, cs.captain.telegram_user_id)
    bot = (
        bot_api.get_chat_member(chat_id, settings.TELEGRAM_BOT_ID)
        if settings.TELEGRAM_BOT_ID
        else None
    )
    statuses = {"creator", "administrator", "member", "restricted", "left", "kicked"}
    captain_status = (captain.result or {}).get("status")
    bot_status = (bot.result or {}).get("status") if bot else None
    bot_known = bool(bot and bot.ok and bot_status in statuses)
    bot_authorized = (
        bool(
            bot_known
            and bot_status == "administrator"
            and (bot.result or {}).get("can_invite_users")
        )
        if bot
        else False
    )
    with transaction.atomic():
        locked = ClanSeason.objects.select_for_update().get(pk=cs.pk)
        # Any committed transition during the remote calls fences positive
        # restoration. Negative evidence can still safely pause the Clan. This
        # also covers source updates whose timestamps have only whole seconds.
        positive_fresh = locked.updated_at == cs.updated_at
        if bot_known and (positive_fresh or not bot_authorized):
            observe_bot_admin_state(
                cs.pk, is_admin=bot_authorized, authoritative=True, observed_at=observed_at
            )
        if captain.ok and captain_status in statuses:
            cs = observe_captain_authority(
                cs.pk,
                has_authority=captain_status in {"creator", "administrator"},
                authoritative=True,
                observed_at=observed_at,
                fresh_verification=positive_fresh,
                bot_authorized=bot_authorized,
            )
    if not captain.ok or captain_status not in statuses or not bot_known:
        raise ClanConflict("Telegram authority verification is temporarily unavailable")
    return cs


def recheck_lost_captain_authority(*, limit: int = 100) -> int:
    """Durable bounded redrive; API budgets still arbitrate every Telegram request."""
    now = timezone.now()
    due = now - timedelta(seconds=max(1, settings.CLAN_AUTHORITY_RECHECK_SECONDS))
    with transaction.atomic():
        rows = list(
            ClanSeason.objects.select_for_update(skip_locked=True)
            .filter(
                state=ClanSeason.State.RECOVERY,
                captain_authority_lost_at__isnull=False,
            )
            .filter(
                Q(captain_authority_last_attempt_at__isnull=True)
                | Q(captain_authority_last_attempt_at__lte=due)
            )
            .order_by("captain_authority_last_attempt_at", "pk")[: min(max(limit, 1), 100)]
        )
        ids = [row.pk for row in rows]
        ClanSeason.objects.filter(pk__in=ids).update(captain_authority_last_attempt_at=now)
    for pk in ids:
        try:
            verify_captain_authority(pk)
        except ClanConflict:
            # Unknown/failed remote state never clears recovery. The next scan retries.
            continue
    return len(ids)
