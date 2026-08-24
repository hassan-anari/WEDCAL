from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Thread, current_thread
from time import monotonic, sleep
from typing import Any
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import (
    GameConfigUnavailable,
    parse_progression,
    publish_game_config,
    resolve_effective_game_config,
)
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression, refresh_player_progression
from apps.rewards.models import RewardLedger
from apps.rewards.services import (
    IdempotencyConflict,
    InvalidReward,
    rebuild_player_reward_projections,
    settle_player_reward,
)
from apps.seasons.models import PlayerSeason, Season
from apps.seasons.services import NoActiveSeason, activate_season, resolve_current_season


def payload(level_two: int = 100) -> dict[str, Any]:
    return {
        "progression": {
            "levels": [
                {"level": 1, "min_lifetime_points": 0},
                {"level": 2, "min_lifetime_points": level_two},
                {"level": 3, "min_lifetime_points": level_two * 2},
            ],
            "dexo_stages": [
                {"key": "egg", "min_level": 1},
                {"key": "adult", "min_level": 3},
            ],
        }
    }


def player(number: int = 1) -> Player:
    now = timezone.now()
    return Player.objects.create(
        telegram_user_id=number,
        first_seen_at=now,
        last_seen_at=now,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )


def published(
    version: str = "v1", *, effective_at: datetime | None = None, level_two: int = 100
) -> GameConfigVersion:
    config = GameConfigVersion.objects.create(
        version=version,
        effective_at=effective_at or timezone.now() - timedelta(days=2),
        payload=payload(level_two),
    )
    return publish_game_config(config)


def active_season(
    config: GameConfigVersion, code: str = "s1", *, at: datetime | None = None
) -> Season:
    at = at or timezone.now()
    season = Season.objects.create(
        code=code,
        title=code,
        starts_at=at - timedelta(days=1),
        ends_at=at + timedelta(days=1),
        baseline_config=config,
    )
    return activate_season(season)


@pytest.mark.django_db
def test_config_validation_publish_resolution_and_immutability() -> None:
    with pytest.raises(ValidationError):
        parse_progression(
            {
                "progression": {
                    "levels": [{"level": 1, "min_lifetime_points": -1}],
                    "dexo_stages": [],
                }
            }
        )
    config = published()
    assert resolve_effective_game_config(timezone.now()).id == config.pk
    config.payload = payload(50)
    with pytest.raises(ValidationError, match="immutable"):
        config.save()
    future = GameConfigVersion.objects.create(
        version="v2", effective_at=timezone.now() + timedelta(days=1), payload=payload(50)
    )
    publish_game_config(future)
    assert resolve_effective_game_config(timezone.now()).id == config.pk
    with pytest.raises(GameConfigUnavailable):
        resolve_effective_game_config(config.effective_at - timedelta(seconds=1))


@pytest.mark.django_db
def test_config_publication_cannot_rewrite_established_history() -> None:
    initial_effective_at = datetime(2026, 1, 1, tzinfo=UTC)
    first_publication = datetime(2026, 1, 2, tzinfo=UTC)
    historical_timestamp = datetime(2026, 1, 10, tzinfo=UTC)
    first = GameConfigVersion.objects.create(
        version="history-v1", effective_at=initial_effective_at, payload=payload()
    )
    publish_game_config(first, published_at=first_publication)

    second_publication = datetime(2026, 2, 1, tzinfo=UTC)
    retroactive = GameConfigVersion.objects.create(
        version="history-v2",
        effective_at=datetime(2026, 1, 5, tzinfo=UTC),
        payload=payload(50),
    )
    with pytest.raises(ValidationError, match="before publication"):
        publish_game_config(retroactive, published_at=second_publication)

    assert resolve_effective_game_config(historical_timestamp).id == first.pk
    retroactive.refresh_from_db()
    assert retroactive.status == GameConfigVersion.Status.DRAFT


