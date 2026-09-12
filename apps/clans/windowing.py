import hashlib
import logging
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.models import ClanActivity, ClanActivityWindow
from apps.core.activity import window_start_for
from apps.game_config.services import resolve_game_config_version

logger = logging.getLogger(__name__)


def _lock_window(membership_id: int, config_id: int, start: datetime) -> None:
    raw = f"clan:{membership_id}:{config_id}:{start.isoformat()}".encode()
    key = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def _finalize(activity: ClanActivity, now: datetime) -> bool:
    config = resolve_game_config_version(activity.config_version_id)
    policy = config.clan
    if policy is None or activity.membership_id is None:
        ClanActivity.objects.filter(pk=activity.pk).update(window_processed=True)
        return True
    start = window_start_for(activity.observed_at, policy.activity.window_seconds)
    end = start + timedelta(seconds=policy.activity.window_seconds)
    # Wait one full exact-duplicate horizon after source-window close for delayed ingestion.
    if end + timedelta(seconds=policy.anti_fraud.exact_duplicate_window_seconds) > now:
        return False
    with transaction.atomic():
        _lock_window(activity.membership_id, activity.config_version_id, start)
        window, _ = ClanActivityWindow.objects.get_or_create(
            membership_id=activity.membership_id,
            season_id=activity.season_id,
            config_version_id=activity.config_version_id,
            window_start=start,
            defaults={"clan_season_id": activity.clan_season_id},
        )
        if window.finalized_at:
            ClanActivity.objects.filter(
                membership_id=activity.membership_id,
                config_version_id=activity.config_version_id,
                observed_at__gte=start,
                observed_at__lt=end,
            ).update(window_processed=True)
            return True
        source = ClanActivity.objects.filter(
            membership_id=activity.membership_id,
            config_version_id=activity.config_version_id,
            observed_at__gte=start,
            observed_at__lt=end,
        )
        chars = (
            source.filter(
                classification__in=[
                    ClanActivity.Classification.VALID,
                    ClanActivity.Classification.SUSPICIOUS,
                ]
            ).aggregate(total=Sum("rewardable_chars"))["total"]
            or 0
        )
        captain_bonus_source_eligible = not source.filter(
            captain_bonus_eligible_at_source=False, rewardable_chars__gt=0
        ).exists()
        gross = min(
            chars // policy.activity.chars_per_gross_point,
            policy.activity.max_gross_points_per_window,
        )
        window.eligible_chars = chars
        window.gross_amount = gross
        window.finalized_at = now
        if gross:
            assert activity.membership is not None
            settlement, _ = settle_clan_reward(
                membership=activity.membership,
                source_type="CLAN_ACTIVITY",
                source_id=f"window:{window.pk}",
                gross_amount=gross,
                idempotency_key=f"clan-window:{window.pk}",
                occurred_at=start,
                resolved_config=config,
                metadata={
                    "window_start": start.isoformat(),
                    "captain_bonus_eligible_at_source": captain_bonus_source_eligible,
                },
            )
            window.settlement = settlement
        window.save()
        source.update(window_processed=True)
        return True


def finalize_due_windows(*, limit: int = 100, now: datetime | None = None) -> int:
    now = now or timezone.now()
    processed = 0
    candidates = list(
        ClanActivity.objects.filter(window_processed=False)
        .select_related("membership", "clan_season", "season", "config_version")
        .order_by("observed_at", "pk")[: max(limit * 4, limit)]
    )
    seen: set[tuple[int | None, int, datetime]] = set()
    for activity in candidates:
        config = resolve_game_config_version(activity.config_version_id)
        if config.clan is None:
            continue
        identity = (
            activity.membership_id,
            activity.config_version_id,
            window_start_for(activity.observed_at, config.clan.activity.window_seconds),
        )
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if _finalize(activity, now):
                processed += 1
        except Exception:
            logger.exception("clan_activity_window_failed", extra={"activity_id": activity.pk})
        if processed >= limit:
            break
    return processed
