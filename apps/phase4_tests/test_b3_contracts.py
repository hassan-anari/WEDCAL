# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import close_old_connections
from django.utils import timezone

from apps.daily.services import claim_daily
from apps.missions.models import MissionDefinition, PlayerMission
from apps.missions.services import claim_mission, start_mission, verify_mission
from apps.phase4_tests.test_api import init_data, phase4_api  # noqa: F401
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason
from apps.telegram.bot_api import TelegramResult
from apps.typing_game.models import TypingAttempt, TypingPhrase
from apps.typing_game.services import start_attempt, submit_attempt

pytestmark = pytest.mark.django_db(transaction=True)
# Imported fixture belongs to the existing integration suite.


def test_exact_http_daily_mission_typing_referral_network_fixtures(
    phase4_api, monkeypatch, settings
):  # noqa: F811
    settings.TELEGRAM_BOT_USERNAME = "DexoFixtureBot"
    client, token, config, season = phase4_api
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9001)}"}
    fixtures = {}

    def get(name, path):
        response = client.get("/api/v1" + path, **auth)
        assert response.status_code == 200, (name, response.content)
        fixtures[name] = response.json()
        return response.json()

    def post(name, path, data=None, key=None, status=200):
        headers = dict(auth)
        if key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        response = client.post(
            "/api/v1" + path,
            data=json.dumps(data or {}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == status, (name, response.content)
        fixtures[name] = {"request": data or {}, "response": response.json(), "http_status": status}
        return response.json()

    get("daily_available", "/daily")
    claimed = post("daily_claim", "/daily/claim")
    assert claimed["canonical"]["progression"]["lifetime_points"] == 80
    duplicate = post("daily_duplicate", "/daily/claim")
    assert not duplicate["created"] and duplicate["canonical"]["season_points"] == 80
    get("mission_list", "/missions")
    opened = post("mission_start", "/missions/visit-dalexio/start")
    post(
        "mission_dwell_wait", "/missions/visit-dalexio/verify", {"open_token": opened["open_token"]}
    )
    PlayerMission.objects.update(opened_at=timezone.now() - timedelta(seconds=7))
    verified = post(
        "mission_verified", "/missions/visit-dalexio/verify", {"open_token": opened["open_token"]}
    )
    assert verified["state"] == "REWARD_READY"
    reward = post("mission_claim", "/missions/visit-dalexio/claim")
    assert reward["canonical"]["progression"]["lifetime_points"] == 200
    post("mission_duplicate", "/missions/visit-dalexio/claim")
    get("typing_state", "/typing")
    attempt = post("typing_start", "/typing/attempts/start", key="b3-run")
    resume = get("typing_resume", "/typing")
    assert resume["active_attempt_detail"]["phrase_text"] == attempt["phrase_text"]
    TypingAttempt.objects.update(started_at=timezone.now() - timedelta(seconds=5))
    submit = post(
        "typing_submit",
        f"/typing/attempts/{attempt['attempt_id']}/submit",
        {"typed_text": attempt["phrase_text"]},
    )
    assert submit["status"] == "VALID" and submit["typing"]["claimable_balance"] > 0
    post(
        "typing_duplicate_submit",
        f"/typing/attempts/{attempt['attempt_id']}/submit",
        {"typed_text": "different text cannot earn again"},
    )
    typing_claim = post("typing_claim", "/typing/claim", key="b3-claim")
    assert (
        typing_claim["canonical"]["progression"]["lifetime_points"] == 200 + typing_claim["amount"]
    )
    post("typing_duplicate_claim", "/typing/claim", key="b3-claim")
    get("referral", "/referrals")
    get("invitees_empty", "/referrals/invitees")
    get("network_summary", "/network")
    get("network_groups_empty", "/network/groups")
    get("canonical_state", "/rewards/state")
    profile = get("profile_overview", "/profile/overview")
    bootstrap = get("bootstrap", "/bootstrap")
    assert profile["canonical"]["progression"] == bootstrap["canonical"]["progression"]
    assert fixtures["canonical_state"]["progression"] == typing_claim["canonical"]["progression"]
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/earn-network.json").write_text(json.dumps(fixtures, indent=2) + "\n")


def test_daily_mission_and_typing_concurrent_claims_cannot_double_credit(
    phase4_config_factory, season_factory, player_factory
):
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    at = season.starts_at + timedelta(days=1)

    def run(_):
        close_old_connections()
        try:
            return claim_daily(player, at)[1]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert results.count(True) == 1
    assert PlayerProgression.objects.get(player=player).lifetime_points == 80
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 80


def test_mission_transient_telegram_recovers_without_consuming_claim(
    phase4_config_factory, season_factory, player_factory, monkeypatch
):
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    at = season.starts_at + timedelta(days=1)
    MissionDefinition.objects.create(
        key="join-telegram",
        version=1,
        title="Join",
        enabled=True,
        verification_family="TELEGRAM",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
        verification_parameters={"telegram_chat_id": -100},
    )
    start_mission(player, "join-telegram", at)
    monkeypatch.setattr(
        "apps.missions.services.get_chat_member",
        lambda *_: TelegramResult(False, category="local_rate_limited", retry_after=3),
    )
    from apps.missions.services import MissionVerificationUnavailable

    with pytest.raises(MissionVerificationUnavailable):
        verify_mission(player, "join-telegram", at)
    assert not RewardLedger.objects.exists() and PlayerMission.objects.get().status == "IN_PROGRESS"
    monkeypatch.setattr(
        "apps.missions.services.get_chat_member",
        lambda *_: TelegramResult(
            True, {"status": "member", "user": {"id": player.telegram_user_id}}
        ),
    )
    assert verify_mission(player, "join-telegram", at).status == "REWARD_READY"
    assert claim_mission(player, "join-telegram", at)[1]
    assert not claim_mission(player, "join-telegram", at)[1]


def test_daily_repeat_mission_new_day_and_disabled_verified_obligation(
    phase4_config_factory, season_factory, player_factory
):
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    at = season.starts_at + timedelta(days=1)
    definition = MissionDefinition.objects.create(
        key="typing-session",
        version=1,
        title="Type",
        enabled=True,
        verification_family="INTERNAL",
        repeat_policy="DAILY",
    )
    TypingPhrase.objects.create(
        key="b3", version=1, text="Dexo explorers share adventures", enabled=True
    )
    for index in range(2):
        now = at + timedelta(days=index)
        attempt, _ = start_attempt(player, f"run-{index}", now)
        submit_attempt(player, attempt.public_id, attempt.phrase.text, now + timedelta(seconds=5))
        verify_mission(player, "typing-session", now + timedelta(seconds=6))
        if index == 1:
            definition.enabled = False
            definition.save()
        assert claim_mission(player, "typing-session", now + timedelta(seconds=7))[1]
    assert RewardLedger.objects.filter(source_type="MISSION").count() == 2


def test_real_creator_activity_settlement_read_model_fixture(
    phase4_config_factory, season_factory, player_factory
):
    from django.test import RequestFactory

    from apps.network.api import groups
    from apps.network.models import CreatorGroupRegistration
    from apps.network.services import record_activity, settle_window, window_start_for
    from apps.telegram.models import TelegramGroup

    config = phase4_config_factory()
    season = season_factory(config)
    creator = player_factory()
    at = season.starts_at + timedelta(days=1, hours=1)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-100,
        chat_type="supergroup",
        title="Synthetic fixture community",
        bot_membership_status="administrator",
        bot_is_admin=True,
        is_monitored=True,
        first_seen_at=at,
        last_seen_at=at,
    )
    registration = CreatorGroupRegistration.objects.create(
        group=group,
        creator=creator,
        state="ACTIVE",
        reward_eligible_since=at,
        bot_verified_at=at,
        creator_verified_at=at,
    )
    for index in range(3):
        speaker = player_factory(900 + index)
        record_activity(
            registration=registration,
            sender=speaker,
            message_id=index + 1,
            update_id=index + 1,
            text=(
                f"Explorer {index} shares a thoughtful story about the growing Dexo community. " * 4
            )[:200],
            observed_at=at + timedelta(seconds=1),
        )
    window = window_start_for(at, 900)
    settlement = settle_window(registration.pk, window, config.pk, season.pk)
    assert settlement.points == 1
    again = settle_window(registration.pk, window, config.pk, season.pk)
    assert (
        again.pk == settlement.pk
        and RewardLedger.objects.filter(source_type="CREATOR").count() == 1
    )
    request = RequestFactory().get("/api/v1/network/groups")
    request.auth = creator  # type: ignore[attr-defined]
    output = groups(request)
    assert output["items"][0]["earned_points"] == 1
    assert output["items"][0]["qualified_characters"] == 600
    assert output["items"][0]["unique_speakers"] == 3
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/creator-network-active.json").write_text(
            json.dumps(output, default=str, indent=2) + "\n"
        )