@pytest.mark.django_db
def test_scheduled_config_changes_resolution_only_at_effective_time() -> None:
    first = GameConfigVersion.objects.create(
        version="scheduled-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload(),
    )
    publish_game_config(first, published_at=datetime(2026, 1, 2, tzinfo=UTC))
    effective_at = datetime(2026, 2, 1, tzinfo=UTC)
    second = GameConfigVersion.objects.create(
        version="scheduled-v2", effective_at=effective_at, payload=payload(50)
    )
    publish_game_config(second, published_at=datetime(2026, 1, 15, tzinfo=UTC))

    assert resolve_effective_game_config(effective_at - timedelta(microseconds=1)).id == first.pk
    assert resolve_effective_game_config(effective_at).id == second.pk


@pytest.mark.django_db
def test_season_constraints_activation_and_resolver() -> None:
    config = published()
    now = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        Season.objects.create(
            code="bad", title="bad", starts_at=now, ends_at=now, baseline_config=config
        )
    season = active_season(config)
    assert resolve_current_season(at=now) == season
    other = Season.objects.create(
        code="other",
        title="other",
        starts_at=now,
        ends_at=now + timedelta(days=1),
        baseline_config=config,
    )
    with pytest.raises(ValidationError):
        activate_season(other)
    assert resolve_current_season(at=now + timedelta(days=2), required=False) is None


@pytest.mark.django_db
def test_progression_integer_boundaries_and_stages() -> None:
    progression = parse_progression(payload())
    assert calculate_progression(0, progression).level == 1
    assert calculate_progression(99, progression).level == 1
    assert calculate_progression(100, progression).level == 2
    maximum = calculate_progression(999, progression)
    assert maximum.level == 3
    assert maximum.next_threshold is None
    assert maximum.dexo_stage == "adult"
    with pytest.raises(ValueError):
        calculate_progression(1.5, progression)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_reward_atomic_idempotent_conflict_and_rollback() -> None:
    config = published()
    active_season(config)
    person = player()
    resolved = resolve_effective_game_config(timezone.now())
    entry, created = settle_player_reward(
        player=person,
        source_type="TEST",
        source_id="a",
        context="TEST",
        amount=100,
        idempotency_key="one",
        occurred_at=timezone.now(),
        resolved_config=resolved,
    )
    duplicate, duplicate_created = settle_player_reward(
        player=person,
        source_type="TEST",
        source_id="a",
        context="TEST",
        amount=100,
        idempotency_key="one",
        occurred_at=entry.occurred_at,
        resolved_config=resolved,
    )
    assert created is True and duplicate_created is False and duplicate.pk == entry.pk
    assert PlayerProgression.objects.get(player=person).lifetime_points == 100
    assert PlayerSeason.objects.get(player=person).season_points == 100
    with pytest.raises(IdempotencyConflict):
        settle_player_reward(
            player=person,
            source_type="TEST",
            source_id="different",
            context="TEST",
            amount=100,
            idempotency_key="one",
            occurred_at=entry.occurred_at,
            resolved_config=resolved,
        )
    with pytest.raises(RuntimeError):
        settle_player_reward(
            player=person,
            source_type="TEST",
            source_id="rollback",
            context="TEST",
            amount=5,
            idempotency_key="rollback",
            occurred_at=timezone.now(),
            resolved_config=resolved,
            after_ledger_hook=Mock(side_effect=RuntimeError("boom")),
        )
    assert not RewardLedger.objects.filter(idempotency_key="rollback").exists()
    assert PlayerProgression.objects.get(player=person).lifetime_points == 100


@pytest.mark.django_db
def test_reward_rejects_invalid_amount_season_window_and_config() -> None:
    published()
    person = player()
    resolved = resolve_effective_game_config(timezone.now())
    with pytest.raises(InvalidReward):
        settle_player_reward(
            player=person,
            source_type="T",
            source_id="x",
            context="T",
            amount=0,
            idempotency_key="x",
            occurred_at=timezone.now(),
            resolved_config=resolved,
        )
    with pytest.raises(NoActiveSeason):
        settle_player_reward(
            player=person,
            source_type="T",
            source_id="x",
            context="T",
            amount=1,
            idempotency_key="x",
            occurred_at=timezone.now(),
            resolved_config=resolved,
        )
    assert RewardLedger.objects.count() == 0


