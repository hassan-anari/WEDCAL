from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connections

from apps.players.models import Player
from apps.players.services import TelegramProfile, resolve_player


@pytest.mark.django_db
def test_repeated_observation_enriches_without_overwriting_first_seen() -> None:
    first = resolve_player(
        TelegramProfile(telegram_user_id=10, first_name="First", username="kept"),
        Player.FirstSeenSource.GROUP_ACTIVITY,
    )
    second = resolve_player(
        TelegramProfile(telegram_user_id=10, first_name="Updated"),
        Player.FirstSeenSource.MINI_APP,
        mini_app=True,
    )
    second.player.refresh_from_db()
    assert first.created is True
    assert second.created is False
    assert second.player.first_seen_source == Player.FirstSeenSource.GROUP_ACTIVITY
    assert second.player.first_name == "Updated"
    assert second.player.username == "kept"
    assert second.player.first_mini_app_at is not None


@pytest.mark.django_db(transaction=True)
def test_concurrent_identity_resolution_creates_one_player() -> None:
    def resolve() -> int:
        connections.close_all()
        result = resolve_player(
            TelegramProfile(telegram_user_id=999, first_name="Concurrent"),
            Player.FirstSeenSource.MINI_APP,
            mini_app=True,
        )
        connections.close_all()
        return result.player.pk

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: resolve(), range(2)))
    assert len(set(ids)) == 1
    assert Player.objects.filter(telegram_user_id=999).count() == 1
