from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.db import close_old_connections

from apps.clans.economy import activate_item, purchase_item, settle_clan_reward
from apps.clans.models import ClanInventory, ClanMembershipEvent


def parallel(calls: list[Callable[[], Any]]) -> list[Any]:
    barrier = Barrier(len(calls))

    def run(call: Callable[[], Any]) -> Any:
        close_old_connections()
        try:
            barrier.wait()
            return call()
        except Exception as exc:
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [future.result(timeout=20) for future in [pool.submit(run, call) for call in calls]]


def activated(member) -> None:  # type: ignore[no-untyped-def]
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )


@pytest.mark.django_db(transaction=True)
def test_duplicate_economic_source_concurrency_settles_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    results = parallel(
        [
            lambda: settle_clan_reward(
                membership=member,
                source_type="CLAN_ACTIVITY",
                source_id="race",
                gross_amount=10,
                idempotency_key="economic-race",
                occurred_at=now,
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    assert {result[0].pk for result in results}.__len__() == 1


@pytest.mark.django_db(transaction=True)
def test_cross_clan_global_idempotency_conflicts_safely(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    first, second = clan(), clan()
    for cs in (first, second):
        activated(cs.memberships.get())
    results = parallel(
        [
            lambda: settle_clan_reward(
                membership=first.memberships.get(),
                source_type="CLAN_ACTIVITY",
                source_id="one",
                gross_amount=10,
                idempotency_key="global-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
            lambda: settle_clan_reward(
                membership=second.memberships.get(),
                source_type="CLAN_ACTIVITY",
                source_id="two",
                gross_amount=10,
                idempotency_key="global-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1
    assert all(type(x).__name__ != "IntegrityError" for x in results)


@pytest.mark.django_db(transaction=True)
def test_duplicate_shop_purchase_concurrency_debits_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.treasury_balance = 5000
    cs.save()
    results = parallel(
        [
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="purchase-race",
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    cs.refresh_from_db()
    assert cs.treasury_balance == 2600
    assert ClanInventory.objects.get(clan_season=cs, item_id="rally-beacon").quantity == 1


@pytest.mark.django_db(transaction=True)
def test_duplicate_inventory_activation_concurrency_consumes_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    ClanInventory.objects.create(clan_season=cs, item_id="rally-beacon", quantity=1)
    results = parallel(
        [
            lambda: activate_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="activation-race",
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    assert ClanInventory.objects.get(clan_season=cs, item_id="rally-beacon").quantity == 0


@pytest.mark.django_db(transaction=True)
def test_duplicate_clan_window_finalizers_concurrency(clan_world) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from apps.clans.models import ClanActivity
    from apps.clans.windowing import finalize_due_windows
    from apps.core.activity import window_start_for

    now, config, _, _, _, clan = clan_world
    cs = clan()
    start = window_start_for(now - timedelta(hours=2), 900)
    type(cs).objects.filter(pk=cs.pk).update(created_at=start - timedelta(minutes=1))
    cs.refresh_from_db()
    member = cs.memberships.get()
    member.started_at = start
    member.save()
    activated(member)
    ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=member.player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=config,
        telegram_message_id=1201,
        telegram_update_id=1201,
        observed_at=start + timedelta(minutes=1),
        character_count=150,
        rewardable_chars=150,
        classification="VALID",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
        captain_bonus_eligible_at_source=True,
    )
    results = parallel([lambda: finalize_due_windows(now=now) for _ in range(2)])
    assert all(not isinstance(x, Exception) for x in results)
    assert cs.economic_settlements.count() == 1


@pytest.mark.django_db(transaction=True)
def test_different_key_treasury_purchases_cannot_overspend(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.treasury_balance = 3000
    cs.save()
    results = parallel(
        [
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="different-a",
                resolved_config=resolved,
            ),
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="contribution-booster",
                idempotency_key="different-b",
                resolved_config=resolved,
            ),
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1
    cs.refresh_from_db()
    assert cs.treasury_balance >= 0


@pytest.mark.django_db(transaction=True)
def test_same_mission_source_event_concurrency_progresses_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from apps.clans.missions import record_mission_progress
    from apps.clans.models import ClanMissionDefinition, ClanMissionSourceEvent

    now, config, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    definition = ClanMissionDefinition.objects.create(
        key="source-race",
        version=1,
        kind="INDIVIDUAL",
        requirement="TEST",
        target_units=2,
        reward_band="SMALL",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        enabled=True,
        config_version=config,
    )
    results = parallel(
        [
            lambda: record_mission_progress(
                definition=definition,
                membership=member,
                source_id="same-domain-event",
                units=1,
                occurred_at=now,
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert all(not isinstance(x, Exception) for x in results)
    assert ClanMissionSourceEvent.objects.filter(definition=definition).count() == 1
    assert results[0].__class__.objects.get(pk=results[0].pk).units == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("captain_cancels", [False, True])
def test_accepted_cancellation_vs_join_observation_is_consistent(
    clan_world: Any, monkeypatch: pytest.MonkeyPatch, captain_cancels: bool
) -> None:
    from apps.clans.models import ClanJoinRequest, ClanMembership, ClanRole, PlayerSeasonClanState
    from apps.clans.services import (
        cancel_request,
        captain_cancel_accepted_request,
        decide_request,
        reconcile_telegram_membership,
        request_join,
    )

    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    req.refresh_from_db()
    assert req.decided_at is not None
    cancel = (
        (lambda: captain_cancel_accepted_request(cs.captain, req.pk))
        if captain_cancels
        else (lambda: cancel_request(soldier, req.pk))
    )

    observed_at = req.decided_at
    assert observed_at is not None

    def observe() -> Any:
        return reconcile_telegram_membership(
            player=soldier,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=5001,
            observed_at=observed_at,
        )

    results = parallel([cancel, observe])
    assert not any(type(x).__name__ in {"IntegrityError", "OperationalError"} for x in results)
    assert sum(not isinstance(x, Exception) for x in results) >= 1
    req.refresh_from_db()
    membership = ClanMembership.objects.filter(player=soldier).first()
    assert (req.state == ClanJoinRequest.State.ACTIVATED) == (membership is not None)
    state = PlayerSeasonClanState.objects.get(player=soldier, season=cs.season)
    assert state.voluntary_leave_count == 0 and state.leave_cooldown_until is None