@pytest.mark.django_db
def test_multiseason_and_reconciliation() -> None:
    config = published()
    resolved = resolve_effective_game_config(timezone.now())
    person = player()
    for index, amount in enumerate((12_000, 8_000, 3_000), 1):
        season = active_season(config, f"s{index}")
        settle_player_reward(
            player=person,
            source_type="TEST",
            source_id=str(index),
            context="TEST",
            amount=amount,
            idempotency_key=str(index),
            occurred_at=timezone.now(),
            resolved_config=resolved,
        )
        if index < 3:
            season.status = Season.Status.CLOSED
            season.save(update_fields=["status"])
    assert PlayerProgression.objects.get(player=person).lifetime_points == 23_000
    assert list(
        PlayerSeason.objects.filter(player=person)
        .order_by("season_id")
        .values_list("season_points", flat=True)
    ) == [12_000, 8_000, 3_000]
    PlayerProgression.objects.filter(player=person).update(lifetime_points=1, level=1)
    PlayerSeason.objects.filter(player=person).update(season_points=1)
    rebuilt = rebuild_player_reward_projections(person, resolved)
    assert rebuilt.lifetime_points == 23_000
    assert list(
        PlayerSeason.objects.filter(player=person)
        .order_by("season_id")
        .values_list("season_points", flat=True)
    ) == [12_000, 8_000, 3_000]


