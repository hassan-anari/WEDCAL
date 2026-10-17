"""Phase 8 Clan economy services.

Clan mutations lock the semantic idempotency key, then ClanSeason, Players in
ascending primary-key order, Memberships, and child projections/inventory.
Telegram ownership transitions lock TelegramGroup before program-specific rows.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models, transaction
from django.utils import timezone

from apps.clans.models import (
    ClanAchievementUnlock,
    ClanActiveEffect,
    ClanCaptainBonusConfigAccumulator,
    ClanCaptainBonusDaily,
    ClanCaptainBonusSource,
    ClanContributionEntry,
    ClanEconomicAccumulator,
    ClanEconomicSettlement,
    ClanHQXPEntry,
    ClanIdempotencyKey,
    ClanInventory,
    ClanItemPurchase,
    ClanItemUse,
    ClanMembership,
    ClanSeason,
    ClanTreasuryEntry,
)
from apps.core.periods import resolve_daily_period
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import (
    ClanConfig,
    ClanHQRule,
    ResolvedGameConfig,
    resolve_game_config_version,
)
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


def canonical_clan_projection(clan_season: ClanSeason) -> dict[str, Any]:
    """Calculate every ClanSeason projection owned by the canonical rebuild service."""
    credits = (
        clan_season.treasury_entries.filter(direction="CREDIT").aggregate(v=models.Sum("amount"))[
            "v"
        ]
        or 0
    )
    debits = (
        clan_season.treasury_entries.filter(direction="DEBIT").aggregate(v=models.Sum("amount"))[
            "v"
        ]
        or 0
    )
    contribution = clan_season.contribution_entries.aggregate(v=models.Sum("amount"))["v"] or 0
    hq_xp = clan_season.hq_xp_entries.aggregate(v=models.Sum("amount"))["v"] or 0
    score_source = clan_season.economic_settlements.filter(clan_score_amount__gt=0).aggregate(
        v=models.Sum("clan_score_amount"), reached_at=models.Max("occurred_at")
    )
    score = score_source["v"] or 0
    policy = resolve_game_config_version(clan_season.config_version_id).clan
    if policy is None or debits > credits:
        raise ValidationError("Clan durable sources cannot produce a valid projection")
    capacity_bonus = 0
    for use in clan_season.item_uses.select_related("config_version"):
        use_policy = resolve_game_config_version(use.config_version_id).clan
        if use_policy is None:
            continue
        item = use_policy.item(use.item_id)
        capacity_bonus += sum(e.value for e in item.effects if e.category == "SEASON_CAPACITY_ADD")
    rule = hq_rule(policy, hq_xp)
    return {
        "treasury_balance": credits - debits,
        "treasury_earned_total": credits,
        "contribution_total": contribution,
        "hq_xp": hq_xp,
        "competitive_score": score,
        "score_reached_at": score_source["reached_at"] if score else None,
        "capacity_bonus": capacity_bonus,
        "hq_level": rule.level,
        "base_capacity": rule.base_capacity,
    }


def rebuild_clan_projections(clan_season: ClanSeason) -> ClanSeason:
    """Rebuild derived Phase 8 totals exclusively from durable source rows."""
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().get(pk=clan_season.pk)
        expected = canonical_clan_projection(cs)
        score_changed = (
            cs.competitive_score != expected["competitive_score"]
            or cs.score_reached_at != expected["score_reached_at"]
        )
        for field, value in expected.items():
            setattr(cs, field, value)
        if score_changed:
            cs.ranking_generation += 1
        cs.save()
        return cs


def active_effect_bps(
    clan_season: ClanSeason, category: str, at: datetime
) -> tuple[int, list[int]]:
    rows = list(
        clan_season.active_effects.filter(
            category=category, starts_at__lte=at, expires_at__gt=at
        ).order_by("-value", "pk")
    )
    return (rows[0].value, [rows[0].pk]) if rows else (0, [])


def _validate_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValidationError("Idempotency key must contain 1 to 200 characters")


def _lock_key(value: str) -> None:
    key = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def claim_idempotency_key(value: str, action: str, semantic: str) -> None:
    _validate_key(value)
    _lock_key(f"clan-idempotency:{value}")
    digest = hashlib.sha256(semantic.encode()).hexdigest()
    row = ClanIdempotencyKey.objects.filter(key=value).first()
    if row:
        if row.action != action or row.semantic_hash != digest:
            raise IdempotencyConflict("Idempotency key belongs to another Clan mutation")
        return
    ClanIdempotencyKey.objects.create(key=value, action=action, semantic_hash=digest)


def _apply_hq_xp_locked(
    *,
    cs: ClanSeason,
    raw_amount: int,
    source_type: str,
    source_id: str,
    idempotency_key: str,
    occurred_at: datetime,
    policy: ClanConfig,
    config: GameConfigVersion,
    apply_effect: bool = True,
) -> tuple[ClanHQXPEntry | None, int]:
    existing = ClanHQXPEntry.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.clan_season_id,
            existing.source_type,
            existing.source_id,
            existing.config_version_id,
        ) != (cs.pk, source_type, source_id, config.pk):
            raise IdempotencyConflict("HQ XP idempotency key conflict")
        return existing, 0
    bps, effect_ids = (
        active_effect_bps(cs, "HQ_XP_RATE_BONUS_BPS", occurred_at) if apply_effect else (0, [])
    )
    amount = raw_amount * (10000 + bps) // 10000
    if amount <= 0:
        return None, 0
    entry = ClanHQXPEntry.objects.create(
        clan_season=cs,
        source_type=source_type,
        source_id=source_id,
        amount=amount,
        config_version=config,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        metadata={"raw_amount": raw_amount, "effect_ids": effect_ids},
    )
    cs.hq_xp += amount
    rule = hq_rule(policy, cs.hq_xp)
    cs.hq_level = rule.level
    cs.base_capacity = rule.base_capacity
    return entry, amount


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
    # banner-raised is owner-deferred to Season 2; no S1 unlock or War evaluation.


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
    source_facts_verified: bool = False,
) -> tuple[ClanEconomicSettlement, bool]:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(membership.season_id)
    _validate_key(idempotency_key)
    if (
        source_type not in {"CLAN_ACTIVITY", "CLAN_MISSION"}
        or type(gross_amount) is not int
        or gross_amount <= 0
    ):
        raise ValidationError("Invalid Clan economic source")
    policy = resolved_config.clan
    if policy is None:
        raise ValidationError("Clan economy is unavailable")
    claim_idempotency_key(
        idempotency_key,
        "ECONOMIC_SETTLEMENT",
        f"{membership.pk}:{source_type}:{source_id}:{gross_amount}:{resolved_config.id}",
    )
    cs = (
        ClanSeason.objects.select_for_update()
        .select_related("season")
        .get(pk=membership.clan_season_id)
    )
    locked_players = {
        row.pk: row
        for row in Player.objects.select_for_update()
        .filter(pk__in={membership.player_id, cs.captain_id})
        .order_by("pk")
    }
    player = locked_players[membership.player_id]
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
    if not source_facts_verified and (
        member.started_at > occurred_at or (member.ended_at and occurred_at >= member.ended_at)
    ):
        raise ValidationError("Membership was not economically eligible at source time")
    from apps.clans.activity import context_owned_at, home_integrity_at, membership_access_at

    if not source_facts_verified and (
        not membership_access_at(member, occurred_at)
        or not home_integrity_at(cs, occurred_at)
        or not context_owned_at(cs, occurred_at)
    ):
        raise ValidationError("Clan Home economy is paused")
    contribution_bps, effect_ids = active_effect_bps(cs, "CONTRIBUTION_RATE_BONUS_BPS", occurred_at)
    config = GameConfigVersion.objects.get(pk=resolved_config.id)
    accumulator, _ = ClanEconomicAccumulator.objects.select_for_update().get_or_create(
        clan_season=cs, config_version=config
    )
    # Prospective carry: historical completed sources return above without consuming it.
    # The existing ClanSeason lock serializes members; config-specific accumulators
    # never blend entitlements under different immutable published policies.
    previous_treasury_remainder = accumulator.treasury_remainder
    treasury, accumulator.treasury_remainder = divmod(
        previous_treasury_remainder + gross_amount * policy.treasury_split_bps, 10000
    )
    net = gross_amount - treasury
    treasury_audit = {
        "treasury_policy": "B6_FRACTIONAL_V1",
        "treasury_split_bps": policy.treasury_split_bps,
        "treasury_remainder_before": previous_treasury_remainder,
        "treasury_remainder_after": accumulator.treasury_remainder,
        "treasury_denominator": 10000,
    }
    contribution_numerator = (
        accumulator.contribution_remainder
        + gross_amount * policy.contribution.gross_numerator * (10000 + contribution_bps)
    )
    contribution_denominator = policy.contribution.gross_denominator * 10000
    contribution, accumulator.contribution_remainder = divmod(
        contribution_numerator, contribution_denominator
    )
    hq_bps, hq_effect_ids = active_effect_bps(cs, "HQ_XP_RATE_BONUS_BPS", occurred_at)
    hq_numerator = accumulator.hq_remainder + contribution * (10000 + hq_bps)
    hq_denominator = policy.contribution.units_per_hq_xp * 10000
    hq_amount, accumulator.hq_remainder = divmod(hq_numerator, hq_denominator)
    score_numerator = (
        accumulator.score_remainder + contribution * policy.contribution.score_numerator
    )
    score, accumulator.score_remainder = divmod(
        score_numerator, policy.contribution.score_denominator
    )
    accumulator.save()
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
            **treasury_audit,
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
            metadata={**(metadata or {}), **treasury_audit},
        )
    reward, _ = settle_reward(
        player=player,
        source_type=source_type,
        source_id=source_id,
        context="CLAN_HOME",
        gross_amount=gross_amount,
        player_amount=net,
        clan_treasury_amount=treasury,
        allow_treasury_only_split=True,
        idempotency_key=f"{idempotency_key}:reward",
        occurred_at=occurred_at,
        resolved_config=resolved_config,
        metadata={
            **(metadata or {}),
            **treasury_audit,
            "clan_season_id": cs.pk,
            "membership_id": member.pk,
        },
        source_season=cs.season,
        preserve_config_version=True,
        allow_closed_source_season=cs.season.status == "CLOSED",
        source_eligibility_verified=source_facts_verified,
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
        _apply_hq_xp_locked(
            cs=cs,
            raw_amount=hq_amount,
            source_type="CONTRIBUTION",
            source_id=str(economic.pk),
            idempotency_key=f"{idempotency_key}:hq",
            occurred_at=occurred_at,
            policy=policy,
            config=config,
            apply_effect=False,
        )
    cs.treasury_balance += treasury
    cs.treasury_earned_total += treasury
    cs.contribution_total += contribution
    cs.competitive_score += score
    if score:
        cs.score_reached_at = max(filter(None, (cs.score_reached_at, occurred_at)))
        cs.ranking_generation += 1
    cs.save()
    if score:
        from apps.arena.services import publish_clan_ranking

        publish_clan_ranking(cs, source_id=f"clan-settlement:{economic.pk}")
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
        period = resolve_daily_period(
            occurred_at,
            resolved_config.daily.reset_timezone,
            resolved_config.daily.reset_hour,
            resolved_config.daily.reset_minute,
        )
        daily, _ = ClanCaptainBonusDaily.objects.select_for_update().get_or_create(
            clan_season=cs,
            period_key=period.key,
            defaults={"config_version": config},
        )
        source_key = f"{source_type}:{source_id}"
        eligible_gross = int((metadata or {}).get("captain_bonus_gross_amount", gross_amount))
        source_eligible = bool((metadata or {}).get("captain_bonus_eligible_at_source", True))
        source = ClanCaptainBonusSource.objects.filter(daily=daily, source_key=source_key).first()
        if source is None:
            config_accumulator, _ = (
                ClanCaptainBonusConfigAccumulator.objects.select_for_update().get_or_create(
                    daily=daily, config_version=config
                )
            )
            numerator = config_accumulator.calculation_remainder
            if source_eligible:
                numerator += eligible_gross * policy.captain_bonus.share_bps
            calculated_delta, config_accumulator.calculation_remainder = divmod(numerator, 10000)
            available = max(policy.captain_bonus.daily_cap - daily.awarded_amount, 0)
            delta = min(calculated_delta, available)
            source = ClanCaptainBonusSource.objects.create(
                daily=daily,
                source_key=source_key,
                config_version=config,
                eligible_gross=eligible_gross if source_eligible else 0,
                awarded_amount=delta,
                occurred_at=occurred_at,
            )
            config_accumulator.eligible_gross += source.eligible_gross
            config_accumulator.calculated_amount += calculated_delta
            config_accumulator.save()
            if delta:
                captain = locked_players[cs.captain_id]
                settle_reward(
                    player=captain,
                    source_type="CAPTAIN_BONUS",
                    source_id=f"{cs.pk}:{daily.period_key}:{source.pk}",
                    context="CLAN_HOME",
                    gross_amount=delta,
                    player_amount=delta,
                    clan_treasury_amount=0,
                    idempotency_key=f"captain-bonus-source:{source.pk}",
                    occurred_at=occurred_at,
                    resolved_config=resolved_config,
                    metadata={
                        "eligible_gross": source.eligible_gross,
                        "source_config_version_id": config.pk,
                    },
                    create_settlement_outbox=False,
                    source_eligibility_verified=True,
                )
                daily.awarded_amount += delta
            daily.eligible_gross += source.eligible_gross
            daily.processed_sources = [*daily.processed_sources, source_key]
            daily.save()
    return economic, True


def _captain(cs: ClanSeason, actor: Player) -> None:
    from apps.players.services import reward_gameplay_allowed

    if not reward_gameplay_allowed(actor):
        raise PermissionDenied("Player cannot perform Clan economic actions")
    if not ClanMembership.objects.filter(
        clan_season=cs,
        player=actor,
        role="CAPTAIN",
        ended_at__isnull=True,
        access_state=ClanMembership.Access.ACTIVE,
    ).exists():
        raise PermissionDenied("Current active Captain membership required")
    if cs.captain_id != actor.pk:
        raise PermissionDenied("Captain role required")
    if cs.state != ClanSeason.State.ACTIVE or cs.season.status != "ACTIVE":
        raise ValidationError("Clan economic mutations are unavailable")
    from apps.clans.governance import clan_home_integrity_available
    from apps.seasons.services import is_reward_earning_time

    if not clan_home_integrity_available(cs) or not is_reward_earning_time(
        cs.season, timezone.now()
    ):
        raise ValidationError("Clan Home integrity or economic window is unavailable")


@transaction.atomic
def purchase_item(
    *,
    clan_season: ClanSeason,
    actor: Player,
    item_id: str,
    idempotency_key: str,
    resolved_config: ResolvedGameConfig,
) -> tuple[ClanItemPurchase, bool]:
    claim_idempotency_key(
        idempotency_key,
        "ITEM_PURCHASE",
        f"{clan_season.pk}:{actor.pk}:{item_id}:{resolved_config.id}",
    )
    cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
    actor = Player.objects.select_for_update().get(pk=actor.pk)
    _captain(cs, actor)
    policy = resolved_config.clan
    if policy is None or not policy.enabled:
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
    claim_idempotency_key(
        idempotency_key,
        "ITEM_USE",
        f"{clan_season.pk}:{actor.pk}:{item_id}:{resolved_config.id}",
    )
    cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
    actor = Player.objects.select_for_update().get(pk=actor.pk)
    _captain(cs, actor)
    policy = resolved_config.clan
    if policy is None or not policy.enabled:
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
                _apply_hq_xp_locked(
                    cs=cs,
                    raw_amount=effect.value,
                    source_type="ITEM",
                    source_id=str(use.pk),
                    idempotency_key=f"{idempotency_key}:hq",
                    occurred_at=now,
                    policy=policy,
                    config=config,
                )
        result = {"effective_capacity": effective_capacity(cs, policy), "hq_xp": cs.hq_xp}
        cs.save()
    inventory.quantity -= 1
    inventory.save(update_fields=("quantity",))
    use.result = result
    use.save(update_fields=("result",))
    return use, True
