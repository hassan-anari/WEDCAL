from datetime import timedelta
from typing import Any

import pytest

from apps.clans.missions import record_mission_progress
from apps.clans.models import (
    ClanHQXPEntry,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanMissionParticipant,
    ClanRole,
)
from apps.clans.services import (
    cancel_request,
    captain_cancel_accepted_request,
    decide_request,
    request_join,
)


def activate(member: ClanMembership) -> None:
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )


@pytest.mark.django_db
def test_individual_mission_source_is_idempotent_and_direct_hq_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activate(member)
    definition = ClanMissionDefinition.objects.create(
        key="test-individual",
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
    progress = record_mission_progress(
        definition=definition,
        membership=member,
        source_id="event-1",
        units=2,
        occurred_at=now,
        resolved_config=resolved,
    )
    replay = record_mission_progress(
        definition=definition,
        membership=member,
        source_id="event-1",
        units=2,
        occurred_at=now,
        resolved_config=resolved,
    )
    assert replay.pk == progress.pk and progress.completed_at
    assert (
        ClanHQXPEntry.objects.filter(
            idempotency_key=f"clan-mission-direct-hq:{progress.pk}"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_group_mission_rewards_every_unique_participant(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, resolved, player, clan = clan_world
    cs = clan()
    captain = cs.memberships.get()
    activate(captain)
    soldier_player = player(ClanRole.SOLDIER)
    soldier = ClanMembership.objects.create(
        player=soldier_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(minutes=1),
        config_version=config,
    )
    activate(soldier)
    definition = ClanMissionDefinition.objects.create(
        key="test-group",
        version=1,
        kind="GROUP",
        requirement="TEST",
        target_units=2,
        reward_band="GROUP",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        enabled=True,
        config_version=config,
    )
    record_mission_progress(
        definition=definition,
        membership=captain,
        source_id="g1",
        units=1,
        occurred_at=now,
        resolved_config=resolved,
    )
    progress = record_mission_progress(
        definition=definition,
        membership=soldier,
        source_id="g2",
        units=1,
        occurred_at=now,
        resolved_config=resolved,
    )
    assert (
        ClanMissionParticipant.objects.filter(
            progress=progress, reward_settlement__isnull=False
        ).count()
        == 2
    )
    assert (
        ClanHQXPEntry.objects.filter(
            idempotency_key=f"clan-mission-direct-hq:{progress.pk}"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_applicant_cancel_before_remote_creation_releases_reservation(
    clan_world: Any, monkeypatch: Any
) -> None:
    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    cancel_request(soldier, req.pk)
    req.refresh_from_db()
    req.invite.refresh_from_db()
    assert (
        req.state == ClanJoinRequest.State.CANCELLED
        and req.invite.state == ClanInvite.State.REVOKED
    )


@pytest.mark.django_db
def test_captain_cancel_before_remote_creation_releases_reservation(
    clan_world: Any, monkeypatch: Any
) -> None:
    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    captain_cancel_accepted_request(cs.captain, req.pk)
    req.refresh_from_db()
    assert req.state == ClanJoinRequest.State.CANCELLED


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [ClanInvite.State.UNCERTAIN, ClanInvite.State.REVOKE_FAILED, ClanInvite.State.MANUAL_REVIEW],
)
def test_unsafe_invite_states_keep_accepted_reservation(clan_world, state) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(join_request=req).update(state=state, unknown_credential_risk=True)
    req.refresh_from_db()
    assert req.state == ClanJoinRequest.State.ACCEPTED
