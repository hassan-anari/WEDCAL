from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import close_old_connections

from apps.clans.activity import record_clan_activity
from apps.clans.models import ClanActivity, ClanMembershipEvent
from apps.clans.windowing import finalize_due_windows


def activate(member: Any, at: datetime) -> None:
    ClanMembershipEvent.objects.create(
        membership=member,
        kind="ACTIVATED",
        occurred_at=at,
        telegram_update_id=member.pk,
    )


def ingest_set(
    clan_world: Any, messages: list[tuple[int, str]], order: list[int]
) -> tuple[Any, Any, list[str]]:
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    source = []
    for index in order:
        offset, text = messages[index]
        row = record_clan_activity(
            clan_season=cs,
            sender=member.player,
            message_id=1000 + index,
            update_id=2000 + index,
            text=text,
            observed_at=now + timedelta(seconds=offset),
        )
        assert row is not None
        source.append(row)
    assert finalize_due_windows(now=now + timedelta(hours=1)) == 1
    cs.refresh_from_db()
    window = cs.clanactivitywindow_set.get()
    return (
        cs,
        window,
        list(
            ClanActivity.objects.filter(clan_season=cs)
            .order_by("observed_at", "telegram_update_id", "telegram_message_id", "pk")
            .values_list("classification", flat=True)
        ),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]])
def test_exact_duplicates_are_source_order_deterministic(clan_world, order) -> None:  # type: ignore[no-untyped-def]
    messages = [(10 + index * 10, "x" * 150) for index in range(4)]
    cs, window, classifications = ingest_set(clan_world, messages, order)
    assert classifications == ["VALID", "SUSPICIOUS", "SUSPICIOUS", "REJECTED"]
    assert window.gross_amount == 3
    settlement = cs.economic_settlements.get()
    assert (
        settlement.gross_amount,
        settlement.treasury_amount,
        settlement.contribution_entry.amount,
        cs.hq_xp,
        cs.competitive_score,
    ) == (3, 0, 3, 0, 3)


@pytest.mark.django_db
def test_reverse_rate_limit_is_source_order_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    messages = [(10 + index * 5, chr(65 + index) * 150) for index in range(6)]
    _, window, classifications = ingest_set(clan_world, messages, list(reversed(range(6))))
    assert classifications == ["VALID"] * 5 + ["REJECTED"]
    assert window.gross_amount == 5


@pytest.mark.django_db
def test_reverse_near_duplicates_are_source_order_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    variants = [
        "Alpha Beta Alpha Beta Alpha Beta",
        " alpha   beta alpha beta alpha beta ",
        "ALPHA BETA ALPHA BETA ALPHA BETA",
        "Alpha  Beta Alpha Beta Alpha Beta",
    ]
    messages = [(10 + index * 10, value) for index, value in enumerate(variants)]
    _, _, classifications = ingest_set(clan_world, messages, [3, 2, 1, 0])
    assert classifications[-1] == "REJECTED"


@pytest.mark.django_db
def test_retry_same_telegram_message_is_one_source(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    kwargs = dict(
        clan_season=cs,
        sender=member.player,
        message_id=77,
        update_id=88,
        text="r" * 150,
        observed_at=now + timedelta(seconds=10),
    )
    first = record_clan_activity(**kwargs)
    replay = record_clan_activity(**kwargs)
    assert first is not None and replay is not None and first.pk == replay.pk
    assert ClanActivity.objects.filter(clan_season=cs).count() == 1


@pytest.mark.django_db
def test_delayed_pre_finalization_source_reclassifies_later_event(clan_world) -> None:  # type: ignore[no-untyped-def]
    messages = [(10 + index * 10, "d" * 150) for index in range(4)]
    cs, window, classifications = ingest_set(clan_world, messages, [0, 1, 3, 2])
    assert classifications[-1] == "REJECTED"
    assert window.gross_amount == 3 and cs.economic_settlements.get().gross_amount == 3


@pytest.mark.django_db
def test_finalized_window_is_immutable_and_delayed_source_is_late(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    observed = now + timedelta(seconds=10)
    first = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=1,
        update_id=1,
        text="z" * 150,
        observed_at=observed,
    )
    assert first is not None and finalize_due_windows(now=now + timedelta(hours=1)) == 1
    before = cs.economic_settlements.get().gross_amount
    late = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=2,
        update_id=2,
        text="z" * 150,
        observed_at=observed + timedelta(seconds=1),
    )
    assert late is not None and late.classification == "LATE" and late.rewardable_chars == 0
    assert cs.economic_settlements.get().gross_amount == before


@pytest.mark.django_db(transaction=True)
def test_concurrent_ingestion_requalifies_in_total_source_order(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)

    def ingest(index):  # type: ignore[no-untyped-def]
        close_old_connections()
        try:
            return record_clan_activity(
                clan_season=cs,
                sender=member.player,
                message_id=3000 + index,
                update_id=4000 + index,
                text="c" * 150,
                observed_at=now + timedelta(seconds=10 + index * 10),
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(ingest, reversed(range(4))))
    assert all(row is not None for row in rows)
    assert finalize_due_windows(now=now + timedelta(hours=1)) == 1
    assert list(
        ClanActivity.objects.filter(clan_season=cs)
        .order_by("observed_at")
        .values_list("classification", flat=True)
    ) == ["VALID", "SUSPICIOUS", "SUSPICIOUS", "REJECTED"]
