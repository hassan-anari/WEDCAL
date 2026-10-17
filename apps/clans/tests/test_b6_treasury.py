# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta
from functools import partial

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.clans.economy import settle_clan_reward
from apps.clans.models import (
    ClanEconomicAccumulator,
    ClanEconomicSettlement,
    ClanMembershipEvent,
    ClanSeasonEvent,
)
from apps.clans.tests.test_phase8_concurrency import parallel
from apps.game_config.services import resolve_game_config_version
from apps.rewards.models import RewardLedger

pytestmark = pytest.mark.django_db(transaction=True)


def setup(clan_world):
    now, config, season, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    return now, config, season, resolved, cs, member


def settle(member, resolved, at, key, gross):
    return settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id=key,
        gross_amount=gross,
        idempotency_key=key,
        occurred_at=at,
        resolved_config=resolved,
    )


def test_owner_fractional_sequence_replay_conservation_and_rollover(clan_world):
    now, config, _, resolved, cs, member = setup(clan_world)
    results = []
    for index, gross in enumerate([2, 1, 4, 3]):
        row, created = settle(member, resolved, now, f"owner-{index}", gross)
        assert created
        results.append(row)
        replay, created = settle(member, resolved, now, f"owner-{index}", gross)
        assert replay.pk == row.pk and not created
        assert row.player_amount + row.treasury_amount == gross
        assert row.reward.clan_treasury_amount == row.treasury_amount
    assert [r.treasury_amount for r in results] == [0, 0, 0, 1]
    assert [r.metadata["treasury_remainder_after"] for r in results] == [2000, 3000, 7000, 0]
    cs.refresh_from_db()
    assert cs.treasury_balance == cs.treasury_earned_total == 1
    assert cs.contribution_total == cs.competitive_score == 10 and cs.hq_xp == 2
    assert cs.treasury_entries.get().amount == 1
    assert cs.treasury_entries.get().metadata["treasury_policy"] == "B6_FRACTIONAL_V1"
    for index in range(15):
        settle(member, resolved, now, f"ones-{index}", 1)
    accumulator = ClanEconomicAccumulator.objects.get(clan_season=cs, config_version=config)
    assert accumulator.treasury_remainder == 5000
    cs.refresh_from_db()
    assert cs.treasury_balance == 2 and cs.contribution_total == 25 and cs.hq_xp == 5


def test_concurrent_small_sources_share_one_atomic_accumulator(clan_world):
    now, config, _, resolved, cs, member = setup(clan_world)
    results = parallel(
        [partial(settle, member, resolved, now, f"concurrent-{i}", 1) for i in range(12)]
    )
    assert all(not isinstance(result, Exception) for result in results), results
    assert sum(result[0].treasury_amount for result in results) == 1
    assert all(r[0].player_amount + r[0].treasury_amount == 1 for r in results)
    assert (
        ClanEconomicAccumulator.objects.get(
            clan_season=cs, config_version=config
        ).treasury_remainder
        == 2000
    )
    cs.refresh_from_db()
    assert cs.treasury_balance == 1 and cs.contribution_total == 12
    retried = parallel(
        [partial(settle, member, resolved, now, "concurrent-0", 1) for _ in range(2)]
    )
    assert all(not result[1] for result in retried)
    assert ClanEconomicSettlement.objects.count() == 12


def test_config_rates_and_versions_never_mix_fractions(clan_world, phase4_config_factory):
    now, old, _, resolved, cs, member = setup(clan_world)
    settle(member, resolved, now, "old-nine", 9)
    new = phase4_config_factory(
        version="b6-quarter",
        effective_at=now + timedelta(seconds=1),
        mutate=lambda p: p["clan"]["treasury"].update(split_bps=2500),
    )
    quarter = resolve_game_config_version(new.pk)
    rows = [
        settle(member, quarter, now + timedelta(seconds=2), f"quarter-{i}", 1)[0] for i in range(4)
    ]
    assert [r.treasury_amount for r in rows] == [0, 0, 0, 1]
    assert all(r.player_amount + r.treasury_amount == r.gross_amount for r in rows)
    assert (
        ClanEconomicAccumulator.objects.get(clan_season=cs, config_version=old).treasury_remainder
        == 9000
    )
    assert (
        ClanEconomicAccumulator.objects.get(clan_season=cs, config_version=new).treasury_remainder
        == 0
    )
    # A delayed source pinned to the old published config continues its own carry.
    row, _ = settle(member, resolved, now, "old-delayed-one", 1)
    assert row.treasury_amount == 1
    assert (
        ClanEconomicAccumulator.objects.get(clan_season=cs, config_version=old).treasury_remainder
        == 0
    )


