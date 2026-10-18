# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import pytest
from django.test import Client

from apps.rewards.models import RewardLedger
from dexo_land.api import api

pytestmark = pytest.mark.django_db


@pytest.fixture
def history_world(clan_world, init_data_factory, settings):
    now, config, season, _, make_player, _ = clan_world
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    owner, other = make_player(), make_player()

    def ledger(source, person=owner):
        return RewardLedger.objects.create(
            player=person,
            season=season,
            config_version=config,
            source_type=source,
            source_id=str(RewardLedger.objects.count()),
            idempotency_key=f"history:{RewardLedger.objects.count()}",
            context="TEST",
            gross_amount=1,
            player_amount=1,
            occurred_at=now,
        )

    client = Client(
        HTTP_AUTHORIZATION="tma "
        + init_data_factory(user={"id": owner.telegram_user_id, "first_name": "Owner"})
    )
    return client, ledger, other


def test_network_filters_before_limit_and_uses_filtered_lookahead(history_world):
    client, ledger, other = history_world
    network = []
    for source in ["CREATOR", "DAILY", "NETWORK_SPEAKER", "TYPING", "CREATOR"]:
        row = ledger(source)
        if source in {"CREATOR", "NETWORK_SPEAKER"}:
            network.append(row.pk)
    for _ in range(25):
        ledger("BASE_TAP")
    ledger("CREATOR", other)
    all_ids = list(
        RewardLedger.objects.exclude(player=other).order_by("-id").values_list("pk", flat=True)
    )
    first = client.get("/api/v1/rewards/history", {"category": "network", "limit": 2})
    assert first.status_code == 200
    assert [x["id"] for x in first.json()["items"]] == network[::-1][:2]
    assert first.json()["next_cursor"] == network[1]
    second = client.get(
        "/api/v1/rewards/history",
        {"category": "network", "limit": 2, "cursor": first.json()["next_cursor"]},
    ).json()
    assert [x["id"] for x in second["items"]] == [network[0]]
    assert second["next_cursor"] is None
    exact = client.get("/api/v1/rewards/history", {"category": "network", "limit": 3}).json()
    assert len(exact["items"]) == 3 and exact["next_cursor"] is None
    empty = client.get(
        "/api/v1/rewards/history", {"category": "network", "cursor": network[0]}
    ).json()
    assert empty == {"items": [], "next_cursor": None}
    # Existing clients retain the complete descending-ID history and numeric cursors.
    seen: list[int] = []
    cursor = None
    while True:
        query = {"limit": 7}
        if cursor:
            query["cursor"] = cursor
        page = client.get("/api/v1/rewards/history", query).json()
        seen.extend(x["id"] for x in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == all_ids


def test_empty_source_exact_matching_and_cursor_isolation(history_world):
    client, ledger, other = history_world
    foreign = ledger("CREATOR", other)
    for source in ["creator", "CREATOR_BONUS", "CLAN_ACTIVITY", "REFERRAL_SHARE", "NETWORK"]:
        ledger(source)
    assert client.get("/api/v1/rewards/history", {"category": "network"}).json() == {
        "items": [],
        "next_cursor": None,
    }
    assert client.get(
        "/api/v1/rewards/history", {"category": "network", "cursor": foreign.pk + 1}
    ).json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("category", ["", "NETWORK", "all", "creator", "arbitrary"])
def test_invalid_category_uses_typed_error(history_world, category):
    client, *_ = history_world
    response = client.get("/api/v1/rewards/history", {"category": category})
    assert response.status_code == 422
    assert response.json() == {
        "code": "REQUEST_INVALID",
        "detail": "request invalid",
        "retryable": False,
        "retry_after_seconds": None,
    }


def test_history_auth_and_schema(client):
    assert client.get("/api/v1/rewards/history?category=network").status_code == 401
    parameters = api.get_openapi_schema()["paths"]["/api/v1/rewards/history"]["get"]["parameters"]
    category = next(p for p in parameters if p["name"] == "category")
    assert not category["required"]
    assert category["schema"]["anyOf"][0]["const"] == "network"
