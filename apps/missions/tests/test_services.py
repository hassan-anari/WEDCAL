# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections

from apps.missions.models import MissionDefinition, PlayerMission
from apps.missions.services import (
    MissionUnavailable,
    MissionVerificationUnavailable,
    claim_mission,
    list_missions,
    start_mission,
    verify_mission,
)
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason
from apps.telegram.bot_api import TelegramResult
from apps.typing_game.models import TypingAttempt, TypingPhrase

BASE = datetime(2026, 1, 2, 12, tzinfo=UTC)


def definition(
    key: str,
    family: str,
    *,
    version: int = 1,
    repeat: str = MissionDefinition.Repeat.ONCE,
    enabled: bool = True,
    parameters: dict[str, object] | None = None,
) -> MissionDefinition:
    return MissionDefinition.objects.create(
        key=key,
        version=version,
        enabled=enabled,
        verification_family=family,
        repeat_policy=repeat,
        verification_parameters=parameters or {},
    )


@pytest.mark.django_db
def test_definition_versions_visibility_and_history_protection(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    definition(
        "visit-dalexio",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        parameters={"destination_url": "https://old.example"},
    )
    latest = definition(
        "visit-dalexio",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        version=2,
        parameters={"destination_url": "https://new.example"},
    )
    definition(
        "view-x-update",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        enabled=False,
        parameters={"destination_url": "https://disabled.example"},
    )
    future = definition(
        "join-telegram",
        MissionDefinition.Verification.TELEGRAM,
        parameters={"telegram_chat_id": -100},
    )
    future.available_from = BASE + timedelta(days=1)
    future.save()
    listed = list_missions(player, BASE)
    assert [(row["key"], row["version"]) for row in listed] == [("visit-dalexio", 2)]
    start_mission(player, "visit-dalexio", BASE)
    latest.verification_parameters = {"destination_url": "https://changed.example"}
    with pytest.raises(ValidationError, match="new mission definition version"):
        latest.save()
    with pytest.raises(ValidationError):
        definition(
            "visit-dalexio",
            MissionDefinition.Verification.TIMED_EXTERNAL,
            version=2,
            parameters={"destination_url": "https://duplicate.example"},
        )


@pytest.mark.django_db
def test_timed_external_start_retry_token_binding_and_dwell(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory(101)
    other = player_factory(102)
    definition(
        "visit-dalexio",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        parameters={"destination_url": "https://example.com"},
    )
    first = start_mission(player, "visit-dalexio", BASE)
    retry = start_mission(player, "visit-dalexio", BASE + timedelta(seconds=2))
    assert retry.progress.pk == first.progress.pk
    assert retry.progress.opened_at == BASE
    assert retry.open_token == first.open_token
    with pytest.raises(ValueError, match="token"):
        verify_mission(player, "visit-dalexio", BASE + timedelta(seconds=10), "fake")
    early = verify_mission(
        player,
        "visit-dalexio",
        BASE
        + timedelta(seconds=config.payload["missions"]["timed_external_min_dwell_seconds"] - 1),
        first.open_token,
    )
    assert early.status == PlayerMission.Status.IN_PROGRESS
    other_token = start_mission(other, "visit-dalexio", BASE).open_token
    with pytest.raises(ValueError, match="token"):
        verify_mission(player, "visit-dalexio", BASE + timedelta(seconds=10), other_token)
    ready = verify_mission(
        player,
        "visit-dalexio",
        BASE + timedelta(seconds=config.payload["missions"]["timed_external_min_dwell_seconds"]),
        first.open_token,
    )
    repeated = verify_mission(
        player, "visit-dalexio", BASE + timedelta(seconds=30), first.open_token
    )
    assert ready.status == PlayerMission.Status.REWARD_READY
    assert repeated.pk == ready.pk and repeated.verified_at == ready.verified_at
    assert not RewardLedger.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "is_member", "verified"),
    [
        ("member", None, True),
        ("administrator", None, True),
        ("creator", None, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("left", None, False),
        ("kicked", None, False),
    ],
)
def test_telegram_membership_statuses_use_server_owned_identities(
    phase4_config_factory, player_factory, season_factory, status, is_member, verified
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory(777)
    definition(
        "join-telegram",
        MissionDefinition.Verification.TELEGRAM,
        parameters={"telegram_chat_id": -12345},
    )
    member = {"status": status, "user": {"id": player.telegram_user_id}}
    if is_member is not None:
        member["is_member"] = is_member
    with patch(
        "apps.missions.services.get_chat_member", return_value=TelegramResult(True, member)
    ) as transport:
        row = verify_mission(player, "join-telegram", BASE)
    transport.assert_called_once_with(-12345, player.telegram_user_id)
    assert (row.status == PlayerMission.Status.REWARD_READY) is verified


@pytest.mark.django_db
def test_telegram_user_mismatch_and_transient_failures_fail_safely(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory(778)
    definition(
        "join-telegram",
        MissionDefinition.Verification.TELEGRAM,
        parameters={"telegram_chat_id": -99},
    )
    with (
        patch(
            "apps.missions.services.get_chat_member",
            return_value=TelegramResult(True, {"status": "member", "user": {"id": 999}}),
        ),
        pytest.raises(ValueError, match="different user"),
    ):
        verify_mission(player, "join-telegram", BASE)
    for result in (
        TelegramResult(False, category="timeout"),
        TelegramResult(False, category="connection_error"),
        TelegramResult(False, category="rate_limited", retry_after=17),
    ):
        with (
            patch("apps.missions.services.get_chat_member", return_value=result),
            pytest.raises(MissionVerificationUnavailable) as caught,
        ):
            verify_mission(player, "join-telegram", BASE)
        assert (
            caught.value.category == result.category
            and caught.value.retry_after == result.retry_after
        )
        assert PlayerMission.objects.get(player=player).status == PlayerMission.Status.IN_PROGRESS
        assert "token" not in str(caught.value).lower()


@pytest.mark.django_db
def test_internal_typing_verifier_accepts_only_valid_current_period(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    mission = definition(
        "typing-session",
        MissionDefinition.Verification.INTERNAL,
        repeat=MissionDefinition.Repeat.DAILY,
    )
    phrase = TypingPhrase.objects.create(key="p", version=1, text="text", enabled=True)
    progress = start_mission(player, mission.key, BASE).progress
    attempt = TypingAttempt.objects.create(
        player=player,
        phrase=phrase,
        season=season,
        config_version=config,
        reset_period=progress.eligibility_period,
        start_idempotency_key="attempt",
        status=TypingAttempt.Status.REJECTED,
        started_at=BASE,
        expires_at=BASE + timedelta(seconds=30),
        submitted_at=BASE + timedelta(seconds=1),
    )
    assert verify_mission(player, mission.key, BASE).status == PlayerMission.Status.IN_PROGRESS
    for status in (TypingAttempt.Status.SUSPICIOUS, TypingAttempt.Status.EXPIRED):
        attempt.status = status
        attempt.save(update_fields=["status"])
        assert verify_mission(player, mission.key, BASE).status == PlayerMission.Status.IN_PROGRESS
    attempt.status = TypingAttempt.Status.VALID
    attempt.save(update_fields=["status"])
    assert verify_mission(player, mission.key, BASE).status == PlayerMission.Status.REWARD_READY
    unsupported = definition("complete-profile", MissionDefinition.Verification.INTERNAL)
    with pytest.raises(MissionUnavailable):
        verify_mission(player, unsupported.key, BASE)


@pytest.mark.django_db
def test_mission_claim_snapshot_idempotency_and_config_boundary(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config_v1 = phase4_config_factory(
        effective_at=datetime(2026, 1, 1, tzinfo=UTC), version="mission-v1"
    )
    season = season_factory(config_v1)
    player = player_factory()
    definition(
        "visit-dalexio",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        parameters={"destination_url": "https://example.com"},
    )
    started = start_mission(player, "visit-dalexio", BASE)
    ready = verify_mission(
        player, "visit-dalexio", BASE + timedelta(seconds=10), started.open_token
    )
    assert (
        ready.reward_amount
        == config_v1.payload["missions"]["initial_definitions"][0]["reward_points"]
    )
    assert ready.season == season and ready.config_version == config_v1 and ready.verified_at
    assert not RewardLedger.objects.exists()

    def mutate(payload):
        payload["missions"]["initial_definitions"][0]["reward_points"] += 1

    phase4_config_factory(
        effective_at=BASE + timedelta(seconds=11), mutate=mutate, version="mission-v2"
    )
    claimed, created = claim_mission(player, "visit-dalexio", BASE + timedelta(seconds=12))
    retry, retry_created = claim_mission(player, "visit-dalexio", BASE + timedelta(seconds=13))
    ledger = RewardLedger.objects.get()
    assert created and not retry_created and retry.pk == claimed.pk
    assert claimed.status == PlayerMission.Status.CLAIMED
    assert ledger.config_version == config_v1 and ledger.season == season
    assert ledger.player_amount == ready.reward_amount
    assert PlayerProgression.objects.get(player=player).lifetime_points == ready.reward_amount
    assert (
        PlayerSeason.objects.get(player=player, season=season).season_points == ready.reward_amount
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_mission_claim_grants_once(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    definition(
        "visit-dalexio",
        MissionDefinition.Verification.TIMED_EXTERNAL,
        parameters={"destination_url": "https://example.com"},
    )
    started = start_mission(player, "visit-dalexio", BASE)
    verify_mission(player, "visit-dalexio", BASE + timedelta(seconds=10), started.open_token)
    barrier = Barrier(2)
    errors: list[BaseException] = []
    results: list[bool] = []

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait()
            results.append(claim_mission(player, "visit-dalexio", BASE + timedelta(seconds=11))[1])
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and sorted(results) == [False, True]
    assert RewardLedger.objects.count() == 1


@pytest.mark.django_db
def test_daily_repeat_honors_configured_reset_time(
    phase4_config_factory, player_factory, season_factory
) -> None:
    def mutate(payload):
        payload["daily"]["reset_hour"] = 6
        payload["daily"]["reset_minute"] = 30

    config = phase4_config_factory(mutate=mutate)
    season_factory(config)
    player = player_factory()
    definition(
        "typing-session",
        MissionDefinition.Verification.INTERNAL,
        repeat=MissionDefinition.Repeat.DAILY,
    )
    before = start_mission(
        player, "typing-session", datetime(2026, 1, 2, 6, 29, tzinfo=UTC)
    ).progress
    after = start_mission(
        player, "typing-session", datetime(2026, 1, 2, 6, 30, tzinfo=UTC)
    ).progress
    assert before.pk != after.pk
    assert before.eligibility_period.startswith("2026-01-01T06:30")
    assert after.eligibility_period.startswith("2026-01-02T06:30")
