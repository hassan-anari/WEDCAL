from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import close_old_connections

from apps.clans.activity import record_clan_activity
from apps.clans.models import ClanActivity, ClanActivityWindow, ClanMembershipEvent
from apps.clans.windowing import finalize_due_windows
from apps.core.activity import window_start_for
from apps.game_config.services import resolve_effective_game_config


def activate(member: Any, at: datetime) -> None:
    ClanMembershipEvent.objects.create(
        membership=member,
        kind="ACTIVATED",
        occurred_at=at,
        telegram_update_id=member.pk,
    )


def next_window_anchor(now: datetime, window_seconds: int) -> datetime:
    """Return a stable point five minutes inside the next complete window."""
    return window_start_for(now, window_seconds) + timedelta(seconds=window_seconds + 300)


def ingest_set(
    clan_world: Any, messages: list[tuple[int, str]], order: list[int]
) -> tuple[Any, Any, list[str]]:
    now, config, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    seconds = resolved.clan.activity.window_seconds
    anchor = next_window_anchor(now, seconds)
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
            observed_at=anchor + timedelta(seconds=offset),
        )
        assert row is not None
        source.append(row)
    source_times = [anchor + timedelta(seconds=offset) for offset, _ in messages]
    assert {window_start_for(at, seconds) for at in source_times} == {
        window_start_for(anchor, seconds)
    }
    assert {resolve_effective_game_config(at).id for at in source_times} == {config.pk}
    assert finalize_due_windows(now=anchor + timedelta(hours=1)) == 1
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
    cs, window, classifications = ingest_set(clan_world, messages, list(reversed(range(6))))
    assert classifications == ["VALID"] * 5 + ["REJECTED"]
    assert window.gross_amount == 5
    assert cs.economic_settlements.count() == 1
    assert sum(cs.treasury_entries.values_list("amount", flat=True)) == 0
    assert sum(cs.contribution_entries.values_list("amount", flat=True)) == 5
    cs.refresh_from_db()
    assert (cs.hq_xp, cs.competitive_score) == (1, 5)


@pytest.mark.django_db
def test_reverse_near_duplicates_are_source_order_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    variants = [
        "Alpha Beta Alpha Beta Alpha Beta",
        " alpha   beta alpha beta alpha beta ",
        "ALPHA BETA ALPHA BETA ALPHA BETA",
        "Alpha  Beta Alpha Beta Alpha Beta",
    ]
    messages = [(10 + index * 10, value) for index, value in enumerate(variants)]
    cs, window, classifications = ingest_set(clan_world, messages, [3, 2, 1, 0])
    assert classifications[-1] == "REJECTED"
    assert window.gross_amount == 0
    assert cs.economic_settlements.count() == 0
    assert cs.treasury_entries.count() == cs.contribution_entries.count() == 0
    cs.refresh_from_db()
    assert (cs.hq_xp, cs.competitive_score) == (0, 0)


@pytest.mark.django_db
def test_retry_same_telegram_message_is_one_source(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    anchor = next_window_anchor(now, resolved.clan.activity.window_seconds)
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    kwargs = dict(
        clan_season=cs,
        sender=member.player,
        message_id=77,
        update_id=88,
        text="r" * 150,
        observed_at=anchor,
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
    now, _, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    anchor = next_window_anchor(now, resolved.clan.activity.window_seconds)
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    observed = anchor
    first = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=1,
        update_id=1,
        text="z" * 150,
        observed_at=observed,
    )
    assert first is not None and finalize_due_windows(now=anchor + timedelta(hours=1)) == 1
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
    now, _, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    anchor = next_window_anchor(now, resolved.clan.activity.window_seconds)
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
                observed_at=anchor + timedelta(seconds=index * 10),
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(ingest, reversed(range(4))))
    assert all(row is not None for row in rows)
    assert finalize_due_windows(now=anchor + timedelta(hours=1)) == 1
    assert list(
        ClanActivity.objects.filter(clan_season=cs)
        .order_by("observed_at")
        .values_list("classification", flat=True)
    ) == ["VALID", "SUSPICIOUS", "SUSPICIOUS", "REJECTED"]


