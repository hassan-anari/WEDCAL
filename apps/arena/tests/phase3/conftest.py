# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.arena.economics import bind_season_policy, prepare_epoch
from apps.arena.epochs import freeze_epoch, initialize_epochs
from apps.arena.models import EconomicPolicyVersion
from apps.arena.policy import digest, parse_policy
from apps.arena.readiness import evaluate_epoch_readiness
from apps.game_config.services import resolve_game_config_version
from apps.typing_game.models import TypingPhrase
from apps.typing_game.services import start_attempt, submit_attempt

START = datetime.now(UTC).replace(hour=22, minute=33, second=6, microsecond=352397) + timedelta(
    days=1
)
TEXT = "Dexo typing arena phrase"
POLICY = json.loads(
    (
        Path(__file__).resolve().parents[4] / "config/examples/season1.economic-policy.v1.json"
    ).read_text()
)


@pytest.fixture
def clock(monkeypatch):
    value = {"at": START}
    monkeypatch.setattr(timezone, "now", lambda: value["at"])
    return value


@pytest.fixture
def economic_world(
    phase4_config_factory, season_factory, player_factory, clock, monkeypatch, request
):
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)

    def configure(payload):
        if getattr(request, "param", None) == "v2":
            payload.update(
                json.loads(
                    (
                        Path(__file__).resolve().parents[4]
                        / "config/examples/season1.game-config.v2.json"
                    ).read_text()
                )
            )

    config = phase4_config_factory(effective_at=START - timedelta(days=3), mutate=configure)
    season = season_factory(config, starts_at=START)
    epochs = initialize_epochs(season)
    actor = User.objects.create_superuser(username="economic-reviewer", password="local-test-only")
    payload = copy.deepcopy(POLICY)
    document = EconomicPolicyVersion.objects.create(
        key="S1_ECONOMICS",
        version=1,
        payload=payload,
        checksum=digest(payload),
        approval_reference="SYNTHETIC_OWNER_APPROVAL",
    )
    bind_season_policy(
        season, document, actor=actor, evidence_reference="SYNTHETIC_SOURCE_COVERAGE"
    )
    TypingPhrase.objects.create(key="p3", version=1, text=TEXT, enabled=True)
    sequence = 0

    def player():
        nonlocal sequence
        sequence += 1
        row = player_factory(930000 + sequence)
        row.first_mini_app_at = START - timedelta(days=2)
        row.save(update_fields=["first_mini_app_at"])
        return row

    def typing(row, at, key=None):
        clock["at"] = at
        attempt, _ = start_attempt(row, key or f"p3:{row.pk}:{at.isoformat()}", at)
        clock["at"] = at + timedelta(seconds=5)
        return submit_attempt(row, attempt.public_id, TEXT, clock["at"])[0]

    def freeze(epoch):
        clock["at"] = max(clock["at"], epoch.ends_at)
        prepare_epoch(epoch)
        run = evaluate_epoch_readiness(
            epoch, evaluation_key=f"p3:{epoch.pk}:{epoch.evaluations.count()}"
        )
        assert run.status == "COMPLETE", run.blocking_reasons
        return freeze_epoch(epoch, at=clock["at"])

    return SimpleNamespace(
        config=config,
        resolved=resolve_game_config_version(config.pk),
        season=season,
        epochs=epochs,
        document=document,
        policy=parse_policy(payload),
        actor=actor,
        player=player,
        typing=typing,
        freeze=freeze,
        clock=clock,
    )
