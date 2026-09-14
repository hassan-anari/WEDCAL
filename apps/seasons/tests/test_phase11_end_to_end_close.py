from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.arena.adjustments import record_adjustment, season_adjustment_totals
from apps.arena.epochs import freeze_epoch, initialize_epochs, settle_epoch
from apps.arena.models import (
    DlxAdjustment,
    DlxAllocation,
    EconomicEpoch,
    RankingProjectionEvent,
    SeasonDlxResidualEvent,
)
from apps.arena.qualification import record_qualification_decision
from apps.arena.services import apply_projection_event
from apps.clans.economy import settle_clan_reward
from apps.clans.models import (
    Clan,
    ClanMembership,
    ClanRole,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.game_config.services import resolve_game_config_version
from apps.profile_collection.models import PlayerMedalAward
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.rewards.services import settle_player_reward
from apps.rewards.tasks import consume_outbox
from apps.seasons.barrier import SeasonFinalizationEstablished
from apps.seasons.close import advance_close, start_or_resume_close
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeasonFinalStanding,
    SeasonCloseRun,
)
from apps.telegram.models import TelegramGroup


@pytest.mark.django_db(transaction=True)
def test_complete_six_epoch_close_failure_resume_and_barrier(
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
    monkeypatch: Any,
) -> None:
    now = timezone.now()
    start = now - timedelta(days=46)
    config = phase4_config_factory(effective_at=start - timedelta(days=1))
    season = season_factory(config, starts_at=start, ends_at=start + timedelta(days=45))
    resolved = resolve_game_config_version(config.pk)
    player = player_factory(13101)
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    monkeypatch.setattr("apps.arena.services.dispatch_projection_safely", lambda _pk: None)
    monkeypatch.setattr("apps.seasons.close._enqueue", lambda *_args, **_kwargs: None)

    reward, created = settle_player_reward(
        player=player,
        source_type="STAGE2_CLOSE",
        source_id="writer-before-barrier",
        context="TEST",
        amount=6000,
        idempotency_key="stage2-close-player",
        occurred_at=start + timedelta(days=1),
        resolved_config=resolved,
        create_settlement_outbox=False,
    )
    assert created
    apply_projection_event(RankingProjectionEvent.objects.get(source_id=f"reward:{reward.pk}"))

    group = TelegramGroup.objects.create(
        telegram_chat_id=-13101,
        chat_type="supergroup",
        title="Stage 2 close",
        first_seen_at=start,
        last_seen_at=start,
    )
    clan = Clan.objects.create(
        telegram_group=group,
        name="Stage Two",
        tag="S2T",
        normalized_tag="s2t",
        creator=player,
    )
    assert resolved.clan is not None
    clan_season = ClanSeason.objects.create(
        clan=clan,
        season=season,
        captain=player,
        base_capacity=resolved.clan.hq_curve[0].base_capacity,
        config_version=config,
    )
    PlayerSeasonClanState.objects.create(player=player, season=season, role=ClanRole.CAPTAIN)
    membership = ClanMembership.objects.create(
        player=player,
        season=season,
        clan_season=clan_season,
        role=ClanRole.CAPTAIN,
        started_at=start,
        config_version=config,
    )
    settlement, clan_created = settle_clan_reward(
        membership=membership,
        source_type="CLAN_ACTIVITY",
        source_id="writer-before-barrier",
        gross_amount=100,
        idempotency_key="stage2-close-clan",
        occurred_at=start + timedelta(days=1),
        resolved_config=resolved,
        source_facts_verified=True,
    )
    assert clan_created and settlement.clan_score_amount > 0
    for outbox in RewardSettlementOutbox.objects.filter(state="PENDING"):
        consume_outbox(outbox.pk)
    for event in RankingProjectionEvent.objects.filter(processed_at__isnull=True):
        apply_projection_event(event)

    assert resolved.dlx_distribution is not None
    predicate = resolved.dlx_distribution.qualified_player_predicate
    record_qualification_decision(
        player=player,
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        decision="QUALIFIED",
        effective_at=start,
        reason_code="STAGE2_CLOSE",
        source="STAGE2_TEST",
        source_id="complete-close",
    )

    epochs = initialize_epochs(season)
    assert [row.epoch_number for row in epochs] == [1, 2, 3, 4, 5, 6]
    for epoch in epochs:
        frozen = freeze_epoch(epoch, at=epoch.ends_at)
        categories = list(frozen.category_budgets.values_list("category", flat=True))

        def allocate_one(_budget: Any, snapshots: list[Any]) -> dict[int, int]:
            return {snapshots[0].player_id: 1}

        settled = settle_epoch(
            frozen,
            strategies={category: allocate_one for category in categories},
            at=now,
        )
        assert settled.state == EconomicEpoch.State.SETTLED
    assert EconomicEpoch.objects.filter(season=season).count() == 6
    allocation = DlxAllocation.objects.filter(season=season).first()
    assert allocation is not None
    record_adjustment(
        allocation=allocation,
        kind=DlxAdjustment.Kind.HOLD,
        amount=1,
        reason_code="STAGE2_REVIEW",
        idempotency_key="stage2-close-hold",
        system_actor="stage2-test",
    )
    assert season_adjustment_totals(season)["held"] == 1

    run = start_or_resume_close(
        season,
        actor=None,
        service_actor="stage2-test",
        reason="end-to-end close verification",
    )
    assert advance_close(run.pk) == SeasonCloseRun.Stage.DRAINING
    assert advance_close(run.pk) == SeasonCloseRun.Stage.RECONCILING
    run.refresh_from_db()
    assert run.barrier_established_at is not None
    with pytest.raises(SeasonFinalizationEstablished):
        settle_player_reward(
            player=player,
            source_type="STAGE2_CLOSE",
            source_id="writer-after-barrier",
            context="TEST",
            amount=1,
            idempotency_key="stage2-close-after-barrier",
            occurred_at=start + timedelta(days=2),
            resolved_config=resolved,
            create_settlement_outbox=False,
        )
    assert advance_close(run.pk) == SeasonCloseRun.Stage.FREEZING_RANKINGS

    from apps.seasons import close as close_module

    original_freeze = close_module.freeze_standings
    monkeypatch.setattr(
        close_module,
        "freeze_standings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected freeze failure")),
    )
    with pytest.raises(RuntimeError, match="injected freeze failure"):
        advance_close(run.pk)
    run.refresh_from_db()
    assert run.stage == SeasonCloseRun.Stage.FAILED
    assert run.failure_stage == SeasonCloseRun.Stage.FREEZING_RANKINGS
    run = start_or_resume_close(
        season,
        actor=None,
        service_actor="stage2-test",
        reason="resume end-to-end close verification",
    )
    assert run.stage == SeasonCloseRun.Stage.FREEZING_RANKINGS
    assert run.retry_count == 1
    monkeypatch.setattr(close_module, "freeze_standings", original_freeze)

    for _ in range(10):
        stage = advance_close(run.pk)
        if stage == SeasonCloseRun.Stage.COMPLETED:
            break
    run.refresh_from_db()
    season.refresh_from_db()
    assert run.stage == SeasonCloseRun.Stage.COMPLETED
    assert season.status == season.Status.CLOSED
    expected_player_points = sum(
        RewardLedger.objects.filter(season=season).values_list("player_amount", flat=True)
    )
    assert (
        PlayerSeasonFinalStanding.objects.get(season=season).final_season_points
        == expected_player_points
    )
    assert (
        ClanSeasonFinalStanding.objects.get(season=season).final_competitive_score
        == settlement.clan_score_amount
    )
    assert PlayerMedalAward.objects.filter(player=player, source_season=season).exists()
    assert SeasonDlxResidualEvent.objects.filter(season=season).exists()
    assert not RewardLedger.objects.filter(idempotency_key="stage2-close-after-barrier").exists()
    assert RewardLedger.objects.filter(idempotency_key="stage2-close-player").count() == 1
