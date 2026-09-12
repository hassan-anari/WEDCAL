from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.clans.activity import record_clan_activity
from apps.clans.economy import (
    activate_item,
    effective_capacity,
    purchase_item,
    rebuild_clan_projections,
    settle_clan_reward,
)
from apps.clans.models import (
    ClanAchievementUnlock,
    ClanActiveEffect,
    ClanActivity,
    ClanContributionEntry,
    ClanEconomicAccumulator,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    ClanTreasuryEntry,
)
from apps.clans.windowing import finalize_due_windows
from apps.profile_collection.services import ClanEntitlements, NoClanEntitlements
from apps.rewards.models import RewardLedger, RewardSettlementOutbox


def activate_membership(membership: ClanMembership) -> None:
    ClanMembershipEvent.objects.get_or_create(
        membership=membership,
        kind="ACTIVATED",
        telegram_update_id=None,
        defaults={"occurred_at": membership.started_at},
    )


@pytest.mark.django_db
def test_atomic_clan_settlement_uses_gross_for_contribution(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    member = ClanMembership.objects.create(
        player=soldier,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(minutes=1),
        config_version=cs.config_version,
    )
    activate_membership(member)
    result, created = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="source-1",
        gross_amount=9,
        idempotency_key="economic-1",
        occurred_at=now,
        resolved_config=resolved,
    )
    assert created and (result.gross_amount, result.player_amount, result.treasury_amount) == (
        9,
        9,
        0,
    )
    assert result.contribution_amount == 9
    assert result.reward_id is not None
    assert RewardLedger.objects.get(pk=result.reward_id).player_amount == 9
    assert RewardSettlementOutbox.objects.filter(reward_id=result.reward_id).exists()
    assert ClanContributionEntry.objects.get(settlement=result).gross_basis == 9


@pytest.mark.django_db
def test_settlement_replay_and_semantic_conflict(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    first, _ = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="a",
        gross_amount=10,
        idempotency_key="same",
        occurred_at=now,
        resolved_config=resolved,
    )
    replay, created = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="a",
        gross_amount=10,
        idempotency_key="same",
        occurred_at=now,
        resolved_config=resolved,
    )
    assert replay.pk == first.pk and not created
    with pytest.raises(ValueError):
        settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id="b",
            gross_amount=10,
            idempotency_key="same",
            occurred_at=now,
            resolved_config=resolved,
        )


@pytest.mark.django_db
def test_version_scoped_rational_remainders(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    for number in range(5):
        settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id=str(number),
            gross_amount=1,
            idempotency_key=f"fraction-{number}",
            occurred_at=now,
            resolved_config=resolved,
        )
    accumulator = ClanEconomicAccumulator.objects.get(clan_season=cs, config_version_id=resolved.id)
    cs.refresh_from_db()
    assert cs.hq_xp == 1 and accumulator.hq_remainder == 0


@pytest.mark.django_db
def test_activity_source_time_and_canonical_clock(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, player, clan = clan_world
    cs = clan()
    type(cs).objects.filter(pk=cs.pk).update(created_at=now - timedelta(hours=3))
    cs.refresh_from_db()
    soldier = player(ClanRole.SOLDIER)
    member = ClanMembership.objects.create(
        player=soldier,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(hours=2),
        ended_at=now,
        inactivity_warned_at=now - timedelta(days=1),
        config_version=cs.config_version,
    )
    activate_membership(member)
    row = record_clan_activity(
        clan_season=cs,
        sender=soldier,
        message_id=1,
        update_id=1,
        text="x" * 150,
        observed_at=now - timedelta(hours=1),
    )
    member.refresh_from_db()
    assert row and row.membership_id == member.pk and row.rewardable_chars == 150
    # Historical attribution survives closure; closed memberships are not reopened/mutated.
    assert member.inactivity_warned_at is not None
    rejected = record_clan_activity(
        clan_season=cs,
        sender=soldier,
        message_id=2,
        update_id=2,
        text="y" * 150,
        observed_at=now + timedelta(seconds=1),
    )
    assert rejected and rejected.membership_id is None and rejected.rewardable_chars == 0


@pytest.mark.django_db
def test_qualified_activity_clears_warning_through_canonical_clock(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    member.inactivity_warned_at = now - timedelta(days=1)
    member.last_clan_active_at = now - timedelta(days=2)
    member.save()
    row = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=99,
        update_id=99,
        text="meaningful clan home activity" * 6,
        observed_at=timezone.now(),
    )
    assert row is not None
    finalize_due_windows(now=row.observed_at + timedelta(hours=1))
    member.refresh_from_db()
    row.refresh_from_db()
    assert row.rewardable_chars > 0
    assert member.inactivity_warned_at is None and member.next_inactivity_check_at is not None


@pytest.mark.django_db
def test_open_activity_window_never_finalizes(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    member.started_at = now - timedelta(hours=3)
    member.save(update_fields=("started_at",))
    activate_membership(member)
    ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=member.player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=cs.config_version,
        telegram_message_id=10,
        telegram_update_id=10,
        observed_at=now,
        character_count=150,
        rewardable_chars=150,
        classification="VALID",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
    )
    assert finalize_due_windows(now=now + timedelta(minutes=1)) == 0


@pytest.mark.django_db
def test_closed_window_aggregates_once_and_zero_window_finishes(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    type(cs).objects.filter(pk=cs.pk).update(created_at=now - timedelta(hours=3))
    cs.refresh_from_db()
    member = cs.memberships.get()
    member.started_at = now - timedelta(hours=3)
    member.save(update_fields=("started_at",))
    activate_membership(member)
    old = now - timedelta(hours=2)
    for message_id, chars in [(20, 100), (21, 100)]:
        ClanActivity.objects.create(
            clan_season=cs,
            membership=member,
            sender=member.player,
            group=cs.clan.telegram_group,
            season=cs.season,
            config_version=cs.config_version,
            telegram_message_id=message_id,
            telegram_update_id=message_id,
            observed_at=old,
            character_count=chars,
            rewardable_chars=chars,
            classification="VALID",
            membership_eligible_at_source=True,
            home_integrity_at_source=True,
            context_owned_at_source=True,
        )
    assert finalize_due_windows(now=now) == 1
    assert finalize_due_windows(now=now) == 0


@pytest.mark.django_db
def test_shop_purchase_and_effect_authorization(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    cs.treasury_balance = 10000
    cs.save()
    purchase, created = purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="purchase-1",
        resolved_config=resolved,
    )
    assert (
        created
        and ClanTreasuryEntry.objects.get(pk=purchase.treasury_entry_id).direction == "DEBIT"
    )
    use, created = activate_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="activate-1",
        resolved_config=resolved,
    )
    assert created and ClanActiveEffect.objects.filter(item_use=use).exists()
    soldier = player(ClanRole.SOLDIER)
    with pytest.raises(PermissionDenied):
        purchase_item(
            clan_season=cs,
            actor=soldier,
            item_id="rally-beacon",
            idempotency_key="soldier",
            resolved_config=resolved,
        )


@pytest.mark.django_db
def test_capacity_item_and_hq_progression_share_projection(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.treasury_balance = 10000
    cs.save()
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="capacity-expansion-module",
        idempotency_key="cap-buy",
        resolved_config=resolved,
    )
    activate_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="capacity-expansion-module",
        idempotency_key="cap-use",
        resolved_config=resolved,
    )
    cs.refresh_from_db()
    assert resolved.clan is not None
    assert effective_capacity(cs, resolved.clan) == 25


