import importlib
from datetime import timedelta

import pytest
from django.apps import apps as django_apps

from apps.clans.economy import settle_clan_reward
from apps.clans.models import ClanEconomicSettlement


@pytest.mark.django_db
def test_clan_tie_provenance_is_independent_of_source_processing_order(  # type: ignore[no-untyped-def]
    clan_world, monkeypatch
) -> None:
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    now, _, _, resolved, _, clan = clan_world
    clan_season = clan()
    member = clan_season.memberships.get()
    for source, occurred in (("later", now), ("earlier", now - timedelta(hours=1))):
        settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id=source,
            gross_amount=10,
            idempotency_key=f"tie-clan:{source}",
            occurred_at=occurred,
            resolved_config=resolved,
            source_facts_verified=True,
        )
    clan_season.refresh_from_db()
    assert clan_season.score_reached_at == now


@pytest.mark.django_db
def test_historical_tie_backfill_ignores_non_score_settlements(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, _, _, clan = clan_world
    clan_season = clan()
    member = clan_season.memberships.get()
    score_time = now - timedelta(hours=2)
    for source_id, score, occurred_at in (
        ("score", 7, score_time),
        ("zero-score", 0, now - timedelta(hours=1)),
    ):
        ClanEconomicSettlement.objects.create(
            clan_season=clan_season,
            membership=member,
            source_type="TEST",
            source_id=source_id,
            gross_amount=10,
            player_amount=10,
            treasury_amount=0,
            contribution_amount=10,
            clan_score_amount=score,
            config_version=config,
            idempotency_key=f"backfill:{source_id}",
            occurred_at=occurred_at,
        )
    clan_season.competitive_score = 7
    clan_season.score_reached_at = None
    clan_season.save(update_fields=("competitive_score", "score_reached_at"))

    migration = importlib.import_module(
        "apps.arena.migrations.0004_phase9_correction_backfill_and_guards"
    )
    migration.backfill_tie_provenance(django_apps, None)

    clan_season.refresh_from_db()
    assert clan_season.score_reached_at == score_time
