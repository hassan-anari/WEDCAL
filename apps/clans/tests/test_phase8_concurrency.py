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
