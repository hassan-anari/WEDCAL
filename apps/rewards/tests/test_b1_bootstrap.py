from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from typing import Any, cast
from unittest.mock import patch

import pytest
from django.db import connection, connections
from django.test import Client
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.progression.services import refresh_player_progression
from apps.rewards.schemas import BootstrapOutput
from apps.rewards.services import settle_player_reward
from apps.seasons.models import Season


@pytest.fixture
def b1_world(
    settings: Any,
    telegram_bot_token: str,
    phase4_config_factory: Callable[..., GameConfigVersion],
    season_factory: Callable[..., Season],
) -> tuple[GameConfigVersion, Season]:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2), version="b1-config")
    season = season_factory(
        config, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=44)
    )
    return config, season


@pytest.mark.django_db
@pytest.mark.parametrize("scheme", ["tma", "TMA", "TmA   "])
def test_signed_bootstrap_is_typed_compatible_and_idempotent(
    client: Client,
    b1_world: tuple[GameConfigVersion, Season],
    init_data_factory: Callable[..., str],
    scheme: str,
) -> None:
    config, season = b1_world
    auth = {"Authorization": f"{scheme} {init_data_factory(user={'id': 42, 'first_name': 'Ada'})}"}
    response = client.get("/api/v1/bootstrap", headers={**auth, "X-Request-ID": "b1-bootstrap"})
    assert response.status_code == 200
    body = response.json()
    parsed = BootstrapOutput.model_validate(body)
    assert parsed.request_id == response["X-Request-ID"] == "b1-bootstrap"
    assert parsed.player.id == "42"
    assert parsed.player.telegram_user_id == 42
    assert parsed.season is not None and parsed.season.code == season.code
    assert parsed.progression.lifetime_points == parsed.season.season_points == 0
    assert parsed.config.version == config.version
    assert parsed.config.payload_checksum == config.payload_checksum
    assert parsed.config.progression.levels[0].min_lifetime_points == 0
    assert parsed.config.progression.clan_min_level == config.payload["clan"]["unlock"]["min_level"]
    assert parsed.features.daily == (parsed.daily is not None)
    assert parsed.features.missions == (parsed.missions is not None)
    assert parsed.features.typing == (parsed.typing is not None)
    assert parsed.features.clan is False
    assert parsed.gameplay.reward_earning_allowed is True
    assert response["Cache-Control"] == "private, no-store"
    assert "Authorization" in response["Vary"]

    # Current frontend's Zod contract accepts additions, but still requires these fields.
    for key in ("first_name", "last_name", "username"):
        assert isinstance(body["player"][key], str)
    assert {"daily", "typing", "missions", "referral", "network"} <= body.keys()
    assert isinstance(body["config"]["version"], str)
    assert isinstance(body["config"]["effective_at"], str)
    assert isinstance(body["season"]["starts_at"], str)
    second = client.get("/api/v1/bootstrap", headers=auth)
    assert second.status_code == 200
    assert second.json()["progression"] == body["progression"]
    assert Player.objects.filter(telegram_user_id=42).count() == 1
    assert PlayerProgression.objects.filter(player__telegram_user_id=42).count() == 1


@pytest.mark.django_db
def test_bootstrap_tracks_real_rewards_and_isolates_identity(
    client: Client,
    b1_world: tuple[GameConfigVersion, Season],
    init_data_factory: Callable[..., str],
) -> None:
    _, season = b1_world
    raw = init_data_factory(user={"id": 42, "first_name": "Ada"})
    auth = {"Authorization": f"tma {raw}"}
    assert client.get("/api/v1/bootstrap", headers=auth).status_code == 200
    now = timezone.now()
    settle_player_reward(
        player=Player.objects.get(telegram_user_id=42),
        source_type="TEST",
        source_id="b1",
        context="TEST",
        amount=2500,
        idempotency_key="b1:reward",
        occurred_at=now,
        resolved_config=resolve_effective_game_config(now),
    )
    body = client.get("/api/v1/bootstrap?player_id=999&telegram_user_id=999", headers=auth).json()
    assert body["player"]["id"] == "42"
    assert body["progression"]["lifetime_points"] == body["season"]["season_points"] == 2500
    assert body["progression"]["level"] == 5
    assert body["progression"]["current_threshold"] == 2500
    assert body["progression"]["points_into_level"] == 0
    assert body["progression"]["points_remaining"] == body["progression"]["next_threshold"] - 2500
    assert body["features"]["clan"] is True
    assert body["season"]["code"] == season.code
    other = client.get(
        "/api/v1/bootstrap", headers={"Authorization": f"tma {init_data_factory(user={'id': 999})}"}
    ).json()
    assert other["player"]["id"] == "999"
    assert other["progression"]["lifetime_points"] == other["season"]["season_points"] == 0
    assert (
        client.get(
            "/api/v1/rewards/history",
            headers={"Authorization": f"tma {init_data_factory(user={'id': 999})}"},
        ).json()["items"]
        == []
    )


