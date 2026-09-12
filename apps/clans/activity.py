import hashlib
import re
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction

from apps.clans.models import ClanActivity, ClanMembership, ClanSeason
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.seasons.services import resolve_historical_season


def _fingerprints(text: str) -> tuple[str, str]:
    exact = hashlib.sha256(text.encode()).hexdigest()
    normalized = re.sub(r"\W+", "", text.casefold())
    return exact, hashlib.sha256(normalized.encode()).hexdigest()


@transaction.atomic
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
        .filter(ended_at__isnull=True)
        .first()
    )
    body = text or ""
    exact, normalized = _fingerprints(body)
    classification = ClanActivity.Classification.REJECTED
    rewardable = 0
    eligible = bool(
        membership
        and membership.access_state == ClanMembership.Access.ACTIVE
        and clan_season.bot_admin_lost_at is None
        and sender.moderation_state
        not in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
    )
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
    duplicate = max(exact_count, near_count)
    if (
        eligible
        and len(body) >= policy.activity.min_message_chars
        and rate < policy.activity.max_messages_per_minute
        and duplicate < policy.anti_fraud.reject_count
    ):
        classification = (
            ClanActivity.Classification.SUSPICIOUS
            if duplicate >= policy.anti_fraud.suspicious_count
            else ClanActivity.Classification.VALID
        )
        rewardable = min(len(body), policy.activity.max_rewardable_chars)
    try:
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
        )
    except IntegrityError:
        return ClanActivity.objects.filter(
            group=clan_season.clan.telegram_group, telegram_message_id=message_id
        ).first()
    if membership and rewardable:
        ClanMembership.objects.filter(pk=membership.pk, last_clan_active_at__lt=observed_at).update(
            last_clan_active_at=observed_at
        )
    return row
