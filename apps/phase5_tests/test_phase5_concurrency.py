import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.db import close_old_connections

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import publish_game_config
from apps.players.models import Player
from apps.referrals.models import ReferralRelation
from apps.referrals.services import attribute_start, get_or_create_code

AT = datetime(2026, 8, 5, 12, tzinfo=UTC)
BASE = json.loads(Path("config/examples/season1.game-config.v1.json").read_text())


@pytest.mark.django_db(transaction=True)
def test_concurrent_two_node_referral_cycle_is_prevented() -> None:
    config = GameConfigVersion.objects.create(
        version="phase5-cycle", effective_at=AT - timedelta(days=1), payload=copy.deepcopy(BASE)
    )
    publish_game_config(config, published_at=AT - timedelta(days=2))
    first = Player.objects.create(
        telegram_user_id=9901,
        first_seen_at=AT - timedelta(days=2),
        last_seen_at=AT,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )
    second = Player.objects.create(
        telegram_user_id=9902,
        first_seen_at=AT - timedelta(days=2),
        last_seen_at=AT,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )
    first_code = get_or_create_code(first).code
    second_code = get_or_create_code(second).code
    barrier = Barrier(2)

    def attribute(invitee_id: int, code: str) -> None:
        close_old_connections()
        try:
            barrier.wait()
            attribute_start(Player.objects.get(pk=invitee_id), code, at=AT)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(attribute, first.pk, second_code),
            pool.submit(attribute, second.pk, first_code),
        )
        for future in futures:
            future.result(timeout=10)

    assert ReferralRelation.objects.count() == 1
