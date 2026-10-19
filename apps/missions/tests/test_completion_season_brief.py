"""The approved website flow reuses timed verification and canonical mission claims."""

# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.clans.tests.test_b6_admin import control
from apps.clans.tests.test_phase8_concurrency import parallel
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.missions.admin_workflow import validate_definition
from apps.missions.models import MissionDefinition, PlayerMission
from apps.missions.services import (
    SEASON_BRIEF_DESTINATION,
    MissionUnavailable,
    claim_mission,
    list_missions,
    start_mission,
    verify_mission,
)
from apps.notifications.models import OperatorAuditEvent
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason


def external_policy(payload):
    for rule in payload["missions"]["initial_definitions"]:
        if rule["id"] == "read-season-brief":
            rule["verification"] = "TIMED_EXTERNAL"


@pytest.fixture
def brief(phase4_config_factory, season_factory, player_factory):
    now = timezone.now()
    old = phase4_config_factory(effective_at=now - timedelta(days=2), version="old-brief")
    season = season_factory(old, starts_at=now - timedelta(days=1))
    legacy = MissionDefinition.objects.create(
        key="read-season-brief",
        version=1,
        enabled=True,
        verification_family="INTERNAL",
        repeat_policy=MissionDefinition.Repeat.ONCE,
    )
    player = player_factory(950001)
    config = phase4_config_factory(
        effective_at=now - timedelta(hours=1), version="external-brief", mutate=external_policy
    )
    definition = MissionDefinition.objects.create(
        key="read-season-brief",
        version=2,
        verification_family="TIMED_EXTERNAL",
        repeat_policy=MissionDefinition.Repeat.ONCE,
        verification_parameters={"destination_url": SEASON_BRIEF_DESTINATION},
        title="Season Brief",
    )
    return now, old, config, season, legacy, definition, player


@pytest.mark.django_db
def test_controlled_activation_and_authenticated_official_flow(
    brief, client, settings, init_data_factory, telegram_bot_token
):
    now, old, config, season, legacy, definition, player = brief
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    actor = User.objects.create_user("brief-operator", is_staff=True)
    actor.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="missions",
            codename__in=[
                "view_missiondefinition",
                "change_missiondefinition",
                "activate_missiondefinition",
            ],
        )
    )
    client.force_login(actor)
    assert control(client, "missions/missiondefinition", definition).status_code == 302
    assert OperatorAuditEvent.objects.filter(action="MISSION_ACTIVATE").count() == 1
    client.logout()
    assert (
        client.post(
            "/api/v1/missions/read-season-brief/start", data={}, content_type="application/json"
        ).status_code
        == 401
    )
    auth = {"HTTP_AUTHORIZATION": "tma " + init_data_factory(user={"id": player.telegram_user_id})}
    listed = client.get("/api/v1/missions", **auth).json()
    assert listed[0]["state"] == "AVAILABLE" and listed[0]["available"]
    assert listed[0]["destination_url"] == SEASON_BRIEF_DESTINATION
    started = client.post(
        "/api/v1/missions/read-season-brief/start", data={}, content_type="application/json", **auth
    )
    assert started.status_code == 200
    body = started.json()
    progress = PlayerMission.objects.get(player=player, definition=definition)
    assert progress.opened_at is not None
    assert body["destination_url"] == SEASON_BRIEF_DESTINATION and body["open_token"]
    assert now <= progress.opened_at <= timezone.now()
    for token in [None, "forged"]:
        invalid = client.post(
            "/api/v1/missions/read-season-brief/verify",
            data={"open_token": token},
            content_type="application/json",
            **auth,
        )
        assert invalid.status_code == 422
    retry = start_mission(player, definition.key, now + timedelta(seconds=2))
    assert retry.progress.pk == progress.pk and retry.progress.opened_at == progress.opened_at
    assert retry.open_token == body["open_token"]
    dwell = config.payload["missions"]["timed_external_min_dwell_seconds"]
    early = verify_mission(
        player,
        definition.key,
        progress.opened_at + timedelta(seconds=dwell - 1),
        body["open_token"],
    )
    assert early.status == "IN_PROGRESS"
    ready = verify_mission(
        player, definition.key, progress.opened_at + timedelta(seconds=dwell), body["open_token"]
    )
    assert ready.status == "REWARD_READY" and not RewardLedger.objects.exists()
    assert ready.verified_at is not None
    first, created = claim_mission(player, definition.key, ready.verified_at)
    replay, repeated = claim_mission(player, definition.key, ready.verified_at)
    assert created and not repeated and first.pk == replay.pk
    assert RewardLedger.objects.filter(player=player).count() == 1
    assert first.config_version_id == config.pk
    assert (
        first.reward_amount
        == resolve_effective_game_config(now).missions.definition(definition.key).reward_points
    )
    assert PlayerProgression.objects.get(player=player).lifetime_points == first.reward_amount
    assert (
        PlayerSeason.objects.get(player=player, season=season).season_points == first.reward_amount
    )
    old.refresh_from_db()
    legacy.refresh_from_db()
    assert old.payload["missions"]["initial_definitions"][-1]["verification"] == "INTERNAL"
    assert legacy.verification_family == "INTERNAL"
    http_claim = client.post(
        "/api/v1/missions/read-season-brief/claim", data={}, content_type="application/json", **auth
    )
    assert http_claim.status_code == 200 and http_claim.json()["created"] is False
    Path("work").mkdir(exist_ok=True)
    Path("work/phase2-mission-http.json").write_text(
        json.dumps(
            {"test_only": True, "list": listed, "start": body, "claim_retry": http_claim.json()},
            indent=2,
        )
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "http://dexoland.dalexio.com/",
        "https://example.com/",
        "https://dexoland.dalexio.com.evil.example/",
        "https://dexoland.dalexio.com/?redirect=elsewhere",
    ],
)
def test_wrong_destination_cannot_activate_or_start(brief, url):
    now, _, _, _, _, definition, player = brief
    definition.verification_parameters = {"destination_url": url}
    definition.save()
    with pytest.raises(MissionUnavailable):
        validate_definition(definition, activating=True)
    definition.enabled = True
    definition.save()
    assert list_missions(player, now)[0]["available"] is False
    with pytest.raises(MissionUnavailable):
        start_mission(player, definition.key, now)
    assert not PlayerMission.objects.exists() and not RewardLedger.objects.exists()


