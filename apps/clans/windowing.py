import logging
from datetime import datetime, timedelta

from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.models import ClanActivity, ClanActivityWindow
from apps.core.activity import (
    lock_activity_qualification,
    lock_activity_window,
    qualify_text,
    window_start_for,
)
from apps.game_config.services import ClanConfig, resolve_game_config_version
from apps.players.services import reward_gameplay_allowed_at
from apps.seasons.services import is_reward_earning_time

logger = logging.getLogger(__name__)


def _requalify(source: models.QuerySet[ClanActivity], policy: ClanConfig) -> None:
    """Persist canonical classifications in total source order before settlement."""
    rows = list(
        source.exclude(classification=ClanActivity.Classification.LATE)
        .select_related("membership", "sender", "season")
        .order_by("observed_at", "telegram_update_id", "telegram_message_id", "pk")
    )
    for row in rows:
        prior = (
            ClanActivity.objects.filter(clan_season_id=row.clan_season_id, sender_id=row.sender_id)
            .exclude(classification=ClanActivity.Classification.LATE)
            .filter(
                models.Q(observed_at__lt=row.observed_at)
                | models.Q(
                    observed_at=row.observed_at, telegram_update_id__lt=row.telegram_update_id
                )
                | models.Q(
                    observed_at=row.observed_at,
                    telegram_update_id=row.telegram_update_id,
                    telegram_message_id__lt=row.telegram_message_id,
                )
                | models.Q(
                    observed_at=row.observed_at,
                    telegram_update_id=row.telegram_update_id,
                    telegram_message_id=row.telegram_message_id,
                    pk__lt=row.pk,
                )
            )
        )
        rate = prior.filter(observed_at__gt=row.observed_at - timedelta(seconds=60)).count()
        exact = (
            prior.filter(
                exact_fingerprint=row.exact_fingerprint,
                observed_at__gt=row.observed_at
                - timedelta(seconds=policy.anti_fraud.exact_duplicate_window_seconds),
            ).count()
            + 1
        )
        near = (
            prior.filter(
                normalized_fingerprint=row.normalized_fingerprint,
                observed_at__gt=row.observed_at
                - timedelta(seconds=policy.anti_fraud.near_duplicate_window_seconds),
            ).count()
            + 1
        )
        member = row.membership
        assert row.sender is not None
        eligible = bool(
            member
            and member.started_at <= row.observed_at
            and (member.ended_at is None or row.observed_at < member.ended_at)
            and row.membership_eligible_at_source
            and row.home_integrity_at_source
            and row.context_owned_at_source
            and is_reward_earning_time(row.season, row.observed_at)
            and reward_gameplay_allowed_at(row.sender, row.observed_at)
        )
        result = qualify_text(
            text="x" * row.character_count,
            eligible=eligible,
            min_chars=policy.activity.min_message_chars,
            max_chars=policy.activity.max_rewardable_chars,
            rate_count=rate,
            rate_limit=policy.activity.max_messages_per_minute,
            duplicate_count=max(exact, near),
            suspicious_count=policy.anti_fraud.suspicious_count,
            reject_count=policy.anti_fraud.reject_count,
        )
        row.classification = (
            ClanActivity.Classification.SUSPICIOUS
            if result.suspicious
            else ClanActivity.Classification.VALID
            if result.accepted
            else ClanActivity.Classification.REJECTED
        )
        row.rewardable_chars = result.rewardable_chars
        row.save(update_fields=("classification", "rewardable_chars"))


