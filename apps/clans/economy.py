"""Phase 8 Clan economy services.

Lock order is Player -> ClanSeason -> Membership -> projections/inventory.  Every
public mutation follows it so activity settlement and Treasury spending cannot
deadlock by acquiring the shared aggregate in opposite orders.
"""

from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.clans.models import (
    ClanAchievementUnlock,
    ClanActiveEffect,
    ClanCaptainBonusDaily,
    ClanContributionEntry,
    ClanEconomicSettlement,
    ClanHQXPEntry,
    ClanInventory,
    ClanItemPurchase,
    ClanItemUse,
    ClanMembership,
    ClanSeason,
    ClanTreasuryEntry,
)
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ClanConfig, ClanHQRule, ResolvedGameConfig
from apps.players.models import Player
from apps.profile_collection.models import PlayerAchievementUnlock
from apps.rewards.services import IdempotencyConflict, settle_reward


def hq_rule(policy: ClanConfig, xp: int) -> ClanHQRule:
    return max((rule for rule in policy.hq_curve if rule.min_xp <= xp), key=lambda rule: rule.level)


def effective_capacity(clan_season: ClanSeason, policy: ClanConfig) -> int:
    return min(
        hq_rule(policy, clan_season.hq_xp).base_capacity + clan_season.capacity_bonus,
        policy.max_capacity_including_items,
    )


def active_effect_bps(
    clan_season: ClanSeason, category: str, at: datetime
) -> tuple[int, list[int]]:
    rows = list(
        clan_season.active_effects.filter(
            category=category, starts_at__lte=at, expires_at__gt=at
        ).order_by("-value", "pk")
    )
    return (rows[0].value, [rows[0].pk]) if rows else (0, [])


def _evaluate_achievements(
    cs: ClanSeason, policy: ClanConfig, config: GameConfigVersion, at: datetime
) -> None:
    rules = (
        ("united-front", cs.contribution_total, policy.united_front_threshold),
        ("treasury-keeper", cs.treasury_earned_total, policy.treasury_keeper_threshold),
    )
    for key, current, target in rules:
        if current >= target:
            ClanAchievementUnlock.objects.get_or_create(
                clan_season=cs,
                key=key,
                defaults={
                    "config_version": config,
                    "unlocked_at": at,
                    "metadata": {"threshold": target},
                },
            )
    # banner-raised deliberately remains fail-closed: no canonical shared milestone exists.


