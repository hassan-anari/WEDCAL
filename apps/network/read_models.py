from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from apps.core.b3 import canonical_state
from apps.game_config.services import resolve_effective_game_config
from apps.network.models import CreatorGroupRegistration, NetworkActivity
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed


def group_output(row: CreatorGroupRegistration) -> dict[str, Any]:
    valid = row.activity.filter(
        classification=NetworkActivity.Classification.VALID, rewardable_chars__gt=0
    )
    activity = valid.aggregate(
        qualified_characters=Sum("rewardable_chars"),
        unique_speakers=Count("sender_id", distinct=True),
        last_activity_at=Max("observed_at"),
    )
    total = row.settlements.aggregate(points=Sum("points"))["points"] or 0
    return {
        "public_id": str(row.public_id),
        "title": row.group.title,
        "state": row.state,
        "bot_admin": row.group.bot_is_admin,
        "broadcast_opted_in": row.group.groupbroadcastsubscription.enabled
        if hasattr(row.group, "groupbroadcastsubscription")
        else False,
        "creator_verified_at": row.creator_verified_at,
        "bot_verified_at": row.bot_verified_at,
        "next_verification_at": row.next_verification_at,
        "verification_retrying": bool(row.last_verification_error),
        "earned_points": total,
        "qualified_characters": activity["qualified_characters"] or 0,
        "unique_speakers": activity["unique_speakers"],
        "last_activity_at": activity["last_activity_at"],
    }


@transaction.atomic
def network_summary(player: Player) -> dict[str, Any]:
    player = Player.objects.select_for_update().get(pk=player.pk)
    canonical = canonical_state(player, timezone.now())
    config = resolve_effective_game_config(timezone.now())
    rows = CreatorGroupRegistration.objects.filter(creator=player)
    active = rows.filter(state="ACTIVE").count()
    from apps.network.models import CreatorWindowSettlement

    earned = (
        CreatorWindowSettlement.objects.filter(registration__creator=player).aggregate(
            total=Sum("points")
        )["total"]
        or 0
    )
    return {
        "canonical": canonical,
        "enabled": config.network.enabled,
        "active_groups": active,
        "group_count": rows.count(),
        "max_active_groups": config.network.max_active_groups,
        "can_register": config.network.enabled
        and active < config.network.max_active_groups
        and reward_gameplay_allowed(player),
        "speaker_rewards_enabled": config.network.speaker_enabled,
        "earned_points": earned,
        "settlement_window_seconds": config.network.window_seconds,
    }
