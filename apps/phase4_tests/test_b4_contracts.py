# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.arena.models import RankingProjectionEvent
from apps.arena.services import rebuild_ranking
from apps.missions.admin import MissionDefinitionForm
from apps.missions.models import MissionDefinition, PlayerMission
from apps.phase4_tests.test_api import init_data, phase4_api  # noqa: F401
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason, PlayerSeasonFinalStanding
from apps.typing_game.models import TypingAttempt

pytestmark = pytest.mark.django_db(transaction=True)


def auth(token, user=9001):
    return {"HTTP_AUTHORIZATION": f"tma {init_data(token, user)}"}


def test_unbound_definition_is_rejected_and_legacy_rows_cannot_break_bootstrap(phase4_api):
    client, token, config, season = phase4_api
    invalid = MissionDefinition(
        key="unknown-production-mission",
        version=1,
        enabled=True,
        verification_family="INTERNAL",
        repeat_policy="DAILY",
    )
    with pytest.raises(ValidationError, match="matching effective GameConfig"):
        invalid.save()
    row = MissionDefinition.objects.get()
    row.verification_family = "INTERNAL"
    with pytest.raises(ValidationError):
        row.save()
    # Simulate historical ORM/SQL misuse; runtime reads must quarantine both invalid shapes.
    MissionDefinition.objects.bulk_create(
        [
            invalid,
            MissionDefinition(
                key="typing-session",
                version=99,
                enabled=True,
                verification_family="INTERNAL",
                repeat_policy="DAILY",
                verification_parameters=["invalid historical JSON"],
            ),
        ]
    )
    bootstrap = client.get("/api/v1/bootstrap", **auth(token))
    assert bootstrap.status_code == 200, bootstrap.content
    missions = client.get("/api/v1/missions", **auth(token))
    assert missions.status_code == 200
    assert [row["key"] for row in missions.json()] == ["visit-dalexio"]
    assert not RewardLedger.objects.exists()
    assert PlayerProgression.objects.get().lifetime_points == 0
    response = client.post(
        "/api/v1/missions/unknown-production-mission/start",
        data="{}",
        content_type="application/json",
        **auth(token),
    )
    assert response.status_code == 409 and response.json()["code"] == "MISSION_UNAVAILABLE"


@pytest.mark.parametrize(
    "destination",
    [
        None,
        42,
        "",
        "javascript:alert(1)",
        "https://user:secret@example.com",
        "https://example.com/\\x",
        "https://example.com/a\n",
    ],
)
def test_destination_validation_is_actionable(phase4_api, destination):
    form = MissionDefinitionForm(
        data={
            "key": "visit-dalexio",
            "version": 2,
            "verification_family": "TIMED_EXTERNAL",
            "repeat_policy": "ONCE_PER_DEFINITION_VERSION",
            "verification_parameters": json.dumps({"destination_url": destination}),
        }
    )
    assert not form.is_valid()
    assert "destination_url" in str(form.errors)
    assert "JSON object" in form.fields["verification_parameters"].help_text


