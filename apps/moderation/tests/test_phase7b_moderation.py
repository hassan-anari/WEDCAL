# mypy: disable-error-code="no-untyped-call"
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.clans.models import ClanMembership, ClanRole
from apps.moderation.models import ModerationAction, ModerationEvidence
from apps.moderation.services import (
    ModerationIdempotencyConflict,
    apply_action,
    close_case,
    create_report,
)
from apps.network.models import CreatorGroupRegistration, NetworkActivity
from apps.network.services import purge_activity_evidence
from apps.players.models import Player

pytestmark = pytest.mark.django_db(transaction=True)


def activity(world, target):  # type: ignore[no-untyped-def]
    now, config, season, _, creator, clan = world
    cs = clan(creator(ClanRole.CAPTAIN))
    registration = CreatorGroupRegistration.objects.create(
        creator=cs.captain,
        group=cs.clan.telegram_group,
        state=CreatorGroupRegistration.State.ACTIVE,
    )
    row = NetworkActivity.objects.create(
        registration=registration,
        group=registration.group,
        sender=target,
        telegram_message_id=target.pk * 10,
        telegram_update_id=target.pk * 10,
        observed_at=now,
        character_count=20,
        rewardable_chars=20,
        classification=NetworkActivity.Classification.VALID,
        text="server evidence",
        exact_fingerprint="exact",
        normalized_fingerprint="normalized",
        config_version=config,
        season=season,
        content_expires_at=now - timedelta(days=1),
        fingerprint_expires_at=now - timedelta(days=1),
    )
    return cs, row


def test_report_rejects_self_and_invalid_input(clan_world):  # type: ignore[no-untyped-def]
    reporter = clan_world[-2]()
    with pytest.raises(ValidationError):
        create_report(reporter=reporter, target=reporter, category="spam", reason="reason")
    target = clan_world[-2]()
    with pytest.raises(ValidationError):
        create_report(reporter=reporter, target=target, category="", reason="reason")


def test_evidence_must_belong_to_target_and_derives_context(clan_world):  # type: ignore[no-untyped-def]
    reporter, target, other = clan_world[-2](), clan_world[-2](), clan_world[-2]()
    cs, row = activity(clan_world, target)
    with pytest.raises(PermissionDenied):
        create_report(
            reporter=reporter,
            target=other,
            category="spam",
            reason="reason",
            network_activity_id=row.pk,
        )
    case = create_report(
        reporter=reporter,
        target=target,
        category="spam",
        reason="reason",
        network_activity_id=row.pk,
    )
    assert case.telegram_group_id == row.group_id and case.clan_season_id == cs.pk
    evidence = case.evidence.get()
    assert evidence.verified_server_observation


@pytest.mark.parametrize(
    ("initial", "action", "expected"),
    [
        (Player.ModerationState.ACTIVE, ModerationAction.Type.FLAG, Player.ModerationState.FLAGGED),
        (
            Player.ModerationState.ACTIVE,
            ModerationAction.Type.SUSPEND,
            Player.ModerationState.SUSPENDED,
        ),
        (Player.ModerationState.ACTIVE, ModerationAction.Type.BAN, Player.ModerationState.BANNED),
        (
            Player.ModerationState.FLAGGED,
            ModerationAction.Type.SUSPEND,
            Player.ModerationState.SUSPENDED,
        ),
        (Player.ModerationState.FLAGGED, ModerationAction.Type.BAN, Player.ModerationState.BANNED),
        (
            Player.ModerationState.SUSPENDED,
            ModerationAction.Type.FLAG,
            Player.ModerationState.SUSPENDED,
        ),
        (
            Player.ModerationState.SUSPENDED,
            ModerationAction.Type.BAN,
            Player.ModerationState.BANNED,
        ),
        (Player.ModerationState.BANNED, ModerationAction.Type.FLAG, Player.ModerationState.BANNED),
        (
            Player.ModerationState.BANNED,
            ModerationAction.Type.SUSPEND,
            Player.ModerationState.BANNED,
        ),
        (Player.ModerationState.BANNED, ModerationAction.Type.WARN, Player.ModerationState.BANNED),
    ],
)
def test_moderation_state_never_downgrades(clan_world, initial, action, expected):  # type: ignore[no-untyped-def]
    reporter, target = clan_world[-2](), clan_world[-2]()
    target.moderation_state = initial
    target.save(update_fields=("moderation_state",))
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    apply_action(
        case_id=case.pk,
        action_type=action,
        reason="review",
        idempotency_key=f"{initial}:{action}",
        system_actor="test",
    )
    target.refresh_from_db()
    assert target.moderation_state == expected


