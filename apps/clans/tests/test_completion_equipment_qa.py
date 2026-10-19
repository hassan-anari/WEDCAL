"""Reproducible isolated QA. Synthetic funding uses canonical test settlements only."""

# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import Client
from django.utils import timezone

from apps.clans.economy import (
    activate_item,
    active_effect_bps,
    canonical_clan_projection,
    purchase_item,
    settle_clan_reward,
)
from apps.clans.missions import record_mission_progress
from apps.clans.models import (
    ClanAchievementUnlock,
    ClanActiveEffect,
    ClanInventory,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanSeason,
)
from apps.clans.tests.test_phase8_concurrency import parallel
from apps.rewards.models import RewardLedger

CATALOG = {
    "rally-beacon": (2400, 120, 100, 24, 10),
    "contribution-booster": (2800, 120, 115, 23, 10),
    "mission-progress-booster": (3100, 90, 100, 20, 12),
    "coordination-booster": (3300, 90, 100, 22, 11),
    "capacity-expansion-module": (4100, None, 100, 20, 10),
    "roster-slot-module": (3800, None, 100, 20, 10),
    "command-relay": (5200, None, 100, 20, 10),
}


@pytest.fixture
def equipment_world(clan_world, init_data_factory, settings, monkeypatch):
    now, config, season, resolved, player, clan = clan_world
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_kw: pytest.fail("No live network QA")
    )
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    # TEST ONLY. No raw Treasury credit or mutable balance shortcut; all accounting legs exist.
    settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="TEST_ONLY:funding",
        gross_amount=1000000,
        idempotency_key="TEST_ONLY:funding",
        occurred_at=now,
        resolved_config=resolved,
        metadata={"test_only": True},
    )
    client = Client(
        HTTP_AUTHORIZATION="tma " + init_data_factory(user={"id": cs.captain.telegram_user_id})
    )
    return now, config, season, resolved, player, cs, member, client