@pytest.mark.django_db
def test_profile_entitlement_is_historical_and_no_clan_seam_is_closed(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="profile",
        gross_amount=1,
        idempotency_key="profile",
        occurred_at=now,
        resolved_config=resolved,
    )
    member.ended_at = now + timedelta(seconds=1)
    member.save()
    assert ClanEntitlements().contribution_units(member.player) == 1
    assert NoClanEntitlements().contribution_units(member.player) == 0


@pytest.mark.django_db
def test_clan_achievements_are_zero_point_durable_unlocks(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    cs.contribution_total = 24999
    cs.save()
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="achievement",
        gross_amount=1,
        idempotency_key="achievement",
        occurred_at=now,
        resolved_config=resolved,
    )
    assert ClanAchievementUnlock.objects.filter(clan_season=cs, key="united-front").count() == 1
    assert not ClanAchievementUnlock.objects.filter(clan_season=cs, key="banner-raised").exists()


@pytest.mark.django_db
def test_projection_rebuild_uses_durable_sources(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="rebuild",
        gross_amount=10,
        idempotency_key="rebuild",
        occurred_at=now,
        resolved_config=resolved,
    )
    cs.treasury_balance = 999
    cs.treasury_earned_total = 999
    cs.contribution_total = 999
    cs.hq_xp = 999
    cs.competitive_score = 999
    cs.save()
    rebuilt = rebuild_clan_projections(cs)
    assert (
        rebuilt.treasury_balance,
        rebuilt.treasury_earned_total,
        rebuilt.contribution_total,
        rebuilt.competitive_score,
    ) == (1, 1, 10, 10)


@pytest.mark.django_db
def test_captain_bonus_is_separate_and_referral_ineligible(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, resolved, player, clan = clan_world
    cs = clan()
    captain = cs.memberships.get()
    activate_membership(captain)
    soldier_player = player(ClanRole.SOLDIER)
    soldier = ClanMembership.objects.create(
        player=soldier_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(minutes=1),
        config_version=config,
    )
    activate_membership(soldier)
    settle_clan_reward(
        membership=soldier,
        source_type="CLAN_ACTIVITY",
        source_id="bonus",
        gross_amount=100,
        idempotency_key="bonus-source",
        occurred_at=now,
        resolved_config=resolved,
        metadata={"captain_bonus_eligible_at_source": True},
    )
    bonus = RewardLedger.objects.get(source_type="CAPTAIN_BONUS")
    assert bonus.player_id == cs.captain_id and bonus.player_amount == 2
    assert bonus.clan_treasury_amount == 0 and not hasattr(bonus, "outbox")


@pytest.mark.django_db
def test_remainders_are_isolated_by_config_version(clan_world, phase4_config_factory) -> None:  # type: ignore[no-untyped-def]
    from apps.game_config.services import resolve_game_config_version

    now, _, _, resolved_a, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate_membership(member)
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="config-a",
        gross_amount=1,
        idempotency_key="config-a",
        occurred_at=now,
        resolved_config=resolved_a,
    )
    config_b = phase4_config_factory(
        effective_at=now + timedelta(seconds=1),
        version="phase8-cutover",
        mutate=lambda p: p["clan"]["contribution"].update(
            gross_points_to_contribution_numerator=1, gross_points_to_contribution_denominator=3
        ),
    )
    resolved_b = resolve_game_config_version(config_b.pk)
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="config-b",
        gross_amount=1,
        idempotency_key="config-b",
        occurred_at=now + timedelta(seconds=1),
        resolved_config=resolved_b,
    )
    rows = list(
        ClanEconomicAccumulator.objects.filter(clan_season=cs).order_by("config_version_id")
    )
    assert len(rows) == 2
    assert rows[0].contribution_remainder != rows[1].contribution_remainder
