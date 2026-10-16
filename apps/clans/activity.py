from datetime import datetime, timedelta

from django.db import IntegrityError, models, transaction

from apps.clans.models import (
    ClanActivity,
    ClanMembership,
    ClanSeason,
)
from apps.core.activity import (
    fingerprints,
    lock_activity_qualification,
    lock_activity_window,
    qualify_text,
    window_start_for,
)
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed_at
from apps.seasons.services import is_reward_earning_time, resolve_historical_season


def membership_access_at(membership: ClanMembership, at: datetime) -> bool:
    event = (
        membership.events.filter(
            kind__in=["ACTIVATED", "ACCESS_LOST", "ACCESS_RESTORED"], occurred_at__lte=at
        )
        .order_by("-occurred_at", "-pk")
        .values_list("kind", flat=True)
        .first()
    )
    return event in {"ACTIVATED", "ACCESS_RESTORED"}


def home_integrity_at(clan_season: ClanSeason, at: datetime) -> bool:
    event = (
        clan_season.events.filter(
            kind__in=["BOT_ADMIN_LOST", "BOT_ADMIN_RESTORED"], occurred_at__lte=at
        )
        .order_by("-occurred_at", "-pk")
        .values_list("kind", flat=True)
        .first()
    )
    authority = (
        clan_season.events.filter(
            kind__in=["CAPTAIN_AUTHORITY_LOST", "CAPTAIN_AUTHORITY_RESTORED"],
            occurred_at__lte=at,
        )
        .order_by("-occurred_at", "-pk")
        .values_list("kind", flat=True)
        .first()
    )
    return event != "BOT_ADMIN_LOST" and authority != "CAPTAIN_AUTHORITY_LOST"


def context_owned_at(clan_season: ClanSeason, at: datetime) -> bool:
    if not clan_season.season.starts_at <= at < clan_season.season.ends_at:
        return False
    ended = clan_season.events.filter(kind="CLAN_SEASON_DISSOLVED", occurred_at__lte=at).exists()
    return clan_season.created_at <= at and not ended


def record_clan_activity(
    *,
    clan_season: ClanSeason,
    sender: Player,
    message_id: int,
    update_id: int,
    text: str | None,
    observed_at: datetime,
) -> ClanActivity | None:
    config = resolve_effective_game_config(observed_at)
    policy = config.clan
    season = resolve_historical_season(at=observed_at, required=False)
    if policy is None or season is None or season.pk != clan_season.season_id:
        return None
    membership = (
        ClanMembership.objects.filter(
            player=sender, clan_season=clan_season, started_at__lte=observed_at
        )
        .filter(models.Q(ended_at__isnull=True) | models.Q(ended_at__gt=observed_at))
        .order_by("-started_at", "-pk")
        .first()
    )
    access = bool(membership and membership_access_at(membership, observed_at))
    integrity = home_integrity_at(clan_season, observed_at)
    owned = context_owned_at(clan_season, observed_at)
    body = text or ""
    exact, normalized = fingerprints(body)
    rate = ClanActivity.objects.filter(
        clan_season=clan_season,
        sender=sender,
        observed_at__gt=observed_at - timedelta(seconds=60),
        observed_at__lte=observed_at,
    ).count()
    exact_count = (
        ClanActivity.objects.filter(
            clan_season=clan_season,
            sender=sender,
            exact_fingerprint=exact,
            observed_at__gt=observed_at
            - timedelta(seconds=policy.anti_fraud.exact_duplicate_window_seconds),
            observed_at__lte=observed_at,
        ).count()
        + 1
    )
    near_count = (
        ClanActivity.objects.filter(
            clan_season=clan_season,
            sender=sender,
            normalized_fingerprint=normalized,
            observed_at__gt=observed_at
            - timedelta(seconds=policy.anti_fraud.near_duplicate_window_seconds),
            observed_at__lte=observed_at,
        ).count()
        + 1
    )
    eligible = bool(
        membership
        and access
        and integrity
        and owned
        and is_reward_earning_time(season, observed_at)
        and reward_gameplay_allowed_at(sender, observed_at)
    )
    captain = Player.objects.get(pk=clan_season.captain_id)
    captain_bonus_eligible = reward_gameplay_allowed_at(captain, observed_at)
    result = qualify_text(
        text=body,
        eligible=eligible,
        min_chars=policy.activity.min_message_chars,
        max_chars=policy.activity.max_rewardable_chars,
        rate_count=rate,
        rate_limit=policy.activity.max_messages_per_minute,
        duplicate_count=max(exact_count, near_count),
        suspicious_count=policy.anti_fraud.suspicious_count,
        reject_count=policy.anti_fraud.reject_count,
    )
    start = window_start_for(observed_at, policy.activity.window_seconds)
    classification = (
        ClanActivity.Classification.SUSPICIOUS
        if result.suspicious
        else ClanActivity.Classification.VALID
        if result.accepted
        else ClanActivity.Classification.REJECTED
    )
    try:
        with transaction.atomic():
            from apps.seasons.barrier import acquire_economic_write_barrier

            acquire_economic_write_barrier(season.pk)
            # Qualification horizons cross aggregation and config boundaries.
            # Always take this broader lock before the narrower window lock.
            lock_activity_qualification(clan_season.pk, sender.pk)
            finalized = False
            if membership:
                lock_activity_window(membership.pk, config.id, start)
                finalized = clan_season.clanactivitywindow_set.filter(
                    membership=membership,
                    config_version_id=config.id,
                    window_start=start,
                    finalized_at__isnull=False,
                ).exists()
                # A later finalized settlement is also a durable qualification
                # cutoff: an older arrival must not rewrite its frozen history.
                finalized = (
                    finalized
                    or clan_season.clanactivitywindow_set.filter(
                        membership=membership,
                        window_start__gt=start,
                        finalized_at__isnull=False,
                    ).exists()
                )
            if finalized:
                classification = ClanActivity.Classification.LATE
            rewardable = 0 if finalized else result.rewardable_chars
            row = ClanActivity.objects.create(
                clan_season=clan_season,
                membership=membership,
                sender=sender,
                group=clan_season.clan.telegram_group,
                season=season,
                config_version=GameConfigVersion.objects.get(pk=config.id),
                telegram_message_id=message_id,
                telegram_update_id=update_id,
                observed_at=observed_at,
                character_count=len(body),
                rewardable_chars=rewardable,
                classification=classification,
                exact_fingerprint=exact,
                normalized_fingerprint=normalized,
                membership_eligible_at_source=access,
                home_integrity_at_source=integrity,
                context_owned_at_source=owned,
                captain_bonus_eligible_at_source=captain_bonus_eligible,
            )
    except IntegrityError:
        return ClanActivity.objects.filter(
            group=clan_season.clan.telegram_group, telegram_message_id=message_id
        ).first()
    return row