@pytest.mark.django_db
def test_no_season_is_a_valid_initial_state(
    client: Client,
    settings: Any,
    telegram_bot_token: str,
    phase4_config_factory: Callable[..., GameConfigVersion],
    init_data_factory: Callable[..., str],
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    phase4_config_factory()
    response = client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION=f"tma {init_data_factory()}")
    assert response.status_code == 200
    body = BootstrapOutput.model_validate(response.json())
    assert all(value is None for value in (body.season, body.daily, body.typing, body.missions))
    assert body.features.season is body.features.base_tap is body.features.clan is False
    assert body.gameplay.reward_earning_allowed is False


@pytest.mark.django_db
def test_config_turnover_preserves_identity_and_lifetime_progression(
    client: Client,
    b1_world: tuple[GameConfigVersion, Season],
    init_data_factory: Callable[..., str],
    phase4_config_factory: Callable[..., GameConfigVersion],
) -> None:
    old_config, season = b1_world
    auth = {"Authorization": f"tma {init_data_factory()}"}
    assert client.get("/api/v1/bootstrap", headers=auth).status_code == 200
    player = Player.objects.get(telegram_user_id=42)
    now = timezone.now()
    settle_player_reward(
        player=player,
        source_type="TEST",
        source_id="b1-max-level",
        context="TEST",
        amount=200007,
        idempotency_key="b1:max-level",
        occurred_at=now,
        resolved_config=resolve_effective_game_config(now),
    )
    new_config = phase4_config_factory(
        effective_at=now - timedelta(seconds=1), version="b1-next-config"
    )
    response = client.get("/api/v1/bootstrap", headers=auth)
    assert response.status_code == 200
    body = BootstrapOutput.model_validate(response.json())
    assert body.config.version == new_config.version != old_config.version
    assert body.player.id == "42"
    assert body.season is not None and body.season.code == season.code
    assert body.progression.lifetime_points == body.season.season_points == 200007
    assert body.progression.level == 30
    assert body.progression.next_threshold is body.progression.points_remaining is None
    assert body.progression.points_into_level == 7
    assert body.progression.dexo_stage == "legend"
    assert PlayerProgression.objects.get(player=player).config_version == new_config


@pytest.mark.django_db
def test_missing_effective_config_returns_safe_initialization_error(
    client: Client,
    settings: Any,
    telegram_bot_token: str,
    init_data_factory: Callable[..., str],
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    response = client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION=f"tma {init_data_factory()}")
    assert response.status_code == 503
    assert response.json()["code"] == "INITIALIZATION_UNAVAILABLE"
    assert response.json()["request_id"] == response["X-Request-ID"]
    assert "No published" not in response.content.decode()
    # Provisioning succeeds before Bootstrap; a retry must reuse this identity.
    assert Player.objects.count() == 1
    assert PlayerProgression.objects.count() == 0


@pytest.mark.django_db
def test_disabled_features_follow_effective_config(
    client: Client,
    settings: Any,
    telegram_bot_token: str,
    phase4_config_factory: Callable[..., GameConfigVersion],
    season_factory: Callable[..., Season],
    init_data_factory: Callable[..., str],
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token

    def disable(payload: dict[str, Any]) -> None:
        for key in (
            "daily",
            "missions",
            "typing",
            "referral",
            "network_activity",
            "base_tap",
            "clan",
        ):
            payload[key]["enabled"] = False

    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2), mutate=disable)
    season_factory(config, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=44))
    response = client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION=f"tma {init_data_factory()}")
    assert response.status_code == 200
    body = response.json()
    for key in ("daily", "missions", "typing", "referral", "network"):
        assert body[key] is None and body["features"][key] is False
    assert body["features"]["base_tap"] is body["features"]["clan"] is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,allowed", [("FLAGGED", True), ("SUSPENDED", False), ("BANNED", False)]
)
def test_authentication_is_separate_from_gameplay_moderation(
    client: Client,
    b1_world: tuple[GameConfigVersion, Season],
    init_data_factory: Callable[..., str],
    state: str,
    allowed: bool,
) -> None:
    auth = {"Authorization": f"tma {init_data_factory()}"}
    assert client.get("/api/v1/bootstrap", headers=auth).status_code == 200
    Player.objects.filter(telegram_user_id=42).update(moderation_state=state)
    response = client.get("/api/v1/bootstrap", headers=auth)
    assert response.status_code == 200
    assert response.json()["gameplay"] == {
        "moderation_state": state,
        "reward_earning_allowed": allowed,
    }


