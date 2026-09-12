from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.clans.economy import active_effect_bps, settle_clan_reward
from apps.clans.models import ClanMembership, ClanMissionDefinition, ClanMissionProgress
from apps.game_config.services import ResolvedGameConfig


@transaction.atomic
def record_mission_progress(
    *,
    definition: ClanMissionDefinition,
    membership: ClanMembership,
    source_id: str,
    units: int,
    occurred_at: datetime,
    resolved_config: ResolvedGameConfig,
) -> ClanMissionProgress:
    """Server-domain hook; intentionally not exposed as a client mutation."""
    if (
        type(units) is not int
        or units <= 0
        or not definition.enabled
        or not definition.starts_at <= occurred_at < definition.ends_at
    ):
        raise ValidationError("Mission progress is not eligible")
    if membership.ended_at and occurred_at >= membership.ended_at:
        raise ValidationError("Membership is not eligible for mission progress")
    target_membership = (
        membership if definition.kind == ClanMissionDefinition.Kind.INDIVIDUAL else None
    )
    progress, _ = ClanMissionProgress.objects.select_for_update().get_or_create(
        definition=definition, clan_season=membership.clan_season, membership=target_membership
    )
    if progress.completed_at:
        return progress
    bonus, _ = active_effect_bps(
        membership.clan_season, "CLAN_MISSION_PROGRESS_BONUS_BPS", occurred_at
    )
    progress.units += units * (10000 + bonus) // 10000
    progress.save(update_fields=("units",))
    if progress.units < definition.target_units:
        return progress
    policy = resolved_config.clan
    if policy is None:
        raise ValidationError("Clan mission economy unavailable")
    band = next((x for x in policy.mission_bands if x.key == definition.reward_band), None)
    if band is None:
        raise ValidationError("Mission reward band unavailable")
    settlement, _ = settle_clan_reward(
        membership=membership,
        source_type="CLAN_MISSION",
        source_id=f"{definition.key}:{definition.version}:{progress.pk}",
        gross_amount=band.participant_gross_points,
        idempotency_key=f"clan-mission:{progress.pk}",
        occurred_at=occurred_at,
        resolved_config=resolved_config,
        metadata={"definition_id": definition.pk, "progress_source": source_id},
    )
    progress.completed_at = occurred_at
    progress.reward_settlement = settlement
    progress.save(update_fields=("completed_at", "reward_settlement"))
    return progress