@transaction.atomic
def settle_clan_reward(
    *,
    membership: ClanMembership,
    source_type: str,
    source_id: str,
    gross_amount: int,
    idempotency_key: str,
    occurred_at: datetime,
    resolved_config: ResolvedGameConfig,
    metadata: dict[str, Any] | None = None,
) -> tuple[ClanEconomicSettlement, bool]:
    if (
        source_type not in {"CLAN_ACTIVITY", "CLAN_MISSION"}
        or type(gross_amount) is not int
        or gross_amount <= 0
    ):
        raise ValidationError("Invalid Clan economic source")
    policy = resolved_config.clan
    if policy is None:
        raise ValidationError("Clan economy is unavailable")
    player = Player.objects.select_for_update().get(pk=membership.player_id)
    cs = (
        ClanSeason.objects.select_for_update()
        .select_related("season")
        .get(pk=membership.clan_season_id)
    )
    member = ClanMembership.objects.select_for_update().get(pk=membership.pk)
    existing = ClanEconomicSettlement.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.membership_id,
            existing.source_type,
            existing.source_id,
            existing.gross_amount,
            existing.config_version_id,
        ) != (member.pk, source_type, source_id, gross_amount, resolved_config.id):
            raise IdempotencyConflict("Idempotency key belongs to another Clan settlement")
        return existing, False
    if (
        member.started_at > occurred_at
        or (member.ended_at and occurred_at >= member.ended_at)
        or member.access_state != ClanMembership.Access.ACTIVE
    ):
        raise ValidationError("Membership was not economically eligible at source time")
    if cs.state == ClanSeason.State.ENDED or cs.bot_admin_lost_at is not None:
        raise ValidationError("Clan Home economy is paused")
    treasury = gross_amount * policy.treasury_split_bps // 10000
    net = gross_amount - treasury
    contribution_bps, effect_ids = active_effect_bps(cs, "CONTRIBUTION_RATE_BONUS_BPS", occurred_at)
    contribution_numerator = (
        gross_amount * policy.contribution.gross_numerator * (10000 + contribution_bps)
    )
    contribution = contribution_numerator // (policy.contribution.gross_denominator * 10000)
    hq_bps, hq_effect_ids = active_effect_bps(cs, "HQ_XP_RATE_BONUS_BPS", occurred_at)
    hq_numerator = cs.hq_conversion_remainder + contribution * (10000 + hq_bps)
    hq_denominator = policy.contribution.units_per_hq_xp * 10000
    hq_amount, cs.hq_conversion_remainder = divmod(hq_numerator, hq_denominator)
    score_numerator = (
        cs.score_conversion_remainder + contribution * policy.contribution.score_numerator
    )
    score, cs.score_conversion_remainder = divmod(
        score_numerator, policy.contribution.score_denominator
    )
    config = GameConfigVersion.objects.get(pk=resolved_config.id)
    economic = ClanEconomicSettlement.objects.create(
        clan_season=cs,
        membership=member,
        source_type=source_type,
        source_id=source_id,
        gross_amount=gross_amount,
        player_amount=net,
        treasury_amount=treasury,
        contribution_amount=contribution,
        hq_xp_amount=hq_amount,
        clan_score_amount=score,
        config_version=config,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        metadata={
            **(metadata or {}),
            "contribution_effect_ids": effect_ids,
            "hq_effect_ids": hq_effect_ids,
        },
    )
    if treasury:
        ClanTreasuryEntry.objects.create(
            clan_season=cs,
            direction=ClanTreasuryEntry.Direction.CREDIT,
            amount=treasury,
            source_type=source_type,
            source_id=source_id,
            config_version=config,
            idempotency_key=f"{idempotency_key}:treasury",
            occurred_at=occurred_at,
            metadata=metadata or {},
        )
    reward, _ = settle_reward(
        player=player,
        source_type=source_type,
        source_id=source_id,
        context="CLAN_HOME",
        gross_amount=gross_amount,
        player_amount=net,
        clan_treasury_amount=treasury,
        idempotency_key=f"{idempotency_key}:reward",
        occurred_at=occurred_at,
        resolved_config=resolved_config,
        metadata={**(metadata or {}), "clan_season_id": cs.pk, "membership_id": member.pk},
    )
    economic.reward = reward
    economic.save(update_fields=("reward",))
    ClanContributionEntry.objects.create(
        clan_season=cs,
        membership=member,
        settlement=economic,
        amount=contribution,
        gross_basis=gross_amount,
        config_version=config,
        occurred_at=occurred_at,
    )
    if hq_amount:
        ClanHQXPEntry.objects.create(
            clan_season=cs,
            source_type="CONTRIBUTION",
            source_id=str(economic.pk),
            amount=hq_amount,
            config_version=config,
            idempotency_key=f"{idempotency_key}:hq",
            occurred_at=occurred_at,
            metadata={"effect_ids": hq_effect_ids},
        )
    cs.treasury_balance += treasury
    cs.treasury_earned_total += treasury
    cs.contribution_total += contribution
    cs.hq_xp += hq_amount
    cs.competitive_score += score
    cs.hq_level = hq_rule(policy, cs.hq_xp).level
    cs.base_capacity = hq_rule(policy, cs.hq_xp).base_capacity
    cs.save()
    member.contribution_total += contribution
    member.save(update_fields=("contribution_total",))
    if contribution:
        PlayerAchievementUnlock.objects.get_or_create(
            player=player,
            achievement_key="first-contribution",
            defaults={
                "unlocked_at": occurred_at,
                "config_version": config,
                "evidence_category": "FIRST_CONTRIBUTION",
                "evidence_metadata": {"settlement_id": economic.pk},
            },
        )
    _evaluate_achievements(cs, policy, config, occurred_at)
    if (
        source_type == "CLAN_ACTIVITY"
        and member.player_id != cs.captain_id
        and policy.captain_bonus.enabled
    ):
        accumulator, _ = ClanCaptainBonusDaily.objects.select_for_update().get_or_create(
            clan_season=cs, period_key=occurred_at.date().isoformat()
        )
        source_key = f"{source_type}:{source_id}"
        if source_key not in accumulator.processed_sources:
            old_due = min(
                accumulator.eligible_gross * policy.captain_bonus.share_bps // 10000,
                policy.captain_bonus.daily_cap,
            )
            accumulator.eligible_gross += gross_amount
            new_due = min(
                accumulator.eligible_gross * policy.captain_bonus.share_bps // 10000,
                policy.captain_bonus.daily_cap,
            )
            delta = new_due - old_due
            accumulator.processed_sources = [*accumulator.processed_sources, source_key]
            if delta:
                captain = Player.objects.select_for_update().get(pk=cs.captain_id)
                try:
                    settle_reward(
                        player=captain,
                        source_type="CAPTAIN_BONUS",
                        source_id=f"{cs.pk}:{accumulator.period_key}:{new_due}",
                        context="CLAN_HOME",
                        gross_amount=delta,
                        player_amount=delta,
                        clan_treasury_amount=0,
                        idempotency_key=f"captain-bonus:{cs.pk}:{accumulator.period_key}:{new_due}",
                        occurred_at=occurred_at,
                        resolved_config=resolved_config,
                        metadata={"eligible_gross_total": accumulator.eligible_gross},
                        create_settlement_outbox=False,
                    )
                    accumulator.awarded_amount += delta
                except Exception:
                    # Moderation-blocked Captains are deliberately not awarded retroactively.
                    pass
            accumulator.save()
    return economic, True


def _captain(cs: ClanSeason, actor: Player) -> None:
    if cs.captain_id != actor.pk:
        raise PermissionDenied("Captain role required")
    if cs.state != ClanSeason.State.ACTIVE or cs.season.status != "ACTIVE":
        raise ValidationError("Clan economic mutations are unavailable")