@pytest.mark.django_db
@pytest.mark.parametrize("item_id", CATALOG)
def test_all_seven_items_purchase_activate_effect_expiry_and_api(equipment_world, item_id):
    _, config, _, resolved, _, cs, member, client = equipment_world
    price, minutes, contribution, hq, mission_units = CATALOG[item_id]
    before = client.get("/api/v1/clans/equipment").json()
    assert {x["item_id"] for x in before["items"]} == set(CATALOG)
    assert {x["item_id"]: x["price"] for x in before["items"]} == {
        key: value[0] for key, value in CATALOG.items()
    }
    points_before = list(RewardLedger.objects.values())
    cs.refresh_from_db()
    balance, capacity_bonus, xp = cs.treasury_balance, cs.capacity_bonus, cs.hq_xp
    path = f"/api/v1/clans/shop/{item_id}/purchase"
    body = {"idempotency_key": f"TEST_ONLY:purchase:{item_id}"}
    purchased = client.post(path, body, content_type="application/json")
    assert purchased.status_code == 200, purchased.content
    assert client.post(path, body, content_type="application/json").json()["replayed"] is True
    assert ClanInventory.objects.get(clan_season=cs, item_id=item_id).quantity == 1
    cs.refresh_from_db()
    assert cs.treasury_balance == balance - price
    path = f"/api/v1/clans/inventory/{item_id}/activate"
    body = {"idempotency_key": f"TEST_ONLY:activate:{item_id}"}
    used = client.post(path, body, content_type="application/json")
    assert used.status_code == 200, used.content
    assert client.post(path, body, content_type="application/json").json()["replayed"] is True
    assert ClanInventory.objects.get(clan_season=cs, item_id=item_id).quantity == 0
    with pytest.raises(ValidationError, match="not in inventory"):
        activate_item(
            clan_season=cs,
            actor=cs.captain,
            item_id=item_id,
            idempotency_key="TEST_ONLY:reuse",
            resolved_config=resolved,
        )
    assert list(RewardLedger.objects.values()) == points_before
    cs.refresh_from_db()
    assert cs.treasury_balance == balance - price
    assert cs.capacity_bonus == capacity_bonus + {
        "capacity-expansion-module": 5,
        "roster-slot-module": 1,
    }.get(item_id, 0)
    assert cs.hq_xp == xp + (2000 if item_id == "command-relay" else 0)
    definition = ClanMissionDefinition.objects.create(
        key="TEST-ONLY-qa",
        version=1,
        kind="INDIVIDUAL",
        requirement="VALID_CLAN_ACTIVITY",
        target_units=1000,
        reward_band="SMALL",
        starts_at=timezone.now() - timedelta(seconds=1),
        ends_at=timezone.now() + timedelta(hours=5),
        enabled=True,
        config_version=config,
    )
    at = timezone.now()
    progress = record_mission_progress(
        definition=definition,
        membership=member,
        source_id="TEST_ONLY:valid-activity-signal",
        units=10,
        occurred_at=at,
        resolved_config=resolved,
    )
    assert progress.units == mission_units
    settlement, _ = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="TEST_ONLY:matching-activity",
        gross_amount=100,
        idempotency_key="TEST_ONLY:matching-activity",
        occurred_at=at,
        resolved_config=resolved,
    )
    assert (settlement.player_amount, settlement.treasury_amount) == (90, 10)
    assert (settlement.contribution_amount, settlement.hq_xp_amount) == (contribution, hq)
    after = client.get("/api/v1/clans/equipment").json()
    assert after["server_time"] and after["config_version"] == config.version
    if minutes:
        effects = list(ClanActiveEffect.objects.filter(clan_season=cs))
        assert effects and all(
            e.expires_at - e.starts_at == timedelta(minutes=minutes) for e in effects
        )
        assert all(active_effect_bps(cs, e.category, e.expires_at) == (0, []) for e in effects)
        expired_at = max(e.expires_at for e in effects)
        expired, _ = settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id="TEST_ONLY:expired",
            gross_amount=100,
            idempotency_key="TEST_ONLY:expired",
            occurred_at=expired_at,
            resolved_config=resolved,
        )
        assert expired.contribution_amount == 100 and expired.hq_xp_amount == 20
    cs.refresh_from_db()
    expected = canonical_clan_projection(cs)
    assert cs.treasury_balance == expected["treasury_balance"]
    assert cs.treasury_earned_total == expected["treasury_earned_total"]
    assert cs.treasury_balance == cs.treasury_earned_total - price
    assert not ClanAchievementUnlock.objects.filter(key="banner-raised").exists()
    evidence = {
        "test_only": True,
        "item": item_id,
        "price": price,
        "equipment_before": before,
        "equipment_after": after,
        "purchase": purchased.json(),
        "activation": used.json(),
        "activity": {
            "gross": 100,
            "player": 90,
            "treasury": 10,
            "contribution": contribution,
            "hq_xp": hq,
            "mission_progress": progress.units,
        },
    }
    Path("work").mkdir(exist_ok=True)
    Path(f"work/phase2-equipment-{item_id}.json").write_text(json.dumps(evidence, indent=2))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "item_id,limit",
    [("capacity-expansion-module", 2), ("roster-slot-module", 5), ("command-relay", 2)],
)
def test_context_item_limits_are_season_scoped(equipment_world, item_id, limit):
    _, _, _, resolved, _, cs, _, _ = equipment_world
    for index in range(limit + 1):
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id=item_id,
            idempotency_key=f"TEST_ONLY:buy:{index}",
            resolved_config=resolved,
        )
        if index < limit:
            activate_item(
                clan_season=cs,
                actor=cs.captain,
                item_id=item_id,
                idempotency_key=f"TEST_ONLY:use:{index}",
                resolved_config=resolved,
            )
        else:
            with pytest.raises(ValidationError, match="limit"):
                activate_item(
                    clan_season=cs,
                    actor=cs.captain,
                    item_id=item_id,
                    idempotency_key=f"TEST_ONLY:use:{index}",
                    resolved_config=resolved,
                )
    assert ClanInventory.objects.get(clan_season=cs, item_id=item_id).quantity == 1


@pytest.mark.django_db
def test_overlapping_effects_take_highest_per_category(equipment_world):
    _, _, _, resolved, _, cs, _, _ = equipment_world
    for item in ["coordination-booster", "rally-beacon", "mission-progress-booster"]:
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id=item,
            idempotency_key=f"TEST_ONLY:buy:{item}",
            resolved_config=resolved,
        )
        activate_item(
            clan_season=cs,
            actor=cs.captain,
            item_id=item,
            idempotency_key=f"TEST_ONLY:use:{item}",
            resolved_config=resolved,
        )
    now = timezone.now()
    assert active_effect_bps(cs, "HQ_XP_RATE_BONUS_BPS", now)[0] == 2000
    assert active_effect_bps(cs, "CLAN_MISSION_PROGRESS_BONUS_BPS", now)[0] == 2000


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["RECOVERY", "ENDED"])
def test_invalid_clan_state_cannot_spend_or_use_inventory(equipment_world, state):
    _, _, _, resolved, _, cs, _, _ = equipment_world
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="TEST_ONLY:buy",
        resolved_config=resolved,
    )
    ClanSeason.objects.filter(pk=cs.pk).update(state=state)
    with pytest.raises(ValidationError):
        activate_item(
            clan_season=cs,
            actor=cs.captain,
            item_id="rally-beacon",
            idempotency_key="TEST_ONLY:use",
            resolved_config=resolved,
        )
    assert ClanInventory.objects.get(clan_season=cs).quantity == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_different_activations_cannot_double_use(equipment_world):
    _, _, _, resolved, _, cs, _, _ = equipment_world
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="TEST_ONLY:buy",
        resolved_config=resolved,
    )
    results = parallel(
        [
            partial(
                activate_item,
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key=f"TEST_ONLY:use:{i}",
                resolved_config=resolved,
            )
            for i in range(2)
        ]
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, ValidationError) for r in results) == 1
    assert cs.item_uses.count() == 1 and ClanInventory.objects.get(clan_season=cs).quantity == 0