def test_confirmed_claim_reveals_history_profile_and_private_contract(phase4_api):
    client, token, config, season = phase4_api
    headers = auth(token)
    fixtures = {}

    def get(name, path):
        response = client.get("/api/v1" + path, **headers)
        assert response.status_code == 200, response.content
        assert response["Cache-Control"] == "private, no-store"
        fixtures[name] = response.json()
        return response.json()

    def post(name, path, body=None, key=None):
        extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        response = client.post(
            "/api/v1" + path,
            data=json.dumps(body or {}),
            content_type="application/json",
            **headers,
            **extra,
        )
        assert response.status_code == 200, response.content
        fixtures[name] = response.json()
        return response.json()

    daily = post("daily_claim", "/daily/claim")
    evidence = daily["reward"]
    assert evidence["amount"] == daily["reward_amount"] == 80
    assert evidence["progression_before"]["lifetime_points"] == 0
    assert evidence["progression_after"]["lifetime_points"] == 80
    duplicate = post("daily_duplicate", "/daily/claim")
    assert not duplicate["reward"]["created"]
    assert duplicate["reward"]["ledger_ids"] == evidence["ledger_ids"]
    opened = post("mission_start", "/missions/visit-dalexio/start")
    PlayerMission.objects.update(opened_at=timezone.now() - timedelta(seconds=7))
    post("mission_verify", "/missions/visit-dalexio/verify", {"open_token": opened["open_token"]})
    mission = post("mission_claim", "/missions/visit-dalexio/claim")
    assert mission["reward"]["amount"] == 120
    assert mission["reward"]["progression_before"]["lifetime_points"] == 80
    assert mission["reward"]["progression_after"]["lifetime_points"] == 200
    attempt = post("typing_start", "/typing/attempts/start", key="b4-real-run")
    TypingAttempt.objects.update(started_at=timezone.now() - timedelta(seconds=5))
    result = post(
        "typing_result",
        f"/typing/attempts/{attempt['attempt_id']}/submit",
        {"typed_text": attempt["phrase_text"], "reward_amount": 999999},
    )
    assert result["status"] == "VALID"
    history = get("typing_history_unclaimed", "/typing/history?limit=1")
    assert len(history["items"]) == 1 and not history["items"][0]["claimed"]
    assert history["items"][0]["attempt_id"] == attempt["attempt_id"]
    assert history["items"][0]["reward_amount"] == result["reward_amount"]
    assert history["items"][0]["score"] == result["score"]
    assert result["score"] == (result["accuracy_bps"] * max(1, result["wpm"]) + 50) // 100
    claim = post("typing_claim", "/typing/claim", key="b4-real-claim")
    assert claim["reward"]["amount"] == result["reward_amount"]
    assert get("typing_history_claimed", "/typing/history")["items"][0]["claimed"]
    assert not post("typing_duplicate", "/typing/claim", key="b4-real-claim")["reward"]["created"]
    assert post("typing_empty_claim", "/typing/claim", key="b4-empty-claim")["reward"] is None
    overview = get("profile_overview", "/profile/overview")
    achievements = get("profile_achievements", "/profile/achievements")
    collection = get("profile_collection", "/profile/collection")
    assert any(x["key"] == "first-mission" and x["unlocked"] for x in achievements["achievements"])
    assert all(
        not x["unlocked"]
        for x in achievements["achievements"]
        if x["key"] in {"first-war", "first-victory"}
    )
    assert overview["progression"]["lifetime_points"] == sum(
        RewardLedger.objects.values_list("player_amount", flat=True)
    )
    denied = client.put(
        "/api/v1/profile/customization",
        data=json.dumps({"active_frame": "captain", "active_title": "explorer"}),
        content_type="application/json",
        **headers,
    )
    assert denied.status_code == 422 and denied.json()["code"] == "CUSTOMIZATION_INVALID"
    equipped = client.put(
        "/api/v1/profile/customization",
        data=json.dumps(
            {
                "active_frame": "standard",
                "active_title": "explorer",
                "featured_achievement": "first-mission",
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert equipped.status_code == 200
    fixtures["customization"] = equipped.json()
    assert collection["owned_medal_count"] == 0
    assert client.get("/api/v1/typing/history", **auth(token, 9002)).json()["items"] == []
    assert client.get("/api/v1/typing/history?limit=101", **headers).status_code == 422
    assert client.get("/api/v1/typing/history").status_code == 401
    get("arena_state", "/arena/state")
    dlx = get("dlx_empty", "/arena/dlx")
    assert dlx["confirmed"] == 0 and not dlx["claimable_enabled"] and not dlx["on_chain"]
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/profile-earn.json").write_text(json.dumps(fixtures, indent=2) + "\n")


def test_live_ranking_projection_lag_and_archive_use_frozen_score(
    phase4_api, close_season_for_test
):
    client, token, config, season = phase4_api
    cache.clear()
    # Authentication provisions the real player and progression; no fabricated rank fallback.
    client.get("/api/v1/bootstrap", **auth(token))
    player = Player.objects.get(telegram_user_id=9001)
    row = PlayerSeason.objects.create(
        player=player,
        season=season,
        season_points=10,
        score_reached_at=timezone.now(),
        ranking_generation=1,
    )
    assert rebuild_ranking(season, "PLAYER")
    response = client.get("/api/v1/arena/rankings/players?limit=1", **auth(token))
    assert response.status_code == 200, response.content
    live = response.json()
    assert live["own_standing"]["rank"] == 1 and live["items"][0]["season_points"] == 10
    assert live["total_count"] == 1 and live["next_offset"] is None
    PlayerSeason.objects.filter(pk=row.pk).update(season_points=20, ranking_generation=2)
    stale = client.get("/api/v1/arena/rankings/players", **auth(token))
    assert stale.status_code == 503 and stale.json()["code"] == "RANKING_RECONCILING"
    assert rebuild_ranking(season, "PLAYER")
    RankingProjectionEvent.objects.create(
        season=season,
        entity_type="PLAYER",
        entity_id=player.pk,
        score=20,
        score_reached_at=timezone.now(),
        operation="UPSERT",
        generation=2,
        source_id="pending",
    )
    assert client.get("/api/v1/arena/rankings/players", **auth(token)).status_code == 503
    assert row.score_reached_at is not None
    PlayerSeasonFinalStanding.objects.create(
        season=season,
        player=player,
        final_rank=1,
        final_season_points=15,
        score_reached_at=row.score_reached_at,
        ranking_generation=2,
        ranked_population=1,
        finalized_at=timezone.now(),
    )
    close_season_for_test(season)
    archived = client.get(f"/api/v1/arena/rankings/players?season={season.code}", **auth(token))
    assert archived.status_code == 200, archived.content
    assert archived.json()["archived"] and archived.json()["items"][0]["season_points"] == 15
    assert (
        client.get("/api/v1/arena/rankings/players?season=unknown", **auth(token)).status_code
        == 409
    )
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/rankings.json").write_text(
            json.dumps(
                {"live": live, "archived": archived.json(), "reconciling": stale.json()}, indent=2
            )
            + "\n"
        )


def test_existing_150_xp_and_reward_ledger_survive_profile_and_new_claims(phase4_api):
    from apps.game_config.services import resolve_game_config_version
    from apps.rewards.services import settle_player_reward

    client, token, config, season = phase4_api
    headers = auth(token)
    client.get("/api/v1/bootstrap", **headers)
    player = Player.objects.get(telegram_user_id=9001)
    ledger, _ = settle_player_reward(
        player=player,
        source_type="MISSION",
        source_id="existing-legitimate-150",
        context="MISSION",
        amount=150,
        idempotency_key="existing-legitimate-150",
        occurred_at=timezone.now(),
        resolved_config=resolve_game_config_version(config.pk),
    )
    original = RewardLedger.objects.filter(pk=ledger.pk).values().get()
    for path in ["/profile/overview", "/profile/achievements", "/profile/collection"]:
        assert client.get("/api/v1" + path, **headers).status_code == 200
    response = client.post(
        "/api/v1/daily/claim", data="{}", content_type="application/json", **headers
    )
    assert response.status_code == 200
    assert response.json()["reward"]["progression_before"]["lifetime_points"] == 150
    assert response.json()["canonical"]["progression"]["lifetime_points"] == 230
    assert RewardLedger.objects.filter(pk=ledger.pk).values().get() == original
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 230


def test_typing_history_paginates_real_rejected_and_valid_runs_and_omits_unfinished(phase4_api):
    from apps.typing_game.services import start_attempt, submit_attempt

    client, token, config, season = phase4_api
    headers = auth(token)
    client.get("/api/v1/bootstrap", **headers)
    player = Player.objects.get(telegram_user_id=9001)
    now = timezone.now()
    first, _ = start_attempt(player, "history-rejected", now - timedelta(seconds=10))
    rejected, _ = submit_attempt(player, first.public_id, "", now - timedelta(seconds=5))
    second, _ = start_attempt(player, "history-valid", now - timedelta(seconds=5))
    valid, _ = submit_attempt(player, second.public_id, second.phrase.text, now)
    repeated, created = submit_attempt(player, second.public_id, "conflicting replay", now)
    assert repeated.pk == valid.pk and not created
    abandoned, _ = start_attempt(player, "history-abandoned", now)
    page = client.get("/api/v1/typing/history?limit=1", **headers).json()
    assert page["items"][0]["attempt_id"] == str(valid.public_id)
    assert page["next_cursor"]
    next_page = client.get(
        f"/api/v1/typing/history?limit=1&cursor={page['next_cursor']}", **headers
    ).json()
    assert next_page["items"][0]["attempt_id"] == str(rejected.public_id)
    assert next_page["items"][0]["status"] == "REJECTED"
    assert next_page["items"][0]["reward_amount"] == 0 and next_page["next_cursor"] is None
    assert len(client.get("/api/v1/typing/history", **headers).json()["items"]) == 2
    assert abandoned.submitted_at is None
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/typing-history.json").write_text(
            json.dumps(
                {
                    "first_page": page,
                    "last_page": next_page,
                    "unfinished_is_omitted": str(abandoned.public_id),
                    "duplicate_created": created,
                },
                indent=2,
            )
            + "\n"
        )


def test_dlx_api_reports_only_confirmed_allocation_and_audited_adjustments(
    client, settings, phase4_config_factory, season_factory, player_factory
):
    from apps.arena.adjustments import record_adjustment
    from apps.arena.tests.test_dlx_corrections import settled_allocation

    settings.TELEGRAM_BOT_TOKEN = "123:isolated-dlx"
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory(9001)
    PlayerProgression.objects.get_or_create(
        player=player, defaults={"config_version": config, "dexo_stage": "newborn"}
    )
    allocation = settled_allocation(config, season, player)
    for kind, amount in [("HOLD", 10), ("RELEASE_HOLD", 3), ("FORFEIT", 5), ("REVERSAL", 2)]:
        record_adjustment(
            allocation=allocation,
            kind=kind,
            amount=amount,
            reason_code="SYNTHETIC_REVIEW",
            idempotency_key="b4-dlx-" + kind,
            system_actor="isolated-test",
            evidence_reference="fixture",
        )
    response = client.get(
        f"/api/v1/arena/dlx?season={season.code}", **auth(settings.TELEGRAM_BOT_TOKEN)
    )
    assert response.status_code == 200, response.content
    values = response.json()
    assert values["confirmed"] == 60 and values["held"] == 7 and values["forfeited"] == 3
    assert values["net_confirmed"] == 57 and values["claimable"] == 0
    assert not values["on_chain"] and values["price_status"] == "UNAVAILABLE"
    assert (
        not RewardLedger.objects.exists()
        and PlayerProgression.objects.get(player=player).lifetime_points == 0
    )
    other = client.get(
        f"/api/v1/arena/dlx?season={season.code}", **auth(settings.TELEGRAM_BOT_TOKEN, 9002)
    )
    assert other.json()["confirmed"] == 0
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/dlx-confirmed.json").write_text(json.dumps(values, indent=2) + "\n")


def test_invitee_and_group_images_return_only_private_capabilities_and_actor_data(
    phase4_api, settings, player_factory
):
    from apps.network.models import CreatorGroupRegistration
    from apps.players.avatars import avatar_url, fingerprint
    from apps.players.models import GroupAvatar, PlayerAvatar
    from apps.referrals.models import ReferralRelation
    from apps.telegram.models import TelegramGroup

    client, token, config, season = phase4_api
    headers = auth(token)
    client.get("/api/v1/bootstrap", **headers)
    creator = Player.objects.get(telegram_user_id=9001)
    invitee = player_factory(10001)
    invitee.photo_url = "https://t.me/i/userpic/unsafe-direct-fallback.jpg"
    invitee.save(update_fields=["photo_url"])
    ReferralRelation.objects.create(inviter=creator, invitee=invitee, attributed_at=timezone.now())
    settings.AVATAR_CACHE_ENABLED = True
    settings.AVATAR_S3_BUCKET = "isolated-private"
    settings.AVATAR_PUBLIC_API_ORIGIN = "https://api.example.com"
    # Metadata is never returned in the private invitee field, including during cache misses.
    assert avatar_url(invitee, private_only=True) == ""
    response = client.get("/api/v1/referrals/invitees", **headers)
    assert response.json()["items"][0]["photo_url"] == ""
    image = PlayerAvatar.objects.get(player=invitee)
    image.object_key = "avatars/isolated.jpg"
    image.expires_at = timezone.now() + timedelta(hours=1)
    image.source_fingerprint = fingerprint(invitee)
    image.save()
    # A /start-only invitee needs no Mini App metadata for its cached photo.
    invitee.photo_url = ""
    invitee.save(update_fields=["photo_url"])
    image.source_fingerprint = fingerprint(invitee)
    image.save()
    assert invitee.first_mini_app_at is None
    invitees = client.get("/api/v1/referrals/invitees", **headers).json()
    assert invitees["items"][0]["photo_url"].startswith(
        "https://api.example.com/api/v1/profile/avatar/"
    )
    settings.GROUP_IMAGE_CACHE_ENABLED = True
    settings.GROUP_IMAGE_S3_ENDPOINT_URL = "https://s3.example.com"
    settings.GROUP_IMAGE_S3_BUCKET = "isolated-groups"
    settings.GROUP_IMAGE_S3_ACCESS_KEY_ID = "synthetic-group-key"
    settings.GROUP_IMAGE_S3_SECRET_ACCESS_KEY = "synthetic-group-secret"
    group = TelegramGroup.objects.create(
        telegram_chat_id=-10001,
        title="Synthetic connected community",
        chat_type="supergroup",
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    CreatorGroupRegistration.objects.create(
        creator=creator, group=group, next_verification_at=timezone.now() + timedelta(hours=1)
    )
    GroupAvatar.objects.create(
        group=group,
        object_key="group-images/12345678.jpg",
        expires_at=timezone.now() + timedelta(hours=1),
        refresh_after=timezone.now() + timedelta(hours=1),
    )
    groups = client.get("/api/v1/network/groups", **headers).json()
    assert groups["items"][0]["photo_url"].startswith(
        "https://api.example.com/api/v1/profile/group-avatar/"
    )
    for path in ["/referrals/invitees", "/network/groups"]:
        foreign = client.get("/api/v1" + path, **auth(token, 9002))
        assert foreign.status_code == 200 and foreign.json()["items"] == []
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/private-images.json").write_text(
            json.dumps({"invitees": invitees, "groups": groups}, indent=2) + "\n"
        )


def test_notification_http_navigation_read_state_and_typed_errors(phase4_api):
    from apps.notifications.models import Notification

    client, token, config, season = phase4_api
    headers = auth(token)
    client.get("/api/v1/bootstrap", **headers)
    player = Player.objects.get(telegram_user_id=9001)
    notification = Notification.objects.create(
        recipient=player,
        event_key="CLAN_ACCESS_LOST",
        source_domain="CLAN",
        source_type="SYNTHETIC_REVIEW",
        source_id="b4-fixture",
        idempotency_key="b4-notification-fixture",
        title="Clan access changed",
        body="Your access needs review.",
        payload={"url": "javascript:alert(1)"},
    )
    page = client.get("/api/v1/notifications", **headers)
    assert page.status_code == 200 and page.json()["unread_count"] == 1
    assert page.json()["items"][0]["deep_link"] == {"path": "/clan"}
    foreign = client.post(
        f"/api/v1/notifications/{notification.public_id}/read", **auth(token, 9002)
    )
    assert foreign.status_code == 404
    read = client.post("/api/v1/notifications/read-all", **headers)
    assert read.json() == {"updated_count": 1, "unread_count": 0}
    assert client.post("/api/v1/notifications/read-all", **headers).json()["updated_count"] == 0
    auth_error = client.get("/api/v1/typing/history")
    bad_page = client.get("/api/v1/typing/history?cursor=-1", **headers)
    bad_season = client.get("/api/v1/arena/dlx?season=missing", **headers)
    assert auth_error.status_code == 401
    assert bad_page.status_code == 422
    assert bad_season.status_code == 409
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/notifications-errors.json").write_text(
            json.dumps(
                {
                    "notifications": page.json(),
                    "read_all": read.json(),
                    "errors": [
                        {"status": response.status_code, "body": response.json()}
                        for response in [auth_error, bad_page, bad_season, foreign]
                    ],
                },
                indent=2,
            )
            + "\n"
        )


