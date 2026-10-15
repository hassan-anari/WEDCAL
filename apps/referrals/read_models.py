from datetime import datetime
from typing import Any

from django.db.models import Sum

from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.referrals.models import ReferralRelation
from apps.referrals.services import referral_activation_is_safe
from apps.rewards.models import RewardLedger


def eligibility(relation: ReferralRelation, at: datetime) -> dict[str, Any]:
    config = resolve_effective_game_config(at)
    points = (
        PlayerProgression.objects.filter(player=relation.invitee)
        .values_list("lifetime_points", flat=True)
        .first()
        or 0
    )
    level = calculate_progression(points, config.progression).level
    return {
        "level": level,
        "required_level": config.referral.min_level,
        "required_account_age_hours": config.referral.min_account_age_hours,
        "eligible": referral_activation_is_safe(relation, config, at),
    }


def earnings(player: Player) -> int:
    return (
        RewardLedger.objects.filter(player=player, source_type="REFERRAL").aggregate(
            total=Sum("player_amount")
        )["total"]
        or 0
    )