@pytest.mark.django_db
def test_definition_version_cutover_preserves_ready_obligation(brief):
    now, _, config, _, _, definition, player = brief
    definition.enabled = True
    definition.save()
    opened = start_mission(player, definition.key, now)
    ready = verify_mission(player, definition.key, now + timedelta(seconds=10), opened.open_token)
    definition.enabled = False
    definition.save()
    replacement = MissionDefinition.objects.create(
        key=definition.key,
        version=3,
        enabled=True,
        verification_family="TIMED_EXTERNAL",
        repeat_policy=MissionDefinition.Repeat.ONCE,
        verification_parameters={"destination_url": SEASON_BRIEF_DESTINATION},
    )
    with pytest.raises(ValidationError), transaction.atomic():
        definition.verification_parameters = {"destination_url": "https://example.com/"}
        definition.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        MissionDefinition.objects.filter(pk=definition.pk).update(verification_family="INTERNAL")
    claimed, created = claim_mission(player, definition.key, now + timedelta(seconds=11))
    assert created and claimed.pk == ready.pk and claimed.config_version_id == config.pk
    new_start = start_mission(player, definition.key, now + timedelta(seconds=12))
    with pytest.raises(ValueError, match="token"):
        verify_mission(player, definition.key, now + timedelta(seconds=25), opened.open_token)
    assert new_start.progress.definition_id == replacement.pk
    assert RewardLedger.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_claim_after_lost_response_awards_once(brief):
    now, _, _, _, _, definition, player = brief
    definition.enabled = True
    definition.save()
    opened = start_mission(player, definition.key, now)
    verify_mission(player, definition.key, now + timedelta(seconds=10), opened.open_token)
    results = parallel(
        [
            lambda: claim_mission(player, definition.key, now + timedelta(seconds=11))
            for _ in range(3)
        ]
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert sum(r[1] for r in results) == 1
    assert RewardLedger.objects.count() == 1
    assert GameConfigVersion.objects.filter(status="PUBLISHED").count() == 2


@pytest.mark.django_db
def test_disabled_policy_cannot_verify_in_progress_brief(brief, phase4_config_factory):
    now, _, _, _, _, definition, player = brief
    definition.enabled = True
    definition.save()
    opened = start_mission(player, definition.key, now)

    def disabled(payload):
        external_policy(payload)
        payload["missions"]["enabled"] = False

    phase4_config_factory(
        effective_at=now + timedelta(seconds=1), mutate=disabled, version="disabled-brief"
    )
    with pytest.raises(MissionUnavailable):
        verify_mission(player, definition.key, now + timedelta(seconds=10), opened.open_token)
    opened.progress.refresh_from_db()
    assert opened.progress.status == "IN_PROGRESS"


@pytest.mark.django_db
def test_deactivation_before_ready_transition_cannot_create_entitlement(brief, monkeypatch):
    from apps.missions import services

    now, _, _, _, _, definition, player = brief
    definition.enabled = True
    definition.save()
    opened = start_mission(player, definition.key, now)
    original = services._mark_ready

    def deactivate_then_mark(*args):
        MissionDefinition.objects.filter(pk=definition.pk).update(enabled=False)
        return original(*args)

    monkeypatch.setattr(services, "_mark_ready", deactivate_then_mark)
    with pytest.raises(MissionUnavailable):
        verify_mission(player, definition.key, now + timedelta(seconds=10), opened.open_token)
    opened.progress.refresh_from_db()
    assert opened.progress.status == "IN_PROGRESS"