def test_feature_error_contract_and_no_unauthorized_rewards(phase4_api, monkeypatch, settings):
    from uuid import uuid4

    from apps.players.models import Player

    client, token, _, _ = phase4_api
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(token, 9001)}"}
    cases = {}

    def record(name, response, status, code):
        assert response.status_code == status, response.content
        assert response.json()["code"] == code
        cases[name] = {"http_status": status, "response": response.json()}

    record("auth_missing", client.get("/api/v1/daily"), 401, "AUTH_MISSING")
    record(
        "mission_not_available",
        client.post(
            "/api/v1/missions/no-mission/start", data="{}", content_type="application/json", **auth
        ),
        409,
        "MISSION_UNAVAILABLE",
    )
    record(
        "typing_key_required",
        client.post(
            "/api/v1/typing/attempts/start", data="{}", content_type="application/json", **auth
        ),
        422,
        "IDEMPOTENCY_KEY_REQUIRED",
    )
    record(
        "foreign_attempt",
        client.post(
            f"/api/v1/typing/attempts/{uuid4()}/submit",
            data='{"typed_text":"x"}',
            content_type="application/json",
            **auth,
        ),
        404,
        "TYPING_ATTEMPT_NOT_FOUND",
    )
    Player.objects.filter(telegram_user_id=9001).update(moderation_state="BANNED")
    record(
        "restricted_daily",
        client.post("/api/v1/daily/claim", data="{}", content_type="application/json", **auth),
        403,
        "PLAYER_RESTRICTED",
    )
    assert not RewardLedger.objects.exists()
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/errors.json").write_text(json.dumps(cases, indent=2) + "\n")