def test_add_bot_intent_keeps_bodyless_compatibility_and_requires_real_boolean(
    phase4_api, settings
):
    from apps.network.models import CreatorRegistrationIntent

    client, token, config, season = phase4_api
    settings.TELEGRAM_BOT_USERNAME = "SyntheticB4Bot"
    headers = auth(token)
    response = client.generic("POST", "/api/v1/network/groups/intents", data=b"", **headers)
    assert response.status_code == 200, response.content
    assert not CreatorRegistrationIntent.objects.get().authorize_group_messages
    invalid = client.post(
        "/api/v1/network/groups/intents",
        data=json.dumps({"authorize_group_messages": "true"}),
        content_type="application/json",
        **headers,
    )
    assert invalid.status_code == 422
    accepted = client.post(
        "/api/v1/network/groups/intents",
        data=json.dumps({"authorize_group_messages": True}),
        content_type="application/json",
        **headers,
    )
    assert accepted.status_code == 200
    assert CreatorRegistrationIntent.objects.filter(authorize_group_messages=True).count() == 1


def test_typing_current_history_reset_pagination_and_independent_timezone(
    client, settings, monkeypatch, phase4_config_factory, season_factory, player_factory
):
    from apps.core.periods import resolve_daily_period
    from apps.telegram.auth import verify_init_data
    from apps.typing_game.models import TypingPhrase
    from apps.typing_game.services import start_attempt, submit_attempt

    clock = [datetime(2026, 1, 2, 9, 59, 50, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    # Retain real HMAC/auth verification, injecting only the deterministic server clock.
    monkeypatch.setattr(
        "apps.telegram.mini_app_auth.verify_init_data",
        lambda raw, **kwargs: verify_init_data(raw, now=clock[0], **kwargs),
    )
    settings.TELEGRAM_BOT_TOKEN = "123:isolated-period-history"

    def policy(payload):
        payload["typing"]["reset_timezone"] = "Pacific/Honolulu"
        payload["daily"]["reset_timezone"] = "UTC"

    config = phase4_config_factory(effective_at=clock[0] - timedelta(days=4), mutate=policy)
    season_factory(
        config, starts_at=clock[0] - timedelta(days=3), ends_at=clock[0] + timedelta(days=42)
    )
    player = player_factory(9001)
    TypingPhrase.objects.create(
        key="period-history", version=1, text="Dexo explorers type", enabled=True
    )
    period = resolve_daily_period(clock[0], "Pacific/Honolulu")
    daily = resolve_daily_period(clock[0], "UTC")
    assert period.start != daily.start

    def run(key, at):
        attempt, _ = start_attempt(player, key, at)
        submitted, _ = submit_attempt(
            player, attempt.public_id, attempt.phrase.text, at + timedelta(seconds=5)
        )
        return submitted

    previous = run("previous-period", period.start - timedelta(seconds=10))
    early = run("current-early", daily.start + timedelta(seconds=1))
    recent = run("current-recent", clock[0] - timedelta(seconds=10))

    def get(user=9001, query=""):
        response = client.get(
            "/api/v1/typing/history?period=current" + query,
            **auth(settings.TELEGRAM_BOT_TOKEN, user),
        )
        assert response.status_code == 200, response.content
        return response.json()

    first = get(query="&limit=1")
    assert first["period"] == "current" and first["period_key"] == period.key
    assert first["items"][0]["attempt_id"] == str(recent.public_id)
    assert first["items"][0]["reset_period"] == period.key
    second = get(query=f"&limit=1&cursor={first['next_cursor']}")
    assert [row["attempt_id"] for row in second["items"]] == [str(early.public_id)]
    assert second["next_cursor"] is None
    empty = get(user=9002)
    assert empty["items"] == [] and empty["period_key"] == period.key
    foreign = client.get(
        f"/api/v1/typing/history?period=current&cursor={first['next_cursor']}",
        **auth(settings.TELEGRAM_BOT_TOKEN, 9002),
    )
    assert foreign.status_code == 422
    assert previous.reset_period != period.key
    # A real run started just before reset retains its original quota/reward provenance.
    clock[0] = period.end - timedelta(seconds=1)
    crossing, _ = start_attempt(player, "crosses-reset", clock[0])
    clock[0] = period.end
    boundary = get()
    assert boundary["items"] == [] and boundary["period_key"] != first["period_key"]
    stale = client.get(
        f"/api/v1/typing/history?period=current&cursor={first['next_cursor']}",
        **auth(settings.TELEGRAM_BOT_TOKEN),
    )
    assert stale.status_code == 409 and stale.json()["code"] == "TYPING_PERIOD_CHANGED"
    clock[0] += timedelta(seconds=4)
    submit_attempt(player, crossing.public_id, crossing.phrase.text, clock[0])
    assert get()["items"] == []  # Submitted after reset, but belongs to previous Typing period.
    clock[0] += timedelta(seconds=1)
    new = run("new-period", clock[0])
    clock[0] += timedelta(seconds=5)
    after = get()
    assert [row["attempt_id"] for row in after["items"]] == [str(new.public_id)]
    all_runs = client.get(
        "/api/v1/typing/history?period=all", **auth(settings.TELEGRAM_BOT_TOKEN)
    ).json()
    assert len(all_runs["items"]) == 5 and all_runs["period_key"] is None
    assert all_runs["period_start"] is None and all_runs["period_end"] is None
    assert (
        client.get(
            "/api/v1/typing/history?period=guessed", **auth(settings.TELEGRAM_BOT_TOKEN)
        ).status_code
        == 422
    )
    assert not RewardLedger.objects.exists()  # Wallet credit is still separate from canonical XP.
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/typing-periods.json").write_text(
            json.dumps(
                {
                    "first_page": first,
                    "last_page": second,
                    "empty": empty,
                    "exact_reset": boundary,
                    "stale_cursor": stale.json(),
                    "new_period": after,
                    "all": all_runs,
                },
                indent=2,
            )
            + "\n"
        )
