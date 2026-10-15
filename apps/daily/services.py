from dataclasses import dataclass
from datetime import datetime

import structlog
from django.db import transaction

from apps.core.b3 import B3Error
from apps.core.periods import resolve_daily_period
from apps.daily.models import DailyClaim
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.services import resolve_current_season

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DailyState:
    claimable: bool
    streak: int
    cycle_day: int
    reward_amount: int
    period_start: datetime
    period_end: datetime
    last_claimed_at: datetime | None


def get_daily_state(
    player: Player, at: datetime, resolved: ResolvedGameConfig | None = None
) -> DailyState:
    config = resolved or resolve_effective_game_config(at)
    policy = config.daily
    if not policy.enabled:
        raise B3Error("DAILY_UNAVAILABLE")
    period = resolve_daily_period(at, policy.reset_timezone, policy.reset_hour, policy.reset_minute)
    current = DailyClaim.objects.filter(player=player, claim_period=period.key).first()
    previous = DailyClaim.objects.filter(player=player).order_by("-period_start").first()
    if current:
        return DailyState(
            False,
            current.streak_count_after_claim,
            current.cycle_day,
            current.reward_amount,
            period.start,
            period.end,
            current.claimed_at,
        )
    consecutive = previous is not None and previous.period_start == period.previous_start
    next_streak = previous.streak_count_after_claim + 1 if consecutive and previous else 1
    cycle = ((next_streak - 1) % len(policy.reward_cycle_points)) + 1
    return DailyState(
        True,
        previous.streak_count_after_claim if consecutive and previous else 0,
        cycle,
        policy.reward_cycle_points[cycle - 1],
        period.start,
        period.end,
        previous.claimed_at if previous else None,
    )


def claim_daily(player: Player, at: datetime) -> tuple[DailyClaim, bool]:
    resolved = resolve_effective_game_config(at)
    if not resolved.daily.enabled:
        raise B3Error("DAILY_UNAVAILABLE")
    season = resolve_current_season(at=at)
    assert season is not None
    period = resolve_daily_period(
        at, resolved.daily.reset_timezone, resolved.daily.reset_hour, resolved.daily.reset_minute
    )
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        existing = DailyClaim.objects.filter(player=player, claim_period=period.key).first()
        if existing:
            logger.info("daily_duplicate_claim", player_id=player.pk, period=period.key)
            return existing, False
        previous = (
            DailyClaim.objects.filter(player=player, period_start__lt=period.start)
            .order_by("-period_start")
            .first()
        )
        streak = (
            previous.streak_count_after_claim + 1
            if previous and previous.period_start == period.previous_start
            else 1
        )
        cycle = ((streak - 1) % len(resolved.daily.reward_cycle_points)) + 1
        reward = resolved.daily.reward_cycle_points[cycle - 1]
        claim = DailyClaim.objects.create(
            player=player,
            season=season,
            claim_period=period.key,
            period_start=period.start,
            period_end=period.end,
            streak_count_after_claim=streak,
            cycle_day=cycle,
            reward_amount=reward,
            config_version=GameConfigVersion.objects.get(pk=resolved.id),
            claimed_at=at,
        )
        ledger, _ = settle_player_reward(
            player=player,
            source_type="DAILY",
            source_id=period.key,
            context="DAILY",
            amount=reward,
            idempotency_key=f"daily:{player.pk}:{period.key}",
            occurred_at=at,
            resolved_config=resolved,
        )
        claim.reward_ledger = ledger
        claim.save(update_fields=["reward_ledger", "updated_at"])
    logger.info("daily_claimed", player_id=player.pk, period=period.key, reward=reward)
    return claim, True