def adjacent_sources(
    clan_world: Any, messages: list[tuple[int, str]], order: list[int]
) -> tuple[Any, list[str]]:
    now, config, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    policy_seconds = resolved.clan.activity.window_seconds
    boundary = window_start_for(now, policy_seconds) + timedelta(seconds=policy_seconds * 2)
    source_times = [boundary + timedelta(seconds=offset) for offset, _ in messages]
    assert {window_start_for(at, policy_seconds) for at in source_times} == {
        boundary - timedelta(seconds=policy_seconds),
        boundary,
    }
    assert {resolve_effective_game_config(at).id for at in source_times} == {config.pk}
    for index in order:
        offset, text = messages[index]
        row = record_clan_activity(
            clan_season=cs,
            sender=member.player,
            message_id=5000 + index,
            update_id=6000 + index,
            text=text,
            observed_at=boundary + timedelta(seconds=offset),
        )
        assert row is not None
    assert finalize_due_windows(now=boundary + timedelta(hours=1)) == 2
    assert cs.clanactivitywindow_set.count() == 2
    return cs, list(
        ClanActivity.objects.filter(clan_season=cs)
        .order_by("observed_at")
        .values_list("classification", flat=True)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]])
def test_adjacent_window_exact_duplicates_are_deterministic(clan_world, order) -> None:  # type: ignore[no-untyped-def]
    messages = [(-20, "w" * 150), (-10, "w" * 150), (10, "w" * 150), (20, "w" * 150)]
    cs, classifications = adjacent_sources(clan_world, messages, order)
    assert classifications == ["VALID", "SUSPICIOUS", "SUSPICIOUS", "REJECTED"]
    assert sum(cs.clanactivitywindow_set.values_list("gross_amount", flat=True)) == 3
    assert sum(cs.treasury_entries.values_list("amount", flat=True)) == 0
    assert sum(cs.contribution_entries.values_list("amount", flat=True)) == 3
    assert cs.economic_settlements.filter(source_type="CLAN_ACTIVITY").count() == 2
    cs.refresh_from_db()
    assert (cs.hq_xp, cs.competitive_score) == (0, 3)


@pytest.mark.django_db
def test_adjacent_window_rate_history_is_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    messages = [(-25 + index * 10, chr(65 + index) * 150) for index in range(6)]
    cs, classifications = adjacent_sources(clan_world, messages, list(reversed(range(6))))
    assert classifications == ["VALID"] * 5 + ["REJECTED"]
    assert sum(cs.clanactivitywindow_set.values_list("gross_amount", flat=True)) == 5
    assert sum(cs.treasury_entries.values_list("amount", flat=True)) == 0
    assert sum(cs.contribution_entries.values_list("amount", flat=True)) == 5
    assert cs.economic_settlements.filter(source_type="CLAN_ACTIVITY").count() == 2
    cs.refresh_from_db()
    assert (cs.hq_xp, cs.competitive_score) == (1, 5)


@pytest.mark.django_db
def test_adjacent_window_near_duplicate_history_is_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    variants = [
        "Cross Window Near Duplicate Text",
        " cross   window near duplicate text ",
        "CROSS WINDOW NEAR DUPLICATE TEXT",
        "Cross  Window Near Duplicate Text",
    ]
    messages = [(-20, variants[0]), (-10, variants[1]), (10, variants[2]), (20, variants[3])]
    cs, classifications = adjacent_sources(clan_world, messages, [3, 1, 2, 0])
    assert classifications == ["VALID", "SUSPICIOUS", "SUSPICIOUS", "REJECTED"]
    assert sum(cs.clanactivitywindow_set.values_list("gross_amount", flat=True)) == 0
    assert cs.economic_settlements.count() == 0
    assert cs.treasury_entries.count() == cs.contribution_entries.count() == 0
    cs.refresh_from_db()
    assert (cs.hq_xp, cs.competitive_score) == (0, 0)


@pytest.mark.django_db
def test_late_prior_window_event_never_changes_next_window(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    boundary = now.replace(microsecond=0) + timedelta(minutes=15)
    boundary -= timedelta(seconds=int(boundary.timestamp()) % 900)
    if boundary - now < timedelta(minutes=1):
        boundary += timedelta(minutes=15)
    for index, offset in enumerate((-20, -10)):
        assert record_clan_activity(
            clan_season=cs,
            sender=member.player,
            message_id=7000 + index,
            update_id=7000 + index,
            text="late-boundary" * 15,
            observed_at=boundary + timedelta(seconds=offset),
        )
    assert finalize_due_windows(now=boundary + timedelta(minutes=30)) == 1
    late = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=7002,
        update_id=7002,
        text="late-boundary" * 15,
        observed_at=boundary - timedelta(seconds=5),
    )
    assert late is not None and late.classification == "LATE"
    next_row = record_clan_activity(
        clan_season=cs,
        sender=member.player,
        message_id=7003,
        update_id=7003,
        text="late-boundary" * 15,
        observed_at=boundary + timedelta(seconds=5),
    )
    assert next_row is not None
    assert finalize_due_windows(now=boundary + timedelta(hours=1)) == 2
    next_row.refresh_from_db()
    assert next_row.classification == "SUSPICIOUS"