@pytest.mark.django_db
def test_trophy_boundaries_use_earned_treasury_despite_spending(clan_world):
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)

    def settle(gross, key):
        return settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id=key,
            gross_amount=gross,
            idempotency_key=key,
            occurred_at=now,
            resolved_config=resolved,
        )

    settle(24999, "TEST_ONLY:contribution-before")
    assert not cs.achievement_unlocks.exists()
    settle(1, "TEST_ONLY:contribution-exact")
    assert list(cs.achievement_unlocks.values_list("key", flat=True)) == ["united-front"]
    settle(174999, "TEST_ONLY:treasury-before")
    assert not cs.achievement_unlocks.filter(key="treasury-keeper").exists()
    settle(1, "TEST_ONLY:treasury-exact")
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="TEST_ONLY:spend",
        resolved_config=resolved,
    )
    settle(1, "TEST_ONLY:treasury-exact")
    cs.refresh_from_db()
    assert cs.treasury_earned_total == 20000 and cs.treasury_balance == 17600
    assert cs.achievement_unlocks.count() == 2
    assert cs.treasury_entries.filter(direction="CREDIT").aggregate(v=Sum("amount"))["v"] == 20000


@pytest.mark.django_db
def test_soldier_purchase_and_activation_rejected_without_mutation(equipment_world):
    from django.core.exceptions import PermissionDenied

    _, config, season, resolved, player, cs, _, _ = equipment_world
    soldier = player("SOLDIER")
    ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role="SOLDIER",
        started_at=timezone.now(),
        config_version=config,
    )
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="TEST_ONLY:captain-buy",
        resolved_config=resolved,
    )
    cs.refresh_from_db()
    balance = cs.treasury_balance
    with pytest.raises(PermissionDenied):
        purchase_item(
            clan_season=cs,
            actor=soldier,
            item_id="rally-beacon",
            idempotency_key="TEST_ONLY:soldier:purchase",
            resolved_config=resolved,
        )
    with pytest.raises(PermissionDenied):
        activate_item(
            clan_season=cs,
            actor=soldier,
            item_id="rally-beacon",
            idempotency_key="TEST_ONLY:soldier:activate",
            resolved_config=resolved,
        )
    cs.refresh_from_db()
    assert cs.treasury_balance == balance
    assert ClanInventory.objects.get(clan_season=cs).quantity == 1


@pytest.mark.django_db
def test_insufficient_treasury_creates_no_inventory_or_debit(clan_world):
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    with pytest.raises(ValidationError, match="Insufficient Treasury"):
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id="rally-beacon",
            idempotency_key="TEST_ONLY:no-funds",
            resolved_config=resolved,
        )
    assert not cs.inventory.exists() and not cs.treasury_entries.exists()


@pytest.mark.django_db
def test_season_finalization_preserves_historical_inventory_and_effects(equipment_world):
    from apps.clans.services import close_season_clan_work
    from apps.seasons.models import Season

    _, config, season, resolved, _, cs, _, _ = equipment_world
    for index in range(2):
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id="rally-beacon",
            idempotency_key=f"TEST_ONLY:rollover-buy:{index}",
            resolved_config=resolved,
        )
    activate_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="TEST_ONLY:rollover-use",
        resolved_config=resolved,
    )
    effects = list(cs.active_effects.values())
    inventory = list(cs.inventory.values())
    assert close_season_clan_work(season.pk)["ended"] == 1
    assert list(cs.active_effects.values()) == effects
    assert list(cs.inventory.values()) == inventory
    next_season = Season.objects.create(
        code="next-test-season",
        title="Next test Season",
        starts_at=season.ends_at,
        ends_at=season.ends_at + timedelta(days=45),
        baseline_config=config,
    )
    next_clan = ClanSeason.objects.create(
        clan=cs.clan,
        season=next_season,
        captain=cs.captain,
        base_capacity=20,
        config_version=config,
    )
    assert not next_clan.inventory.exists() and not next_clan.active_effects.exists()
    assert next_clan.capacity_bonus == 0 and next_clan.hq_xp == 0
