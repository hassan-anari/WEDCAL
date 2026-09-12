from datetime import timedelta

import pytest

from apps.clans.economy import settle_clan_reward


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
