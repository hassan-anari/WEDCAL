from datetime import timedelta

import pytest
from django.utils import timezone

from apps.clans.models import Clan, ClanMembership, ClanRole, ClanSeason, PlayerSeasonClanState
from apps.game_config.services import resolve_effective_game_config
from apps.progression.models import PlayerProgression
from apps.telegram.models import TelegramGroup


@pytest.fixture
def clan_world(phase4_config_factory, season_factory, player_factory):  # type: ignore[no-untyped-def]
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
            player=row, lifetime_points=2500, level=5, dexo_stage="explorer", config_version=config
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
        cs = ClanSeason.objects.create(
            clan=identity,
            season=season,
            captain=captain,
            base_capacity=capacity,
            config_version=config,
        )
        # Keep the fixture's source clock deterministic: domain events use `now`.
        ClanSeason.objects.filter(pk=cs.pk).update(created_at=now)
        cs.refresh_from_db()
        ClanMembership.objects.create(
            player=captain,
            season=season,
            clan_season=cs,
            role=ClanRole.CAPTAIN,
            started_at=now,
            config_version=config,
        )
        return cs

    return now, config, season, resolved, player, clan
