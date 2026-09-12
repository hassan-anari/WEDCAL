# mypy: disable-error-code="no-untyped-call"
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError

from apps.clans.governance import (
    cast_vote,
    clan_home_integrity_available,
    finalize_case,
    observe_bot_admin_state,
    open_case,
    record_clan_activity,
    request_dissolution,
    sweep_bot_admin_deadlines,
    sweep_inactivity,
)
from apps.clans.models import (
    ClanExpectedRemoval,
    ClanGovernanceCase,
    ClanGovernanceVote,
    ClanMembership,
    ClanMembershipEvent,
    ClanMembershipIncident,
    ClanRole,
    ClanSeason,
    ClanSeasonEvent,
    PlayerSeasonClanState,
)
from apps.clans.services import ClanConflict, reconcile_telegram_membership
from apps.players.models import Player

pytestmark = pytest.mark.django_db(transaction=True)


def add_member(world, cs, role=ClanRole.SOLDIER, state=Player.ModerationState.ACTIVE):  # type: ignore[no-untyped-def]
    now, config, season, _, make_player, _ = world
    player = make_player(role)
    player.moderation_state = state
    player.save(update_fields=("moderation_state",))
    return ClanMembership.objects.create(
        player=player,
        season=season,
        clan_season=cs,
        role=role,
        started_at=now,
        last_clan_active_at=now,
        next_inactivity_check_at=now + timedelta(days=3),
        config_version=config,
    )


def electorate(world, cs):  # type: ignore[no-untyped-def]
    return [add_member(world, cs) for _ in range(4)]


def test_no_warning_before_threshold(clan_world):  # type: ignore[no-untyped-def]
    now, _, _, _, _, clan = clan_world
    cs = clan()
    member = add_member(clan_world, cs)
    member.next_inactivity_check_at = now + timedelta(hours=1)
    member.save(update_fields=("next_inactivity_check_at",))
    assert sweep_inactivity(now=now) == 0
    assert not ClanMembershipEvent.objects.filter(
        membership=member, kind="INACTIVITY_WARNING"
    ).exists()


