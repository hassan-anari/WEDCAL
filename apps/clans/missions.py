from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.clans.activity import context_owned_at, home_integrity_at, membership_access_at
from apps.clans.economy import (
    _apply_hq_xp_locked,
    active_effect_bps,
    claim_idempotency_key,
    settle_clan_reward,
)
from apps.clans.models import (
    ClanMembership,
    ClanMissionDefinition,
    ClanMissionParticipant,
    ClanMissionProgress,
    ClanMissionSourceEvent,
    ClanSeason,
)
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_game_config_version
from apps.players.services import reward_gameplay_allowed_at
from apps.rewards.services import IdempotencyConflict
from apps.seasons.services import is_reward_earning_time


@transaction.atomic
def record_mission_progress(
    *,
    definition: ClanMissionDefinition,
    membership: ClanMembership,
    source_id: str,
    units: int,
    occurred_at: datetime,
    resolved_config: ResolvedGameConfig | None = None,
) -> ClanMissionProgress:
    """Record one server-owned domain event; no client route exposes this hook."""
    if not source_id.strip() or len(source_id) > 128 or type(units) is not int or units <= 0:
        raise ValidationError("Invalid mission source event")
    claim_idempotency_key(
        f"mission:{definition.pk}:{source_id}",
        "MISSION_PROGRESS",
        f"{definition.pk}:{membership.pk}:{units}:{occurred_at.isoformat()}",
    )
    definition = ClanMissionDefinition.objects.select_related("config_version").get(
        pk=definition.pk
    )
    config = resolve_game_config_version(definition.config_version_id)
    if resolved_config is not None and resolved_config.id != config.id:
        raise ValidationError("Mission must use its definition configuration")
    policy = config.clan
    if policy is None:
        raise ValidationError("Clan mission economy unavailable")
    identity = ClanMembership.objects.values("clan_season_id", "player_id").get(pk=membership.pk)
    cs = ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
    member = (
        ClanMembership.objects.select_for_update()
        .select_related("player", "clan_season__season")
        .get(pk=membership.pk)
    )
    if (
        not definition.enabled
        or not definition.starts_at <= occurred_at < definition.ends_at
        or member.started_at > occurred_at
        or (member.ended_at and occurred_at >= member.ended_at)
        or not membership_access_at(member, occurred_at)
        or not home_integrity_at(cs, occurred_at)
        or not is_reward_earning_time(member.clan_season.season, occurred_at)
        or not reward_gameplay_allowed_at(member.player, occurred_at)
    ):
        raise ValidationError("Mission progress is not eligible at source time")
    existing = ClanMissionSourceEvent.objects.filter(
        definition=definition, source_id=source_id
    ).first()
    if existing:
        if (
            existing.clan_season_id,
            existing.membership_id,
            existing.units,
            existing.occurred_at,
        ) != (cs.pk, member.pk, units, occurred_at):
            raise IdempotencyConflict("Mission source id belongs to another event")
        target_member = member if definition.kind == ClanMissionDefinition.Kind.INDIVIDUAL else None
        return ClanMissionProgress.objects.get(
            definition=definition, clan_season=cs, membership=target_member
        )
    target_member = member if definition.kind == ClanMissionDefinition.Kind.INDIVIDUAL else None
    progress, _ = ClanMissionProgress.objects.select_for_update().get_or_create(
        definition=definition, clan_season=cs, membership=target_member
    )
    if progress.completed_at:
        raise ValidationError("Mission is already complete")
    config_model = GameConfigVersion.objects.get(pk=config.id)
    ClanMissionSourceEvent.objects.create(
        definition=definition,
        clan_season=cs,
        membership=member,
        source_id=source_id,
        units=units,
        occurred_at=occurred_at,
        config_version=config_model,
    )
    participant, _ = ClanMissionParticipant.objects.get_or_create(
        progress=progress, membership=member
    )
    bonus, effect_ids = active_effect_bps(cs, "CLAN_MISSION_PROGRESS_BONUS_BPS", occurred_at)
    numerator = progress.progress_remainder + units * (10000 + bonus)
    credited, progress.progress_remainder = divmod(numerator, 10000)
    progress.units += credited
    if progress.units < definition.target_units:
        progress.save(update_fields=("units", "progress_remainder"))
        return progress
    band = next((x for x in policy.mission_bands if x.key == definition.reward_band), None)
    if band is None:
        raise ValidationError("Mission reward band unavailable")
    participants = list(
        progress.participants.select_related("membership__player").order_by("membership_id")
    )
    from apps.players.models import Player

    list(
        Player.objects.select_for_update()
        .filter(pk__in={item.membership.player_id for item in participants} | {cs.captain_id})
        .order_by("pk")
    )
    first_settlement = None
    for mission_participant in participants:
        participant_membership = mission_participant.membership
        eligible_at_completion = bool(
            participant_membership.started_at <= occurred_at
            and (
                participant_membership.ended_at is None
                or occurred_at < participant_membership.ended_at
            )
            and membership_access_at(participant_membership, occurred_at)
            and home_integrity_at(cs, occurred_at)
            and context_owned_at(cs, occurred_at)
            and reward_gameplay_allowed_at(participant_membership.player, occurred_at)
        )
        mission_participant.eligibility_evaluated_at = occurred_at
        mission_participant.reward_eligible = eligible_at_completion
        mission_participant.ineligibility_reason = (
            "" if eligible_at_completion else "INELIGIBLE_AT_COMPLETION"
        )
        if not eligible_at_completion:
            mission_participant.save(
                update_fields=(
                    "eligibility_evaluated_at",
                    "reward_eligible",
                    "ineligibility_reason",
                )
            )
            continue
        settlement, _ = settle_clan_reward(
            membership=participant_membership,
            source_type="CLAN_MISSION",
            source_id=f"{definition.key}:{definition.version}:{progress.pk}:{participant_membership.pk}",
            gross_amount=band.participant_gross_points,
            idempotency_key=f"clan-mission-reward:{progress.pk}:{participant_membership.pk}",
            occurred_at=occurred_at,
            resolved_config=config,
            metadata={"definition_id": definition.pk, "progress_effect_ids": effect_ids},
            source_facts_verified=True,
        )
        mission_participant.reward_settlement = settlement
        mission_participant.save(
            update_fields=(
                "reward_settlement",
                "eligibility_evaluated_at",
                "reward_eligible",
                "ineligibility_reason",
            )
        )
        first_settlement = first_settlement or settlement
    _apply_hq_xp_locked(
        cs=cs,
        raw_amount=band.direct_hq_xp,
        source_type="CLAN_MISSION",
        source_id=str(progress.pk),
        idempotency_key=f"clan-mission-direct-hq:{progress.pk}",
        occurred_at=occurred_at,
        policy=policy,
        config=config_model,
    )
    cs.save()
    progress.completed_at = occurred_at
    progress.reward_settlement = first_settlement
    progress.save(
        update_fields=("units", "progress_remainder", "completed_at", "reward_settlement")
    )
    return progress
