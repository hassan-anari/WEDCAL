from datetime import datetime
from typing import Any

from django.db import transaction

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.players.avatars import avatar_url
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed
from apps.progression.services import refresh_player_progression
from apps.seasons.models import PlayerSeason
from apps.seasons.services import is_reward_earning_time, resolve_current_season


@transaction.atomic
def build_bootstrap(player: Player, now: datetime, *, request_id: str) -> dict[str, Any]:
    # Reward settlement uses this same Player lock before updating both projections.
    player = Player.objects.select_for_update().get(pk=player.pk)
    resolved = resolve_effective_game_config(now)
    progression, result = refresh_player_progression(player, resolved)
    season = resolve_current_season(at=now, required=False)
    season_data: dict[str, Any] | None = None
    if season:
        player_season = (
            PlayerSeason.objects.filter(player=player, season=season).only("season_points").first()
        )
        season_data = {
            "code": season.code,
            "title": season.title,
            "status": season.status,
            "starts_at": season.starts_at,
            "ends_at": season.ends_at,
            "season_points": player_season.season_points if player_season else 0,
        }
    daily_data = None
    typing_data = None
    missions_data = None
    if season and resolved.daily.enabled:
        from apps.daily.services import get_daily_state

        daily = get_daily_state(player, now, resolved)
        daily_data = {
            "claimable": daily.claimable,
            "streak": daily.streak,
            "cycle_day": daily.cycle_day,
            "reward_amount": daily.reward_amount,
            "next_reset": daily.period_end,
        }
    if season and resolved.typing.enabled:
        from apps.typing_game.services import get_typing_state

        typing = get_typing_state(player, now)
        typing_data = {
            "attempts_used": typing.attempts_used,
            "attempts_remaining": typing.attempts_remaining,
            "claimable_balance": typing.claimable_balance,
        }
    if season and resolved.missions.enabled:
        from apps.missions.models import PlayerMission
        from apps.missions.services import list_missions

        missions = list_missions(player, now)
        ready = PlayerMission.objects.filter(
            player=player, status=PlayerMission.Status.REWARD_READY
        ).count()
        missions_data = {
            "available_count": sum(item["state"] == "AVAILABLE" for item in missions),
            "reward_ready_count": ready,
            "unclaimed_count": ready,
        }
    referral_data = None
    network_data = None
    if resolved.referral.enabled:
        from apps.referrals.models import ReferralRelation

        relation = ReferralRelation.objects.filter(invitee=player).first()
        referral_data = {
            "status": relation.status if relation else None,
            "verified_invites": ReferralRelation.objects.filter(
                inviter=player, status=ReferralRelation.Status.VERIFIED
            ).count(),
        }
    if resolved.network.enabled:
        from apps.network.models import CreatorGroupRegistration

        groups = CreatorGroupRegistration.objects.filter(creator=player)
        network_data = {
            "group_count": groups.count(),
            "active_group_count": groups.filter(
                state=CreatorGroupRegistration.State.ACTIVE
            ).count(),
        }
    config_model = GameConfigVersion.objects.get(pk=resolved.id)
    reward_earning_allowed = (
        season is not None
        and is_reward_earning_time(season, now)
        and reward_gameplay_allowed(player)
    )
    return {
        "contract_version": "1.1",
        "request_id": request_id,
        "server_time": now,
        "player": {
            "id": str(player.telegram_user_id),
            "language_code": player.language_code,
            "photo_url": avatar_url(player),
            "telegram_user_id": player.telegram_user_id,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "username": player.username,
        },
        "progression": {
            "lifetime_points": progression.lifetime_points,
            "level": result.level,
            "current_threshold": result.current_threshold,
            "next_threshold": result.next_threshold,
            "points_into_level": result.points_into_level,
            "points_remaining": result.points_remaining,
            "dexo_stage": result.dexo_stage,
        },
        "season": season_data,
        "config": {
            "version": resolved.version,
            "effective_at": resolved.effective_at,
            "schema_version": config_model.schema_version,
            "payload_checksum": config_model.payload_checksum,
            "progression": {
                "levels": [
                    {"level": rule.level, "min_lifetime_points": rule.min_lifetime_points}
                    for rule in resolved.progression.levels
                ],
                "dexo_stages": [
                    {"key": stage.key, "min_level": stage.min_level}
                    for stage in resolved.progression.dexo_stages
                ],
                "clan_min_level": resolved.clan.min_level if resolved.clan else None,
            },
        },
        "features": {
            "season": season is not None,
            "daily": daily_data is not None,
            "typing": typing_data is not None,
            "missions": missions_data is not None,
            "referral": referral_data is not None,
            "network": network_data is not None,
            "base_tap": season is not None and resolved.base_tap.enabled,
            "clan": (
                season is not None
                and resolved.clan is not None
                and resolved.clan.enabled
                and result.level >= resolved.clan.min_level
            ),
            "profile": resolved.profile_collection is not None,
            "arena": season is not None,
        },
        "gameplay": {
            "moderation_state": player.moderation_state,
            "reward_earning_allowed": reward_earning_allowed,
        },
        "daily": daily_data,
        "typing": typing_data,
        "missions": missions_data,
        "referral": referral_data,
        "network": network_data,
    }