@transaction.atomic
def purchase_item(
    *,
    clan_season: ClanSeason,
    actor: Player,
    item_id: str,
    idempotency_key: str,
    resolved_config: ResolvedGameConfig,
) -> tuple[ClanItemPurchase, bool]:
    Player.objects.select_for_update().get(pk=actor.pk)
    cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
    _captain(cs, actor)
    policy = resolved_config.clan
    if policy is None:
        raise ValidationError("Clan economy unavailable")
    item = policy.item(item_id)
    if not item.available_s1:
        raise ValidationError("Item is unavailable in Season 1")
    existing = ClanItemPurchase.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.clan_season_id != cs.pk
            or existing.item_id != item_id
            or existing.price != item.price
        ):
            raise IdempotencyConflict("Idempotency key belongs to another purchase")
        return existing, False
    if cs.treasury_balance < item.price:
        raise ValidationError("Insufficient Treasury balance")
    config = GameConfigVersion.objects.get(pk=resolved_config.id)
    entry = ClanTreasuryEntry.objects.create(
        clan_season=cs,
        direction="DEBIT",
        amount=item.price,
        source_type="SHOP_PURCHASE",
        source_id=item_id,
        config_version=config,
        idempotency_key=f"{idempotency_key}:treasury",
        occurred_at=timezone.now(),
    )
    inventory, _ = ClanInventory.objects.select_for_update().get_or_create(
        clan_season=cs, item_id=item_id
    )
    inventory.quantity += 1
    inventory.save(update_fields=("quantity",))
    cs.treasury_balance -= item.price
    cs.save(update_fields=("treasury_balance", "updated_at"))
    return ClanItemPurchase.objects.create(
        clan_season=cs,
        purchaser=actor,
        item_id=item_id,
        price=item.price,
        config_version=config,
        treasury_entry=entry,
        idempotency_key=idempotency_key,
    ), True


@transaction.atomic
def activate_item(
    *,
    clan_season: ClanSeason,
    actor: Player,
    item_id: str,
    idempotency_key: str,
    resolved_config: ResolvedGameConfig,
    at: datetime | None = None,
) -> tuple[ClanItemUse, bool]:
    now = at or timezone.now()
    Player.objects.select_for_update().get(pk=actor.pk)
    cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
    _captain(cs, actor)
    policy = resolved_config.clan
    if policy is None:
        raise ValidationError("Clan economy unavailable")
    item = policy.item(item_id)
    if not item.available_s1:
        raise ValidationError("Item is unavailable in Season 1")
    existing = ClanItemUse.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.clan_season_id != cs.pk or existing.item_id != item_id:
            raise IdempotencyConflict("Idempotency key belongs to another activation")
        return existing, False
    inventory = (
        ClanInventory.objects.select_for_update().filter(clan_season=cs, item_id=item_id).first()
    )
    if inventory is None or inventory.quantity == 0:
        raise ValidationError("Item is not in inventory")
    uses = cs.item_uses.filter(item_id=item_id).count()
    if item.max_uses_per_season is not None and uses >= item.max_uses_per_season:
        raise ValidationError("Season item-use limit reached")
    if item.activation == "CONTEXT" and any(
        e.category == "SEASON_CAPACITY_ADD" for e in item.effects
    ):
        addition = sum(e.value for e in item.effects if e.category == "SEASON_CAPACITY_ADD")
        if effective_capacity(cs, policy) + addition > policy.max_capacity_including_items:
            raise ValidationError("Capacity is already at its hard maximum")
    config = GameConfigVersion.objects.get(pk=resolved_config.id)
    use = ClanItemUse.objects.create(
        clan_season=cs,
        actor=actor,
        item_id=item_id,
        config_version=config,
        idempotency_key=idempotency_key,
        used_at=now,
    )
    result: dict[str, Any] = {}
    if item.activation == "TIMED":
        assert item.duration_minutes is not None
        for effect in item.effects:
            ClanActiveEffect.objects.create(
                clan_season=cs,
                item_use=use,
                category=effect.category,
                value=effect.value,
                starts_at=now,
                expires_at=now + timedelta(minutes=item.duration_minutes),
            )
        result = {"expires_at": (now + timedelta(minutes=item.duration_minutes)).isoformat()}
    else:
        for effect in item.effects:
            if effect.category == "SEASON_CAPACITY_ADD":
                cs.capacity_bonus += effect.value
            elif effect.category == "HQ_XP_GRANT":
                cs.hq_xp += effect.value
                ClanHQXPEntry.objects.create(
                    clan_season=cs,
                    source_type="ITEM",
                    source_id=str(use.pk),
                    amount=effect.value,
                    config_version=config,
                    idempotency_key=f"{idempotency_key}:hq",
                    occurred_at=now,
                )
        cs.hq_level = hq_rule(policy, cs.hq_xp).level
        cs.base_capacity = hq_rule(policy, cs.hq_xp).base_capacity
        result = {"effective_capacity": effective_capacity(cs, policy), "hq_xp": cs.hq_xp}
        cs.save()
    inventory.quantity -= 1
    inventory.save(update_fields=("quantity",))
    use.result = result
    use.save(update_fields=("result",))
    return use, True