def test_soldier_warning_once_and_schedules_removal(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    member = add_member(clan_world, cs)
    member.last_clan_active_at = now - timedelta(days=3)
    member.next_inactivity_check_at = now
    member.save(update_fields=("last_clan_active_at", "next_inactivity_check_at"))
    assert sweep_inactivity(now=now) == 1
    assert sweep_inactivity(now=now) == 0
    assert (
        ClanMembershipEvent.objects.filter(membership=member, kind="INACTIVITY_WARNING").count()
        == 1
    )


def test_soldier_inactivity_removal_has_no_voluntary_penalty(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    member = add_member(clan_world, cs)
    member.last_clan_active_at = now - timedelta(days=6)
    member.next_inactivity_check_at = now
    member.save(update_fields=("last_clan_active_at", "next_inactivity_check_at"))
    sweep_inactivity(now=now)
    member.refresh_from_db()
    state = PlayerSeasonClanState.objects.get(player=member.player, season=member.season)
    assert member.end_reason == ClanMembership.EndReason.INACTIVITY_REMOVAL
    assert state.voluntary_leave_count == 0 and state.leave_cooldown_until is None
    assert ClanExpectedRemoval.objects.filter(membership=member).exists()


def test_activity_resets_warning_and_due_time(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    member = add_member(clan_world, cs)
    member.inactivity_warned_at = now - timedelta(hours=1)
    member.save(update_fields=("inactivity_warned_at",))
    record_clan_activity(member.pk, occurred_at=now + timedelta(minutes=1))
    member.refresh_from_db()
    assert member.inactivity_warned_at is None
    assert member.next_inactivity_check_at > now


def test_captain_enters_recovery_without_succession(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    captain = cs.memberships.get(role=ClanRole.CAPTAIN)
    captain.last_clan_active_at = now - timedelta(days=8)
    captain.next_inactivity_check_at = now
    captain.save(update_fields=("last_clan_active_at", "next_inactivity_check_at"))
    sweep_inactivity(now=now)
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.RECOVERY
    assert cs.captain_id == captain.player_id
    assert ClanSeasonEvent.objects.filter(clan_season=cs, kind="RECOVERY_ENTERED").exists()


def test_recovery_cancels_before_case_but_not_after(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    captain = cs.memberships.get(role=ClanRole.CAPTAIN)
    cs.state, cs.recovery_reason, cs.recovery_started_at = (
        ClanSeason.State.RECOVERY,
        "CAPTAIN_INACTIVITY",
        now,
    )
    cs.save()
    record_clan_activity(captain.pk, occurred_at=now + timedelta(minutes=1))
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.ACTIVE
    electorate(clan_world, cs)
    cs.state, cs.recovery_reason, cs.recovery_started_at = (
        ClanSeason.State.RECOVERY,
        "CAPTAIN_INACTIVITY",
        now,
    )
    cs.save()
    open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
        reason="CAPTAIN_INACTIVITY",
        now=now,
    )
    record_clan_activity(captain.pk, occurred_at=now + timedelta(minutes=2))
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.RECOVERY


def test_recovery_case_is_voteable_only_in_recovery(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    members = electorate(clan_world, cs)
    cs.state = ClanSeason.State.RECOVERY
    cs.save(update_fields=("state",))
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
        reason="CAPTAIN_INACTIVITY",
        now=now,
    )
    assert (
        cast_vote(case_id=case.pk, voter=members[0].player, choice="YES", now=now).choice == "YES"
    )
    cs.state = ClanSeason.State.ENDED
    cs.save(update_fields=("state",))
    with pytest.raises(ClanConflict):
        cast_vote(case_id=case.pk, voter=members[1].player, choice="YES", now=now)


def test_electorate_snapshot_excludes_target_and_blocked(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    allowed = [add_member(clan_world, cs) for _ in range(2)]
    add_member(clan_world, cs, state=Player.ModerationState.SUSPENDED)
    add_member(clan_world, cs, state=Player.ModerationState.BANNED)
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=target,
        now=now,
    )
    ids = set(case.electors.values_list("player_id", flat=True))
    assert target.player_id not in ids
    assert {cs.captain_id, *(m.player_id for m in allowed)} == ids


def test_snapshot_does_not_change_with_roster(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    electorate(clan_world, cs)
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=target,
        now=now,
    )
    count = case.eligible_count
    add_member(clan_world, cs)
    assert case.electors.count() == count


def test_non_elector_and_duplicate_vote_rejected(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    members = electorate(clan_world, cs)
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=target,
        now=now,
    )
    outsider = clan_world[-2]()
    with pytest.raises(PermissionDenied):
        cast_vote(case_id=case.pk, voter=outsider, choice="YES", now=now)
    cast_vote(case_id=case.pk, voter=members[0].player, choice="YES", now=now)
    with pytest.raises(ClanConflict):
        cast_vote(case_id=case.pk, voter=members[0].player, choice="NO", now=now)


def test_same_request_replays_different_target_conflicts(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    a = add_member(clan_world, cs)
    b = add_member(clan_world, cs)
    electorate(clan_world, cs)
    first = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=a,
        now=now,
    )
    assert (
        open_case(
            initiator=cs.captain,
            clan_season=cs,
            case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
            reason="SPAM",
            target=a,
            now=now,
        ).pk
        == first.pk
    )
    with pytest.raises(ClanConflict):
        open_case(
            initiator=cs.captain,
            clan_season=cs,
            case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
            reason="SPAM",
            target=b,
            now=now,
        )


@pytest.mark.parametrize("reason", ["", "INVALID", "x" * 33])
def test_invalid_removal_reason_rejected(clan_world, reason):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    electorate(clan_world, cs)
    with pytest.raises(ClanConflict):
        open_case(
            initiator=cs.captain,
            clan_season=cs,
            case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
            reason=reason,
            target=target,
            now=now,
        )


def test_integer_boundary_and_approved_removal_once(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    electorate(clan_world, cs)
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=target,
        now=now,
    )
    voters = list(case.electors.select_related("player"))
    for elector in voters[:2]:
        cast_vote(case_id=case.pk, voter=elector.player, choice="YES", now=now)
    result = finalize_case(case.pk, now=case.voting_deadline)
    assert result.quorum_met and result.approval_met
    target.refresh_from_db()
    assert target.end_reason == ClanMembership.EndReason.GOVERNANCE_REMOVAL
    assert finalize_case(case.pk, now=case.voting_deadline).pk == case.pk
    assert (
        ClanMembershipEvent.objects.filter(
            membership=target, kind=ClanMembership.EndReason.GOVERNANCE_REMOVAL
        ).count()
        == 1
    )


def test_only_captain_direct_dissolution_preserves_clan(clan_world):  # type: ignore[no-untyped-def]
    cs = clan_world[-1]()
    clan_id = cs.clan_id
    assert request_dissolution(cs.captain, cs) is None
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.ENDED and cs.clan_id == clan_id


def test_multiple_members_require_dissolution_vote(clan_world):  # type: ignore[no-untyped-def]
    cs = clan_world[-1]()
    electorate(clan_world, cs)
    case = request_dissolution(cs.captain, cs)
    assert case and case.case_type == ClanGovernanceCase.Type.CAPTAIN_DISSOLUTION


def test_authoritative_bot_loss_restore_and_ambiguity(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    observe_bot_admin_state(cs.pk, is_admin=False, authoritative=False, observed_at=now)
    cs.refresh_from_db()
    assert cs.bot_admin_lost_at is None
    observe_bot_admin_state(cs.pk, is_admin=False, authoritative=True, observed_at=now)
    cs.refresh_from_db()
    assert not clan_home_integrity_available(cs)
    observe_bot_admin_state(
        cs.pk, is_admin=True, authoritative=True, observed_at=now + timedelta(hours=1)
    )
    cs.refresh_from_db()
    assert cs.bot_admin_lost_at is None
    assert set(ClanSeasonEvent.objects.filter(clan_season=cs).values_list("kind", flat=True)) >= {
        "BOT_ADMIN_LOST",
        "BOT_ADMIN_RESTORED",
    }


def test_bot_restore_during_governance_keeps_recovery(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    electorate(clan_world, cs)
    observe_bot_admin_state(cs.pk, is_admin=False, authoritative=True, observed_at=now)
    cs.state = ClanSeason.State.RECOVERY
    cs.save(update_fields=("state",))
    open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
        reason="BOT_ADMIN_LOSS",
        now=now,
    )
    observe_bot_admin_state(
        cs.pk, is_admin=True, authoritative=True, observed_at=now + timedelta(hours=1)
    )
    cs.refresh_from_db()
    assert cs.bot_admin_lost_at is None and cs.state == ClanSeason.State.RECOVERY
    assert not clan_home_integrity_available(cs)


def test_bot_grace_expiry_enters_recovery(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    observe_bot_admin_state(cs.pk, is_admin=False, authoritative=True, observed_at=now)
    cs.bot_admin_restore_deadline = now
    cs.save(update_fields=("bot_admin_restore_deadline",))
    assert sweep_bot_admin_deadlines(now=now) == 1
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.RECOVERY


def test_unexpected_and_expected_telegram_loss(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    member = add_member(clan_world, cs)
    reconcile_telegram_membership(
        player=member.player,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=10,
        observed_at=now + timedelta(minutes=1),
        telegram_actor_user_id=999,
    )
    member.refresh_from_db()
    assert member.access_state == ClanMembership.Access.ACCESS_LOST
    assert ClanMembershipIncident.objects.get(membership=member).telegram_actor_user_id == 999
    expected_member = add_member(clan_world, cs)
    expected_member.ended_at, expected_member.end_reason = (
        now,
        ClanMembership.EndReason.INACTIVITY_REMOVAL,
    )
    expected_member.save(update_fields=("ended_at", "end_reason"))
    expected = ClanExpectedRemoval.objects.create(
        membership=expected_member,
        reason=expected_member.end_reason,
        source_type="INACTIVITY",
        source_id=str(expected_member.pk),
    )
    reconcile_telegram_membership(
        player=expected_member.player,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=11,
        observed_at=now + timedelta(minutes=1),
    )
    expected.refresh_from_db()
    assert expected.reconciled_at is not None
    assert not ClanMembershipIncident.objects.filter(membership=expected_member).exists()


def test_vote_database_constraint(clan_world):  # type: ignore[no-untyped-def]
    now, *_ = clan_world
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    electorate(clan_world, cs)
    case = open_case(
        initiator=cs.captain,
        clan_season=cs,
        case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
        reason="SPAM",
        target=target,
        now=now,
    )
    elector = case.electors.first()
    assert elector is not None
    voter = elector.player
    ClanGovernanceVote.objects.create(case=case, voter=voter, choice="YES", cast_at=now)
    with pytest.raises(IntegrityError):
        ClanGovernanceVote.objects.create(case=case, voter=voter, choice="NO", cast_at=now)