def test_action_idempotency_replays_and_rejects_semantic_conflict(clan_world):  # type: ignore[no-untyped-def]
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    first = apply_action(
        case_id=case.pk,
        action_type="FLAG",
        reason="review",
        idempotency_key="same",
        system_actor="test",
    )
    assert (
        apply_action(
            case_id=case.pk,
            action_type="FLAG",
            reason="review",
            idempotency_key="same",
            system_actor="test",
        ).pk
        == first.pk
    )
    with pytest.raises(ModerationIdempotencyConflict):
        apply_action(
            case_id=case.pk,
            action_type="BAN",
            reason="review",
            idempotency_key="same",
            system_actor="test",
        )


def test_remove_from_clan_requires_and_uses_case_context(clan_world):  # type: ignore[no-untyped-def]
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    with pytest.raises(ValidationError):
        apply_action(
            case_id=case.pk,
            action_type="REMOVE_FROM_CLAN",
            reason="review",
            idempotency_key="missing-context",
            system_actor="test",
        )
    cs, row = activity(clan_world, target)
    membership = ClanMembership.objects.create(
        player=target,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=timezone.now(),
        config_version=cs.config_version,
    )
    contextual = create_report(
        reporter=reporter,
        target=target,
        category="risk",
        reason="reason",
        network_activity_id=row.pk,
    )
    apply_action(
        case_id=contextual.pk,
        action_type="REMOVE_FROM_CLAN",
        reason="review",
        idempotency_key="remove",
        system_actor="test",
    )
    membership.refresh_from_db()
    assert membership.end_reason == ClanMembership.EndReason.MODERATION_REMOVAL


def test_active_and_closed_evidence_hold_content_and_metadata(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    reporter, target = clan_world[-2](), clan_world[-2]()
    _, row = activity(clan_world, target)
    case = create_report(
        reporter=reporter,
        target=target,
        category="spam",
        reason="reason",
        network_activity_id=row.pk,
    )
    assert purge_activity_evidence(now=now, limit=1) == (0, 0)
    close_case(case, now=now)
    assert purge_activity_evidence(now=now + timedelta(days=1), limit=1) == (0, 0)
    assert case.evidence_retain_until is not None
    content, fingerprints = purge_activity_evidence(
        now=case.evidence_retain_until + timedelta(seconds=1), limit=1
    )
    row.refresh_from_db()
    assert (content, fingerprints) == (1, 1)
    assert row.text is None and row.exact_fingerprint == ""


def test_bounded_purge_progresses_around_held_row(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    reporter, held_target, free_target = clan_world[-2](), clan_world[-2](), clan_world[-2]()
    _, held = activity(clan_world, held_target)
    _, free = activity(clan_world, free_target)
    create_report(
        reporter=reporter,
        target=held_target,
        category="spam",
        reason="reason",
        network_activity_id=held.pk,
    )
    assert purge_activity_evidence(now=now, limit=1) == (1, 1)
    free.refresh_from_db()
    held.refresh_from_db()
    assert free.text is None and held.text == "server evidence"


def test_evidence_model_contains_reference_not_copied_text(clan_world):  # type: ignore[no-untyped-def]
    reporter, target = clan_world[-2](), clan_world[-2]()
    _, row = activity(clan_world, target)
    case = create_report(
        reporter=reporter,
        target=target,
        category="spam",
        reason="reason",
        network_activity_id=row.pk,
    )
    evidence = ModerationEvidence.objects.get(case=case)
    assert evidence.object_id == row.pk and evidence.manual_note == ""


def test_concurrent_duplicate_action_executes_once(clan_world):  # type: ignore[no-untyped-def]
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")

    def run():  # type: ignore[no-untyped-def]
        close_old_connections()
        try:
            return apply_action(
                case_id=case.pk,
                action_type="BAN",
                reason="review",
                idempotency_key="concurrent",
                system_actor="test",
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    assert ModerationAction.objects.filter(idempotency_key="concurrent").count() == 1
