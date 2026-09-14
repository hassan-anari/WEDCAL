import copy
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.clans.models import Clan, ClanMembership, ClanRole, ClanSeason, PlayerSeasonClanState
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import publish_game_config, resolve_effective_game_config
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.seasons.models import Season
from apps.seasons.services import activate_season
from apps.telegram.models import TelegramGroup

BASE_CONFIG = json.loads(
    (
        Path(__file__).resolve().parents[1] / "config/examples/season1.game-config.v1.json"
    ).read_text()
)


@pytest.fixture
def telegram_bot_token() -> str:
    return "123456:test-token"


@pytest.fixture
def init_data_factory(telegram_bot_token: str) -> Callable[..., str]:
    """Canonical signed Telegram Mini App init-data builder for all app tests."""

    def make(
        *,
        user: dict[str, object] | None = None,
        auth_date: datetime | None = None,
        token: str = telegram_bot_token,
    ) -> str:
        values = {
            "auth_date": str(int((auth_date or datetime.now(tz=UTC)).timestamp())),
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps(user or {"id": 42, "first_name": "Dexo"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    return make


@pytest.fixture
def phase4_config_factory(db) -> Callable[..., GameConfigVersion]:  # type: ignore[no-untyped-def]
    sequence = 0

    def create(
        *,
        effective_at: datetime | None = None,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        version: str | None = None,
    ) -> GameConfigVersion:
        nonlocal sequence
        sequence += 1
        payload = copy.deepcopy(BASE_CONFIG)
        if mutate:
            mutate(payload)
        effective = effective_at or datetime(2026, 1, 1, tzinfo=UTC)
        config = GameConfigVersion.objects.create(
            version=version or f"phase4-v{sequence}",
            schema_version=payload["meta"]["schema_version"],
            effective_at=effective,
            payload=payload,
        )
        return publish_game_config(config, published_at=effective - timedelta(seconds=1))

    return create


@pytest.fixture
def player_factory(db) -> Callable[[int], Player]:  # type: ignore[no-untyped-def]
    def create(telegram_id: int = 100) -> Player:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Player.objects.create(
            telegram_user_id=telegram_id,
            first_name="Dexo",
            first_seen_at=now,
            last_seen_at=now,
            first_seen_source=Player.FirstSeenSource.MINI_APP,
        )

    return create


@pytest.fixture
def season_factory(db) -> Callable[..., Season]:  # type: ignore[no-untyped-def]
    sequence = 0

    def create(
        config: GameConfigVersion,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Season:
        nonlocal sequence
        sequence += 1
        start = starts_at or datetime(2026, 1, 1, tzinfo=UTC)
        season = Season.objects.create(
            code=f"phase4-s{sequence}",
            title="Phase 4",
            starts_at=start,
            ends_at=ends_at or start + timedelta(days=45),
            baseline_config=config,
        )
        return activate_season(season)

    return create


@pytest.fixture
def close_season_for_test(db) -> Callable[[Season], Season]:  # type: ignore[no-untyped-def]
    """Use the production transition capability in one transaction for legacy setup only."""

    def close(season: Season) -> Season:
        with transaction.atomic():
            locked = Season.objects.select_for_update().get(pk=season.pk)
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('dexo.season_transition', 'on', true)")
            locked.status = Season.Status.CLOSED
            locked.save(update_fields=("status", "updated_at"))
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('dexo.season_transition', 'off', true)")
        return locked

    return close


@pytest.fixture
def clan_world(phase4_config_factory, season_factory, player_factory):  # type: ignore[no-untyped-def]
    """Shared Clan world used by Clan and cross-domain Season-close tests."""
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2), version="phase7-domain")
    season = season_factory(
        config, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=44)
    )
    resolved = resolve_effective_game_config(now)
    sequence = 0

    def player(role: str | None = None):  # type: ignore[no-untyped-def]
        nonlocal sequence
        sequence += 1
        row = player_factory(71000 + sequence)
        PlayerProgression.objects.create(
            player=row,
            lifetime_points=2500,
            level=5,
            dexo_stage="explorer",
            config_version=config,
        )
        if role:
            PlayerSeasonClanState.objects.create(player=row, season=season, role=role)
        return row

    def clan(captain=None, capacity=20):  # type: ignore[no-untyped-def]
        nonlocal sequence
        captain = captain or player(ClanRole.CAPTAIN)
        sequence += 1
        group = TelegramGroup.objects.create(
            telegram_chat_id=-71000 - sequence,
            chat_type="supergroup",
            title=f"Group {sequence}",
            first_seen_at=now,
            last_seen_at=now,
        )
        identity = Clan.objects.create(
            telegram_group=group,
            name=f"Clan {sequence}",
            tag=f"C{sequence}",
            normalized_tag=f"c{sequence}",
            creator=captain,
        )
        clan_season = ClanSeason.objects.create(
            clan=identity,
            season=season,
            captain=captain,
            base_capacity=capacity,
            config_version=config,
        )
        ClanSeason.objects.filter(pk=clan_season.pk).update(created_at=now)
        clan_season.refresh_from_db()
        ClanMembership.objects.create(
            player=captain,
            season=season,
            clan_season=clan_season,
            role=ClanRole.CAPTAIN,
            started_at=now,
            config_version=config,
        )
        return clan_season

    return now, config, season, resolved, player, clan
