import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.test import Client
from django.utils import timezone

from apps.daily.models import DailyClaim
from apps.missions.models import MissionDefinition, PlayerMission
from apps.rewards.models import RewardLedger
from apps.typing_game.models import TypingAttempt, TypingPhrase, TypingWallet


def init_data(token: str, user_id: int, *, age_seconds: int = 0) -> str:
    values = {
        "auth_date": str(int((timezone.now() - timedelta(seconds=age_seconds)).timestamp())),
        "query_id": "phase4-query",
        "user": json.dumps({"id": user_id, "first_name": "Phase4"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.fixture
def phase4_api(settings, client: Client, phase4_config_factory, season_factory):  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = "123:phase4-token"
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2), version="api-config")
    season = season_factory(
        config, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=44)
    )
    TypingPhrase.objects.create(
        key="api-phrase", version=1, text="Dexo typing arena phrase", enabled=True
    )
    MissionDefinition.objects.create(
        key="visit-dalexio",
        version=1,
        enabled=True,
        verification_family="TIMED_EXTERNAL",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
        verification_parameters={"destination_url": "https://example.com"},
    )
    return client, settings.TELEGRAM_BOT_TOKEN, config, season


@pytest.mark.django_db
def test_authenticated_phase4_api_flows_and_privacy(phase4_api) -> None:  # type: ignore[no-untyped-def]
    client, token, config, season = phase4_api
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9001)}"}
    daily_state = client.get("/api/v1/daily", **auth)
    assert daily_state.status_code == 200 and daily_state.json()["claimable"] is True
    daily_claim = client.post(
        "/api/v1/daily/claim", data="{}", content_type="application/json", **auth
    )
    assert daily_claim.status_code == 200
    assert DailyClaim.objects.get().config_version == config

    missions = client.get("/api/v1/missions", **auth)
    assert missions.status_code == 200 and missions.json()[0]["state"] == "AVAILABLE"
    started = client.post(
        "/api/v1/missions/visit-dalexio/start", data="{}", content_type="application/json", **auth
    )
    assert started.status_code == 200
    progress = PlayerMission.objects.get()
    progress.opened_at = timezone.now() - timedelta(
        seconds=config.payload["missions"]["timed_external_min_dwell_seconds"]
    )
    progress.save(update_fields=["opened_at"])
    verified = client.post(
        "/api/v1/missions/visit-dalexio/verify",
        data=json.dumps(
            {"open_token": started.json()["open_token"], "client_elapsed_seconds": 999}
        ),
        content_type="application/json",
        **auth,
    )
    assert verified.status_code == 200 and verified.json()["state"] == "REWARD_READY"
    claimed = client.post(
        "/api/v1/missions/visit-dalexio/claim", data="{}", content_type="application/json", **auth
    )
    assert claimed.status_code == 200 and claimed.json()["state"] == "CLAIMED"

    typing_state = client.get("/api/v1/typing", **auth)
    assert typing_state.status_code == 200 and typing_state.json()["attempts_used"] == 0
    typing_start = client.post(
        "/api/v1/typing/attempts/start",
        data="{}",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-attempt",
        **auth,
    )
    assert typing_start.status_code == 200
    attempt = TypingAttempt.objects.get(public_id=typing_start.json()["attempt_id"])
    attempt.started_at = timezone.now() - timedelta(seconds=5)
    attempt.expires_at = timezone.now() + timedelta(seconds=20)
    attempt.save(update_fields=["started_at", "expires_at"])
    submitted = client.post(
        f"/api/v1/typing/attempts/{attempt.public_id}/submit",
        data=json.dumps(
            {
                "typed_text": attempt.phrase.text,
                "wpm": 999,
                "accuracy": 0,
                "reward_amount": 99999,
                "season_id": 999,
            }
        ),
        content_type="application/json",
        **auth,
    )
    assert submitted.status_code == 200
    assert submitted.json()["wpm"] != 999 and submitted.json()["accuracy_bps"] == 10000
    wallet_before = TypingWallet.objects.get().claimable_balance
    typing_claim = client.post(
        "/api/v1/typing/claim",
        data=json.dumps({"player_id": 999, "reward": 999}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-claim",
        **auth,
    )
    assert typing_claim.status_code == 200 and typing_claim.json()["amount"] == wallet_before
    assert TypingWallet.objects.get().claimable_balance == 0
    assert RewardLedger.objects.filter(source_type="TYPING_CLAIM").exists()
    assert all(row.season == season for row in RewardLedger.objects.all())

    other_auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9002)}"}
    assert client.get("/api/v1/typing", **other_auth).json()["attempts_used"] == 0
    assert (
        client.post(
            f"/api/v1/typing/attempts/{attempt.public_id}/submit",
            data=json.dumps({"typed_text": attempt.phrase.text}),
            content_type="application/json",
            **other_auth,
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_phase4_api_rejects_invalid_auth_and_bounds_submission(phase4_api, settings) -> None:  # type: ignore[no-untyped-def]
    client, token, _, _ = phase4_api
    for path in ("/api/v1/daily", "/api/v1/missions", "/api/v1/typing"):
        assert client.get(path).status_code == 401
        assert client.get(path, HTTP_AUTHORIZATION="tma telegram_user_id=9001").status_code == 401
        assert (
            client.get(path, HTTP_AUTHORIZATION=f"tma {init_data(token, 9001)}tampered").status_code
            == 401
        )
        expired = init_data(
            token,
            9001,
            age_seconds=settings.TELEGRAM_INIT_DATA_MAX_AGE_SECONDS + 1,
        )
        assert (
            client.get(
                path,
                HTTP_AUTHORIZATION=f"tma {expired}",
            ).status_code
            == 401
        )
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9001)}"}
    started = client.post(
        "/api/v1/typing/attempts/start",
        data="{}",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="bounded",
        **auth,
    )
    response = client.post(
        f"/api/v1/typing/attempts/{started.json()['attempt_id']}/submit",
        data=json.dumps({"typed_text": "x" * 4097}),
        content_type="application/json",
        **auth,
    )
    assert response.status_code < 500


@pytest.mark.django_db
def test_bootstrap_contains_compact_real_phase4_state(phase4_api) -> None:  # type: ignore[no-untyped-def]
    client, token, _, _ = phase4_api
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9010)}"}
    client.post("/api/v1/daily/claim", data="{}", content_type="application/json", **auth)
    client.post(
        "/api/v1/typing/attempts/start",
        data="{}",
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="bootstrap-attempt",
        **auth,
    )
    response = client.get("/api/v1/bootstrap", **auth)
    assert response.status_code == 200
    body = response.json()
    assert {
        "player",
        "progression",
        "season",
        "config",
        "daily",
        "typing",
        "missions",
    } <= body.keys()
    assert body["daily"]["claimable"] is False and body["daily"]["streak"] == 1
    assert body["typing"]["attempts_used"] == 0 and body["typing"]["claimable_balance"] == 0
    assert body["missions"]["available_count"] == 1
    assert "items" not in body["missions"] and "definitions" not in body["missions"]