def test_rollback_and_worker_retry_cannot_consume_or_duplicate_carry(clan_world, monkeypatch):
    now, config, _, resolved, cs, member = setup(clan_world)
    settle(member, resolved, now, "initial-nine", 9)

    def fail(*_, **__):
        raise RuntimeError("synthetic transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr("apps.clans.economy.settle_reward", fail)
        with pytest.raises(RuntimeError):
            settle(member, resolved, now, "retry-one", 1)
    assert (
        ClanEconomicAccumulator.objects.get(
            clan_season=cs, config_version=config
        ).treasury_remainder
        == 9000
    )
    assert not cs.treasury_entries.exists()
    assert not ClanEconomicSettlement.objects.filter(source_id="retry-one").exists()
    first, created = settle(member, resolved, now, "retry-one", 1)
    again, replayed = settle(member, resolved, now, "retry-one", 1)
    assert created and not replayed and first.pk == again.pk and first.treasury_amount == 1
    assert cs.treasury_entries.count() == 1


def test_historical_settlement_replay_does_not_backfill_fraction(clan_world):
    now, config, _, resolved, cs, member = setup(clan_world)
    # Durable pre-cutover fixture: its fraction was already included in player_amount.
    historical = ClanEconomicSettlement.objects.create(
        clan_season=cs,
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="historical",
        gross_amount=2,
        player_amount=2,
        treasury_amount=0,
        contribution_amount=2,
        config_version=config,
        idempotency_key="historical",
        occurred_at=now,
        metadata={},
    )
    before = (historical.player_amount, historical.treasury_amount, historical.metadata)
    replay, created = settle(member, resolved, now, "historical", 2)
    assert not created and (replay.player_amount, replay.treasury_amount, replay.metadata) == before
    assert not ClanEconomicAccumulator.objects.filter(clan_season=cs).exists()
    for index in range(8):
        settle(member, resolved, now, f"prospective-{index}", 1)
    assert not cs.treasury_entries.exists()  # Old gross 2 was not silently added to carry.
    assert ClanEconomicAccumulator.objects.get(clan_season=cs).treasury_remainder == 8000
    historical.refresh_from_db()
    assert (historical.player_amount, historical.treasury_amount, historical.metadata) == before


def test_paused_home_and_closed_boundaries_leave_accumulator_unchanged(clan_world):
    now, config, season, resolved, cs, member = setup(clan_world)
    settle(member, resolved, now, "safe-nine", 9)
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind="BOT_ADMIN_LOST", occurred_at=now + timedelta(seconds=1)
    )
    for at in [now + timedelta(seconds=2), season.ends_at]:
        with pytest.raises(ValidationError), transaction.atomic():
            settle(member, resolved, at, f"paused-{at.isoformat()}", 1)
    assert (
        ClanEconomicAccumulator.objects.get(
            clan_season=cs, config_version=config
        ).treasury_remainder
        == 9000
    )
    assert ClanEconomicSettlement.objects.count() == RewardLedger.objects.count() == 1
    assert not cs.treasury_entries.exists()


def test_zero_player_rollover_keeps_canonical_ledger_and_ranking_tie(clan_world):
    from apps.progression.models import PlayerProgression
    from apps.rewards.services import InvalidReward, settle_reward
    from apps.seasons.models import PlayerSeason

    now, _, _, resolved, cs, member = setup(clan_world)
    settle(member, resolved, now, "tie-before", 9)
    prior = PlayerSeason.objects.get(player=member.player, season=cs.season)
    lifetime = PlayerProgression.objects.get(player=member.player).lifetime_points
    row, _ = settle(member, resolved, now + timedelta(seconds=1), "treasury-only", 1)
    assert row.player_amount == row.reward.player_amount == 0
    assert row.treasury_amount == row.reward.clan_treasury_amount == 1
    assert row.reward.gross_amount == 1 and row.reward.outbox
    current = PlayerSeason.objects.get(pk=prior.pk)
    assert (current.season_points, current.score_reached_at, current.ranking_generation) == (
        prior.season_points,
        prior.score_reached_at,
        prior.ranking_generation,
    )
    assert PlayerProgression.objects.get(player=member.player).lifetime_points == lifetime
    for source, context, flag in [("DAILY", "DAILY", True), ("CLAN_ACTIVITY", "CLAN_HOME", False)]:
        with pytest.raises(InvalidReward):
            settle_reward(
                player=member.player,
                source_type=source,
                source_id="not-permitted",
                context=context,
                gross_amount=1,
                player_amount=0,
                clan_treasury_amount=1,
                idempotency_key=f"zero-invalid-{source}",
                occurred_at=now,
                resolved_config=resolved,
                allow_treasury_only_split=flag,
            )


def test_zero_net_still_enforces_canonical_moderation_and_rolls_back(clan_world):
    from apps.players.models import Player
    from apps.rewards.services import InvalidReward

    now, _, _, resolved, cs, member = setup(clan_world)
    settle(member, resolved, now, "moderation-nine", 9)
    Player.objects.filter(pk=member.player_id).update(moderation_state="BANNED")
    with pytest.raises(InvalidReward):
        settle(member, resolved, now, "moderation-zero", 1)
    assert ClanEconomicAccumulator.objects.get(clan_season=cs).treasury_remainder == 9000
    assert not cs.treasury_entries.exists()
    assert ClanEconomicSettlement.objects.count() == 1