def _finalize(activity: ClanActivity, now: datetime) -> bool:
    config = resolve_game_config_version(activity.config_version_id)
    policy = config.clan
    if policy is None or activity.membership_id is None:
        ClanActivity.objects.filter(pk=activity.pk).update(window_processed=True)
        return True
    start = window_start_for(activity.observed_at, policy.activity.window_seconds)
    end = start + timedelta(seconds=policy.activity.window_seconds)
    qualification_horizon = max(
        policy.anti_fraud.exact_duplicate_window_seconds,
        policy.anti_fraud.near_duplicate_window_seconds,
    )
    if end + timedelta(seconds=qualification_horizon) > now:
        return False
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(activity.season_id)
        assert activity.sender_id is not None
        lock_activity_qualification(activity.clan_season_id, activity.sender_id)
        lock_activity_window(activity.membership_id, activity.config_version_id, start)
        window, _ = ClanActivityWindow.objects.get_or_create(
            membership_id=activity.membership_id,
            season_id=activity.season_id,
            config_version_id=activity.config_version_id,
            window_start=start,
            defaults={"clan_season_id": activity.clan_season_id, "window_end": end},
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
        _requalify(source, policy)
        chars = (
            source.filter(
                classification__in=[
                    ClanActivity.Classification.VALID,
                    ClanActivity.Classification.SUSPICIOUS,
                ]
            ).aggregate(total=Sum("rewardable_chars"))["total"]
            or 0
        )
        captain_eligible_chars = (
            source.filter(
                classification__in=[
                    ClanActivity.Classification.VALID,
                    ClanActivity.Classification.SUSPICIOUS,
                ],
                captain_bonus_eligible_at_source=True,
            ).aggregate(total=Sum("rewardable_chars"))["total"]
            or 0
        )
        captain_bonus_gross = min(
            captain_eligible_chars // policy.activity.chars_per_gross_point,
            policy.activity.max_gross_points_per_window,
        )
        gross = min(
            chars // policy.activity.chars_per_gross_point,
            policy.activity.max_gross_points_per_window,
        )
        window.eligible_chars = chars
        window.gross_amount = gross
        window.window_end = end
        window.finalized_at = now
        if gross:
            assert activity.membership is not None
            economic_at = source.filter(rewardable_chars__gt=0).latest("observed_at").observed_at
            settlement, _ = settle_clan_reward(
                membership=activity.membership,
                source_type="CLAN_ACTIVITY",
                source_id=f"window:{window.pk}",
                gross_amount=gross,
                idempotency_key=f"clan-window:{window.pk}",
                occurred_at=economic_at,
                resolved_config=config,
                metadata={
                    "window_start": start.isoformat(),
                    "captain_bonus_eligible_at_source": captain_bonus_gross > 0,
                    "captain_bonus_gross_amount": captain_bonus_gross,
                    "window_end": end.isoformat(),
                },
                source_facts_verified=True,
            )
            window.settlement = settlement
            from apps.clans.missions import queue_mission_signal

            queue_mission_signal(
                membership=activity.membership,
                source_key=f"contribution:{settlement.pk}",
                requirement="CLAN_CONTRIBUTION",
                units=settlement.contribution_amount,
                occurred_at=economic_at,
            )
        window.save()
        from apps.clans.missions import queue_mission_signal

        for item in source.filter(rewardable_chars__gt=0).select_related("membership"):
            assert item.membership is not None
            queue_mission_signal(
                membership=item.membership,
                source_key=f"clan-activity:{item.pk}",
                requirement="VALID_CLAN_ACTIVITY",
                units=1,
                occurred_at=item.observed_at,
            )
        source.update(window_processed=True)
        from apps.arena.recognition import record_clan_window_presence

        record_clan_window_presence(window)
        latest_accepted = source.filter(rewardable_chars__gt=0).order_by("-observed_at").first()
        if latest_accepted and latest_accepted.membership_id:
            from apps.clans.governance import record_clan_activity as advance_activity_clock

            advance_activity_clock(
                latest_accepted.membership_id, occurred_at=latest_accepted.observed_at
            )
        return True


def finalize_due_windows(
    *, limit: int = 100, now: datetime | None = None, season_id: int | None = None
) -> int:
    now = now or timezone.now()
    processed = 0
    source = ClanActivity.objects.filter(window_processed=False)
    if season_id is not None:
        source = source.filter(season_id=season_id)
    candidates = list(
        source.select_related("membership", "clan_season", "season", "config_version").order_by(
            "observed_at", "pk"
        )[: max(limit * 4, limit)]
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