@pytest.mark.django_db(transaction=True)
def test_concurrent_signed_launches_provision_one_canonical_player(
    settings: Any,
    telegram_bot_token: str,
    init_data_factory: Callable[..., str],
) -> None:
    assert connection.vendor == "postgresql", "Provisioning concurrency requires real PostgreSQL"
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    raw = init_data_factory(user={"id": 711001, "first_name": "Concurrent"})
    start = Barrier(4)

    def launch(_: int) -> dict[str, Any]:
        connections.close_all()
        try:
            start.wait(timeout=5)
            response = Client().post(
                "/api/v1/auth/telegram", {"init_data": raw}, content_type="application/json"
            )
            assert response.status_code == 200
            return cast(dict[str, Any], response.json())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=4) as executor:
        bodies = list(executor.map(launch, range(4)))
    assert sum(body["created"] for body in bodies) == 1
    assert {body["telegram_user_id"] for body in bodies} == {711001}
    assert Player.objects.filter(telegram_user_id=711001).count() == 1
    assert Player.objects.get(telegram_user_id=711001).first_mini_app_at is not None


@pytest.mark.django_db(transaction=True)
def test_bootstrap_cannot_mix_projections_during_concurrent_reward_commit(
    client: Client,
    b1_world: tuple[GameConfigVersion, Season],
    init_data_factory: Callable[..., str],
) -> None:
    assert connection.vendor == "postgresql", "Snapshot concurrency requires real PostgreSQL"
    auth = {"Authorization": f"tma {init_data_factory()}"}
    assert client.get("/api/v1/bootstrap", headers=auth).status_code == 200
    player = Player.objects.get(telegram_user_id=42)
    now = timezone.now()
    resolved = resolve_effective_game_config(now)
    read_lifetime = Event()
    continue_snapshot = Event()
    writer_ready = Event()
    writer_pid: list[int] = []

    def pause_after_lifetime(*args: Any, **kwargs: Any) -> Any:
        result = refresh_player_progression(*args, **kwargs)
        read_lifetime.set()
        assert continue_snapshot.wait(timeout=10)
        return result

    def snapshot() -> dict[str, Any]:
        connections.close_all()
        try:
            response = Client().get("/api/v1/bootstrap", headers=auth)
            assert response.status_code == 200
            return cast(dict[str, Any], response.json())
        finally:
            connections.close_all()

    def reward() -> None:
        connections.close_all()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(cursor.fetchone()[0])
            writer_ready.set()
            settle_player_reward(
                player=player,
                source_type="TEST",
                source_id="b1-race",
                context="TEST",
                amount=7,
                idempotency_key="b1:race",
                occurred_at=now,
                resolved_config=resolved,
            )
        finally:
            connections.close_all()

    with (
        patch(
            "apps.rewards.bootstrap.refresh_player_progression", side_effect=pause_after_lifetime
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        reading = executor.submit(snapshot)
        try:
            assert read_lifetime.wait(timeout=5)
            writing = executor.submit(reward)
            assert writer_ready.wait(timeout=5)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [writer_pid[0]])
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                sleep(0.01)
            assert blocked, "Reward writer must wait for the Bootstrap Player snapshot lock"
        finally:
            continue_snapshot.set()
        body = reading.result(timeout=5)
        writing.result(timeout=5)
    assert body["progression"]["lifetime_points"] == body["season"]["season_points"] == 0
    after = client.get("/api/v1/bootstrap", headers=auth).json()
    assert after["progression"]["lifetime_points"] == after["season"]["season_points"] == 7


def test_openapi_publishes_bootstrap_and_auth_error_schemas() -> None:
    from dexo_land.api import api

    schema = api.get_openapi_schema()
    bootstrap = schema["paths"]["/api/v1/bootstrap"]["get"]
    assert bootstrap["responses"][200]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BootstrapOutput"
    }
    for status in (401, 503):
        assert bootstrap["responses"][status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/IntegrationErrorOutput"
        }
    assert bootstrap["security"] == [{"MiniAppAuth": []}]
    fields = schema["components"]["schemas"]["BootstrapOutput"]["properties"]
    assert {"player", "progression", "season", "config", "features", "gameplay"} <= fields.keys()
    auth = schema["paths"]["/api/v1/auth/telegram"]["post"]
    assert {200, 400, 401, 422, 503} <= auth["responses"].keys()
