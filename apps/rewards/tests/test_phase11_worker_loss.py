from typing import Any

import pytest
from django.db import transaction

from apps.game_config.services import resolve_game_config_version
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.rewards.services import settle_player_reward
from apps.rewards.tasks import consume_outbox


@pytest.mark.django_db
def test_worker_loss_before_commit_leaves_no_partial_economic_effect(
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
    monkeypatch: Any,
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory(12901)
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    with pytest.raises(RuntimeError, match="simulated worker loss"), transaction.atomic():
        settle_player_reward(
            player=player,
            source_type="STAGE2_WORKER_LOSS",
            source_id="before-commit",
            context="TEST",
            amount=10,
            idempotency_key="stage2-worker-loss-before",
            occurred_at=season.starts_at,
            resolved_config=resolve_game_config_version(config.pk),
        )
        raise RuntimeError("simulated worker loss")
    assert not RewardLedger.objects.filter(idempotency_key="stage2-worker-loss-before").exists()
    assert RewardSettlementOutbox.objects.count() == 0


@pytest.mark.django_db
def test_worker_loss_after_commit_duplicate_delivery_converges_once(
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
    monkeypatch: Any,
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory(12902)
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    reward, created = settle_player_reward(
        player=player,
        source_type="STAGE2_WORKER_LOSS",
        source_id="after-commit",
        context="TEST",
        amount=10,
        idempotency_key="stage2-worker-loss-after",
        occurred_at=season.starts_at,
        resolved_config=resolve_game_config_version(config.pk),
    )
    assert created
    assert consume_outbox(reward.outbox.pk)
    assert not consume_outbox(reward.outbox.pk)
    assert RewardLedger.objects.filter(idempotency_key="stage2-worker-loss-after").count() == 1
    reward.outbox.refresh_from_db()
    assert reward.outbox.state == RewardSettlementOutbox.State.PROCESSED