@pytest.mark.django_db(transaction=True)
def test_concurrent_distinct_rewards_do_not_lose_updates() -> None:
    config = published()
    active_season(config)
    person = player()
    resolved = resolve_effective_game_config(timezone.now())
    barrier = Barrier(2)
    errors: list[BaseException] = []

    def run(key: str) -> None:
        close_old_connections()
        try:
            barrier.wait()
            settle_player_reward(
                player=person,
                source_type="TEST",
                source_id=key,
                context="TEST",
                amount=10,
                idempotency_key=key,
                occurred_at=timezone.now(),
                resolved_config=resolved,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert RewardLedger.objects.count() == 2
    assert PlayerProgression.objects.get(player=person).lifetime_points == 20
    assert PlayerSeason.objects.get(player=person).season_points == 20


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_key_settles_once_without_database_error() -> None:
    config = published()
    active_season(config)
    person = player()
    resolved = resolve_effective_game_config(timezone.now())
    occurred_at = timezone.now()
    barrier = Barrier(2)
    results: list[tuple[int, bool]] = []
    errors: list[BaseException] = []

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait()
            entry, created = settle_player_reward(
                player=person,
                source_type="TEST",
                source_id="same-source",
                context="TEST",
                amount=10,
                idempotency_key="same-key",
                occurred_at=occurred_at,
                resolved_config=resolved,
            )
            results.append((entry.pk, created))
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert len({entry_id for entry_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]
    assert RewardLedger.objects.filter(idempotency_key="same-key").count() == 1
    assert PlayerProgression.objects.get(player=person).lifetime_points == 10
    assert PlayerSeason.objects.get(player=person).season_points == 10


@pytest.mark.django_db(transaction=True)
def test_concurrent_global_key_conflict_is_a_domain_error() -> None:
    config = published()
    active_season(config)
    players = (player(1), player(2))
    resolved = resolve_effective_game_config(timezone.now())
    occurred_at = timezone.now()
    barrier = Barrier(2)
    successes: list[int] = []
    errors: list[BaseException] = []

    def run(person: Player) -> None:
        close_old_connections()
        try:
            barrier.wait()
            entry, _ = settle_player_reward(
                player=person,
                source_type="TEST",
                source_id="global-source",
                context="TEST",
                amount=10,
                idempotency_key="global-key",
                occurred_at=occurred_at,
                resolved_config=resolved,
            )
            successes.append(entry.player_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run, args=(person,)) for person in players]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], IdempotencyConflict)
    assert RewardLedger.objects.filter(idempotency_key="global-key").count() == 1
    assert (
        sum(
            PlayerProgression.objects.filter(player__in=players).values_list(
                "lifetime_points", flat=True
            )
        )
        == 10
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_refresh_cannot_overwrite_reward_progression() -> None:
    config = published()
    active_season(config)
    person = player()
    resolved = resolve_effective_game_config(timezone.now())
    projection, _ = refresh_player_progression(person, resolved)
    projection.lifetime_points = 90
    projection.save(update_fields=["lifetime_points"])
    refresh_calculating = Event()
    allow_refresh = Event()
    original_calculator = calculate_progression
    errors: list[BaseException] = []

    def controlled_calculator(points: int, progression_config: Any) -> Any:
        if current_thread().name == "projection-refresh":
            refresh_calculating.set()
            assert allow_refresh.wait(timeout=5)
        return original_calculator(points, progression_config)

    def refresh() -> None:
        close_old_connections()
        try:
            refresh_player_progression(person, resolved, force=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    def reward() -> None:
        close_old_connections()
        try:
            settle_player_reward(
                player=person,
                source_type="TEST",
                source_id="threshold",
                context="TEST",
                amount=10,
                idempotency_key="threshold",
                occurred_at=timezone.now(),
                resolved_config=resolved,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    with patch(
        "apps.progression.services.calculate_progression", side_effect=controlled_calculator
    ):
        refresh_thread = Thread(target=refresh, name="projection-refresh")
        reward_thread = Thread(target=reward, name="reward-settlement")
        refresh_thread.start()
        assert refresh_calculating.wait(timeout=5)
        reward_thread.start()
        deadline = monotonic() + 0.5
        while (
            monotonic() < deadline
            and not RewardLedger.objects.filter(idempotency_key="threshold").exists()
        ):
            sleep(0.01)
        allow_refresh.set()
        refresh_thread.join()
        reward_thread.join()

    assert errors == []
    final = PlayerProgression.objects.get(player=person)
    assert final.lifetime_points == 100
    assert final.level == 2
    assert final.dexo_stage == "egg"
    assert final.config_version_id == config.pk


@pytest.mark.django_db
def test_stale_progression_refresh() -> None:
    old = published()
    person = player()
    resolved_old = resolve_effective_game_config(timezone.now())
    projection, _ = refresh_player_progression(person, resolved_old)
    projection.lifetime_points = 60
    projection.save(update_fields=["lifetime_points"])
    new = GameConfigVersion.objects.create(
        version="v2", effective_at=timezone.now() + timedelta(seconds=1), payload=payload(50)
    )
    publish_game_config(new)
    resolved_new = resolve_effective_game_config(new.effective_at + timedelta(seconds=1))
    projection, result = refresh_player_progression(person, resolved_new)
    assert old.pk != projection.config_version_id == new.pk
    assert result.level == 2 and projection.lifetime_points == 60


@pytest.mark.django_db
def test_bootstrap_and_history_are_authenticated_and_private(
    client: Client, settings: Any, init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    config = published(effective_at=datetime.now(tz=UTC) - timedelta(days=1))
    active_season(config)
    raw = init_data_factory(user={"id": 42, "first_name": "Ada"})
    auth = {"Authorization": f"tma {raw}"}
    bootstrap_response = client.get("/api/v1/bootstrap", headers=auth)
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["player"]["telegram_user_id"] == 42
    person = Player.objects.get(telegram_user_id=42)
    settle_player_reward(
        player=person,
        source_type="TEST",
        source_id="safe",
        context="TEST",
        amount=7,
        idempotency_key="secret",
        occurred_at=timezone.now(),
        resolved_config=resolve_effective_game_config(timezone.now()),
        metadata={"private": True},
    )
    other = player(99)
    RewardLedger.objects.filter(player=person).update(metadata={"private": True})
    history = client.get("/api/v1/rewards/history?limit=1", headers=auth)
    assert history.status_code == 200
    body = history.json()
    assert len(body["items"]) == 1
    assert "metadata" not in body["items"][0] and "idempotency_key" not in body["items"][0]
    assert client.get("/api/v1/bootstrap").status_code == 401
    assert other.telegram_user_id != body["items"][0].get("telegram_user_id")