@pytest.mark.django_db(transaction=True)
def test_previous_window_late_ingestion_racing_next_finalizer_is_deterministic(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    boundary = now.replace(microsecond=0) - timedelta(hours=1)
    boundary -= timedelta(seconds=int(boundary.timestamp()) % 900)
    member.started_at = boundary - timedelta(hours=1)
    member.save(update_fields=("started_at",))
    activate(member, member.started_at)
    # Two committed prior events make the next event suspicious but eligible.
    for index, offset in enumerate((-20, -10)):
        record_clan_activity(
            clan_season=cs,
            sender=member.player,
            message_id=8000 + index,
            update_id=8000 + index,
            text="race-boundary" * 15,
            observed_at=boundary + timedelta(seconds=offset),
        )
    next_row = ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=member.player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=config,
        telegram_message_id=8003,
        telegram_update_id=8003,
        observed_at=boundary + timedelta(seconds=5),
        character_count=195,
        rewardable_chars=195,
        classification="VALID",
        exact_fingerprint="irrelevant",
        normalized_fingerprint="irrelevant",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
        captain_bonus_eligible_at_source=True,
    )
    ClanActivityWindow.objects.create(
        clan_season=cs,
        membership=member,
        season=cs.season,
        config_version=config,
        window_start=boundary - timedelta(minutes=15),
        window_end=boundary,
        finalized_at=now - timedelta(minutes=1),
    )

    def delayed() -> Any:
        close_old_connections()
        try:
            return record_clan_activity(
                clan_season=cs,
                sender=member.player,
                message_id=8002,
                update_id=8002,
                text="race-boundary" * 15,
                observed_at=boundary - timedelta(seconds=5),
            )
        finally:
            close_old_connections()

    def finalize() -> int:
        close_old_connections()
        try:
            return finalize_due_windows(now=now)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        delayed_future = pool.submit(delayed)
        finalize_future = pool.submit(finalize)
        delayed_row: Any = delayed_future.result()
        finalize_future.result()
    assert (
        delayed_row is not None
        and delayed_row.classification == "LATE"
        and delayed_row.rewardable_chars == 0
    )
    next_row.refresh_from_db()
    assert next_row.classification == "VALID"


@pytest.mark.django_db
def test_antifraud_history_crosses_config_cutover(clan_world, phase4_config_factory) -> None:  # type: ignore[no-untyped-def]
    now, config_a, _, resolved, _, clan = clan_world
    assert resolved.clan is not None
    seconds = resolved.clan.activity.window_seconds
    period_start = window_start_for(now, seconds) + timedelta(seconds=seconds)
    cutover = period_start + timedelta(minutes=7)
    phase4_config_factory(effective_at=cutover, version="activity-cutover")
    cs = clan()
    member = cs.memberships.get()
    activate(member, now)
    source_times = (cutover - timedelta(seconds=1), cutover + timedelta(seconds=1))
    assert {window_start_for(at, seconds) for at in source_times} == {period_start}
    assert resolve_effective_game_config(source_times[0]).id == config_a.pk
    assert resolve_effective_game_config(source_times[1]).id != config_a.pk
    for index, at in enumerate(source_times):
        row = record_clan_activity(
            clan_season=cs,
            sender=member.player,
            message_id=9000 + index,
            update_id=9000 + index,
            text="config-crossing" * 15,
            observed_at=at,
        )
        assert row is not None
    assert finalize_due_windows(now=period_start + timedelta(hours=1)) == 2
    # Config provenance is part of the settlement identity; an intentional
    # in-period cutover therefore creates one durable window per config.
    assert cs.clanactivitywindow_set.count() == 2
    assert list(
        ClanActivity.objects.filter(clan_season=cs)
        .order_by("observed_at")
        .values_list("classification", flat=True)
    ) == ["VALID", "SUSPICIOUS"]
