from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.clans.models import (
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    PlayerSeasonClanState,
)
from apps.clans.services import (
    ClanConflict,
    choose_role,
    create_personal_invite,
    decide_request,
    get_or_create_rejoin_invite,
    reconcile_telegram_membership,
    request_join,
    voluntary_leave,
)
from apps.rewards.models import RewardLedger
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup


@pytest.mark.django_db
def test_roles_are_closed_idempotent_and_immutable(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, season, _, player, _ = clan_world
    row = player()
    first = choose_role(row, season, ClanRole.SOLDIER)
    assert choose_role(row, season, ClanRole.SOLDIER).pk == first.pk
    with pytest.raises(ClanConflict):
        choose_role(row, season, ClanRole.CAPTAIN)
    with pytest.raises(ClanConflict):
        choose_role(player(), season, "OFFICER")


@pytest.mark.django_db
def test_duplicate_request_and_acceptance_is_not_membership(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    assert request_join(soldier, cs, resolved).pk == req.pk
    assert decide_request(cs.captain, req.pk, accept=True).state == ClanJoinRequest.State.ACCEPTED
    assert not ClanMembership.objects.filter(player=soldier).exists()
    assert decide_request(cs.captain, req.pk, accept=True).state == ClanJoinRequest.State.ACCEPTED


@pytest.mark.django_db
def test_accept_rechecks_current_membership_and_stale_clan_state(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, season, resolved, player, clan = clan_world
    soldier, target, other = player(ClanRole.SOLDIER), clan(), clan()
    req = request_join(soldier, target, resolved)
    ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=other,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    with pytest.raises(ClanConflict):
        decide_request(target.captain, req.pk, accept=True)
    ClanMembership.objects.filter(player=soldier).delete()
    target.accepting_members = False
    target.save(update_fields=("accepting_members",))
    with pytest.raises(ClanConflict):
        decide_request(target.captain, req.pk, accept=True)


@pytest.mark.django_db
def test_activation_requires_accepted_path_and_source_time(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, season, resolved, player, clan = clan_world
    random_user, accepted, cs = player(ClanRole.SOLDIER), player(ClanRole.SOLDIER), clan()
    reconcile_telegram_membership(
        player=random_user,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=1,
        observed_at=now,
    )
    assert not ClanMembership.objects.filter(player=random_user).exists()
    req = request_join(accepted, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    reconcile_telegram_membership(
        player=accepted,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=2,
        observed_at=season.ends_at,
    )
    assert not ClanMembership.objects.filter(player=accepted).exists()
    reconcile_telegram_membership(
        player=accepted,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=3,
        observed_at=season.ends_at - timedelta(seconds=1),
    )
    assert ClanMembership.objects.filter(player=accepted, ended_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_access_transitions_do_not_extend_or_fabricate_events(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, player, clan = clan_world
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=10,
        observed_at=now,
    )
    membership = ClanMembership.objects.get(player=soldier)
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=11,
        observed_at=now,
    )
    assert not ClanMembershipEvent.objects.filter(
        membership=membership, kind="ACCESS_RESTORED"
    ).exists()
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=12,
        observed_at=now,
    )
    membership.refresh_from_db()
    deadline = membership.access_rejoin_deadline
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=13,
        observed_at=now + timedelta(hours=1),
    )
    membership.refresh_from_db()
    assert membership.access_rejoin_deadline == deadline
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=14,
        observed_at=now + timedelta(hours=2),
    )
    membership.refresh_from_db()
    assert membership.access_state == ClanMembership.Access.ACTIVE
    assert (
        ClanMembershipEvent.objects.filter(membership=membership, kind="ACCESS_RESTORED").count()
        == 1
    )


@pytest.mark.django_db
def test_rejoin_invite_is_target_only_and_idempotent(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    now, config, season, _, player, clan = clan_world
    member, stranger, cs = player(ClanRole.SOLDIER), player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(True, {"invite_link": "secret"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    first = get_or_create_rejoin_invite(member, membership)
    assert get_or_create_rejoin_invite(member, membership).pk == first.pk
    with pytest.raises(PermissionDenied):
        get_or_create_rejoin_invite(stranger, membership)


@pytest.mark.django_db
def test_invite_create_claim_and_ambiguous_outcome_are_bounded(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    calls = []

    def ambiguous(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(False, category="timeout", ambiguous=True)

    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        ambiguous,
    )
    invite = create_personal_invite(req.invite)
    assert invite.state == ClanInvite.State.UNCERTAIN
    create_personal_invite(invite)
    assert len(calls) == 1


@pytest.mark.django_db
def test_soldier_leave_is_idempotent_and_preserves_points(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    now, config, season, resolved, player, clan = clan_world
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    soldier, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    assert voluntary_leave(soldier, season, resolved) == membership
    assert voluntary_leave(soldier, season, resolved) is None
    assert RewardLedger.objects.filter(player=soldier).count() == 0
    state = PlayerSeasonClanState.objects.get(player=soldier, season=season)
    assert state.voluntary_leave_count == 1 and state.leave_cooldown_until is not None
    with pytest.raises(PermissionDenied):
        voluntary_leave(cs.captain, season, resolved)


@pytest.mark.django_db
def test_profile_first_clan_is_historical_frames_are_current(clan_world) -> None:  # type: ignore[no-untyped-def]
    from apps.profile_collection.services import entitlement, reconcile_profile

    now, config, season, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    _, facts, unlocks, _ = reconcile_profile(soldier, resolved, now)
    assert "first-clan" in unlocks and facts.active_clan and not facts.active_captain
    assert resolved.profile_collection is not None
    frame = next(x for x in resolved.profile_collection.frames if x.key == "clan")
    assert entitlement(frame, facts, set(unlocks), set())
    voluntary_leave(soldier, season, resolved)
    _, facts, unlocks, _ = reconcile_profile(soldier, resolved, now + timedelta(seconds=1))
    assert "first-clan" in unlocks and not facts.active_clan
    membership.refresh_from_db()
    assert membership.ended_at is not None
    assert RewardLedger.objects.filter(player=soldier).count() == 0


@pytest.mark.django_db
def test_captain_frame_tracks_current_captain_membership(clan_world) -> None:  # type: ignore[no-untyped-def]
    from apps.profile_collection.services import reconcile_profile

    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    _, facts, _, _ = reconcile_profile(cs.captain, resolved, now)
    assert facts.active_clan and facts.active_captain


@pytest.mark.django_db
def test_accepted_already_present_user_activates_after_fresh_check(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.services import finalize_accepted_request

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    req = decide_request(cs.captain, req.pk, accept=True)
    assert req.decided_at is not None
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "member"}),
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    assert finalize_accepted_request(req).state == ClanJoinRequest.State.ACTIVATED
    assert ClanMembership.objects.filter(player=soldier, ended_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_failed_remote_revoke_preserves_credential(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.services import queue_invite_revoke, revoke_invite_remote

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    invite = req.invite
    invite.state = ClanInvite.State.ACTIVE
    invite.invite_link = "credential-must-survive"
    invite.save()
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link",
        lambda *_: TelegramResult(False, category="rate_limited", retry_after=1),
    )
    queue_invite_revoke(invite.pk, terminal=ClanInvite.State.REVOKED)
    invite = revoke_invite_remote(invite.pk)
    assert invite.state == ClanInvite.State.REVOKE_FAILED
    assert invite.invite_link == "credential-must-survive"


@pytest.mark.django_db
def test_registration_requires_backend_group_authority_and_bot_capability(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import register_clan
    from apps.telegram.models import TelegramUserMembership

    now, _, season, resolved, player, _ = clan_world
    captain = player(ClanRole.CAPTAIN)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-79991,
        chat_type="supergroup",
        title="Eligible",
        bot_is_admin=True,
        bot_can_invite_users=True,
        first_seen_at=now,
        last_seen_at=now,
    )
    TelegramUserMembership.objects.create(
        group=group,
        telegram_user_id=captain.telegram_user_id,
        player=captain,
        status="administrator",
        is_admin=True,
        observed_at=now,
    )
    replies = iter(
        [
            TelegramResult(True, {"status": "administrator"}),
            TelegramResult(True, {"status": "administrator", "can_invite_users": True}),
        ]
    )
    monkeypatch.setattr("apps.clans.services.bot_api.get_chat_member", lambda *_: next(replies))
    monkeypatch.setattr("apps.clans.services.settings.TELEGRAM_BOT_ID", 999)
    created = register_clan(
        captain, season, resolved, group_id=group.pk, name="Eligible", tag="ELG", motto=""
    )
    assert created.captain == captain
    outsider = player(ClanRole.CAPTAIN)
    with pytest.raises(PermissionDenied):
        register_clan(
            outsider, season, resolved, group_id=group.pk, name="Other", tag="OTH", motto=""
        )


@pytest.mark.django_db
def test_registration_tag_conflict_is_domain_conflict(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.services import register_clan
    from apps.telegram.models import TelegramUserMembership

    now, _, season, resolved, player, _ = clan_world
    monkeypatch.setattr("apps.clans.services.settings.TELEGRAM_BOT_ID", 999)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "administrator", "can_invite_users": True}),
    )
    captains = [player(ClanRole.CAPTAIN), player(ClanRole.CAPTAIN)]
    groups = []
    for index, captain in enumerate(captains):
        group = TelegramGroup.objects.create(
            telegram_chat_id=-79980 - index,
            chat_type="supergroup",
            title=f"G{index}",
            bot_is_admin=True,
            bot_can_invite_users=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        TelegramUserMembership.objects.create(
            group=group,
            telegram_user_id=captain.telegram_user_id,
            player=captain,
            status="administrator",
            is_admin=True,
            observed_at=now,
        )
        groups.append(group)
    register_clan(
        captains[0], season, resolved, group_id=groups[0].pk, name="One", tag="SAME", motto=""
    )
    with pytest.raises(ClanConflict):
        register_clan(
            captains[1], season, resolved, group_id=groups[1].pk, name="Two", tag="same", motto=""
        )


@pytest.mark.django_db
def test_invite_rate_limit_honors_retry_after_without_consuming_poll_attempts(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    calls = []

    def rate_limited(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(False, category="rate_limited", retry_after=90)

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", rate_limited)
    first = create_personal_invite(req.invite)
    retry_at = first.next_create_retry_at
    assert retry_at is not None and retry_at >= timezone.now() + timedelta(seconds=85)
    create_personal_invite(first)
    create_personal_invite(first)
    first.refresh_from_db()
    assert len(calls) == 1
    assert first.attempt_count == 1


@pytest.mark.django_db
def test_explicit_invite_failure_uses_backoff_without_poll_attempts(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    calls: list[int] = []

    def unavailable(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(False, category="connection_error")

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", unavailable)
    invite = create_personal_invite(req.invite)
    assert invite.next_create_retry_at is not None
    create_personal_invite(invite)
    assert calls == [1]
    assert invite.attempt_count == 1


@pytest.mark.django_db
def test_ambiguous_join_recovery_releases_capacity_reservation(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(False, category="timeout", ambiguous=True),
    )
    invite = create_personal_invite(req.invite)
    ClanInvite.objects.filter(pk=invite.pk).update(
        uncertain_since=timezone.now()
        - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS + 1)
    )
    assert recover_invite_creations() == 1
    invite.refresh_from_db()
    req.refresh_from_db()
    assert invite.state == ClanInvite.State.MANUAL_REVIEW
    assert req.state == ClanJoinRequest.State.CANCELLED


@pytest.mark.django_db
def test_ambiguous_rejoin_recovers_with_cumulative_attempt_cap(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    now, config, season, _, player, clan = clan_world
    member, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    replies = iter(
        [
            TelegramResult(False, category="timeout", ambiguous=True),
            TelegramResult(True, {"invite_link": "recovered"}),
        ]
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link", lambda *_: next(replies)
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    invite = get_or_create_rejoin_invite(member, membership)
    ClanInvite.objects.filter(pk=invite.pk).update(
        uncertain_since=timezone.now()
        - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS + 1)
    )
    assert recover_invite_creations() == 1
    invite.refresh_from_db()
    assert invite.state == ClanInvite.State.ACTIVE
    assert invite.attempt_count == 2 <= settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS
    assert ClanMembership.objects.filter(player=member, ended_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_stale_creating_lease_enters_uncertain_recovery_without_external_call(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(pk=req.invite.pk).update(
        state=ClanInvite.State.CREATING,
        claim_expires_at=timezone.now() - timedelta(seconds=1),
    )
    calls = []
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link", lambda *_: calls.append(1)
    )
    assert recover_invite_creations() == 1
    invite = ClanInvite.objects.get(pk=req.invite.pk)
    assert invite.state == ClanInvite.State.UNCERTAIN
    assert not calls
    ClanInvite.objects.filter(pk=invite.pk).update(
        uncertain_since=timezone.now()
        - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS + 1)
    )
    assert recover_invite_creations() == 1
    invite.refresh_from_db()
    req.refresh_from_db()
    assert invite.state == ClanInvite.State.MANUAL_REVIEW
    assert req.state == ClanJoinRequest.State.CANCELLED


@pytest.mark.django_db
def test_rejoin_recovery_stops_at_cumulative_attempt_cap(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    now, config, season, _, player, clan = clan_world
    member, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    invite = ClanInvite.objects.create(
        membership=membership,
        purpose=ClanInvite.Purpose.REJOIN,
        state=ClanInvite.State.UNCERTAIN,
        attempt_count=settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS,
        uncertain_since=timezone.now()
        - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS + 1),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link", lambda *_: calls.append(1)
    )
    assert recover_invite_creations() == 1
    invite.refresh_from_db()
    assert invite.state == ClanInvite.State.MANUAL_REVIEW
    assert invite.attempt_count == settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS
    assert not calls


@pytest.mark.django_db
def test_periodic_recovery_processes_stranded_accepted_pending_invite(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    assert req.invite.state == ClanInvite.State.PENDING
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(True, {"invite_link": "recovered-pending"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "left"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    assert recover_invite_creations() == 1
    req.invite.refresh_from_db()
    assert req.invite.state == ClanInvite.State.ACTIVE
    assert req.invite.attempt_count == 1


@pytest.mark.django_db
def test_later_beat_recovers_retry_row_stranded_after_pending_commit(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    # Simulate recovery committing FAILED -> PENDING and dying before external I/O.
    ClanInvite.objects.filter(pk=req.invite.pk).update(
        state=ClanInvite.State.PENDING,
        next_create_retry_at=None,
    )
    calls: list[int] = []

    def create(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(True, {"invite_link": "later-beat"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", create)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "left"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    assert recover_invite_creations() == 1
    assert calls == [1]
    assert ClanInvite.objects.get(pk=req.invite.pk).state == ClanInvite.State.ACTIVE


@pytest.mark.django_db
def test_fresh_verification_rejects_delayed_leave_but_accepts_newer_leave(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import finalize_accepted_request

    now, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    req = decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "member"}),
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    assert finalize_accepted_request(req).state == ClanJoinRequest.State.ACTIVATED
    membership = ClanMembership.objects.get(player=soldier)
    assert membership.telegram_verified_at is not None
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=100,
        observed_at=membership.telegram_verified_at - timedelta(seconds=1),
    )
    membership.refresh_from_db()
    assert membership.access_state == ClanMembership.Access.ACTIVE
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=101,
        observed_at=membership.telegram_verified_at + timedelta(seconds=1),
    )
    membership.refresh_from_db()
    assert membership.access_state == ClanMembership.Access.ACCESS_LOST


@pytest.mark.django_db
def test_unknown_join_manual_review_blocks_same_clan_reapplication(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(False, category="timeout", ambiguous=True),
    )
    invite = create_personal_invite(req.invite)
    ClanInvite.objects.filter(pk=invite.pk).update(
        uncertain_since=timezone.now()
        - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS + 1)
    )
    assert recover_invite_creations() == 1
    req.refresh_from_db()
    invite.refresh_from_db()
    assert req.state == ClanJoinRequest.State.CANCELLED
    assert invite.state == ClanInvite.State.MANUAL_REVIEW
    assert invite.unknown_credential_risk
    with pytest.raises(ClanConflict):
        request_join(soldier, cs, resolved)
    assert (
        ClanInvite.objects.filter(
            join_request__applicant=soldier,
            join_request__clan_season=cs,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_known_create_success_racing_terminalization_is_tracked_for_revoke(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import queue_invite_revoke

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    invite = req.invite
    dispatched: list[int] = []

    def create(*_args: object) -> TelegramResult:
        queue_invite_revoke(invite.pk, terminal=ClanInvite.State.USED)
        return TelegramResult(True, {"invite_link": "must-not-be-lost"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", create)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", dispatched.append)
    result = create_personal_invite(invite)
    assert result.state == ClanInvite.State.REVOKE_PENDING
    assert result.invite_link == "must-not-be-lost"
    assert result.desired_terminal_state == ClanInvite.State.USED
    assert dispatched == [invite.pk]


@pytest.mark.django_db
def test_revoke_rate_limit_is_due_only_and_confirmed_revoke_clears_link(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import queue_invite_revoke, revoke_invite_remote

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    invite = req.invite
    ClanInvite.objects.filter(pk=invite.pk).update(
        state=ClanInvite.State.ACTIVE, invite_link="known"
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    queue_invite_revoke(invite.pk, terminal=ClanInvite.State.REVOKED)
    replies = iter(
        [
            TelegramResult(False, category="rate_limited", retry_after=60),
            TelegramResult(True, {}),
        ]
    )
    calls: list[int] = []

    def revoke(*_args: object) -> TelegramResult:
        calls.append(1)
        return next(replies)

    monkeypatch.setattr("apps.clans.services.bot_api.revoke_chat_invite_link", revoke)
    failed = revoke_invite_remote(invite.pk)
    assert failed.state == ClanInvite.State.REVOKE_FAILED
    assert failed.invite_link == "known" and failed.next_revoke_retry_at is not None
    revoke_invite_remote(invite.pk)
    assert calls == [1]
    ClanInvite.objects.filter(pk=invite.pk).update(next_revoke_retry_at=timezone.now())
    done = revoke_invite_remote(invite.pk)
    assert done.state == ClanInvite.State.REVOKED
    assert done.invite_link == ""


@pytest.mark.django_db
def test_pre_accept_event_cannot_activate_but_fresh_verification_can(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import finalize_accepted_request

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    req = decide_request(cs.captain, req.pk, accept=True)
    assert req.decided_at is not None
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=50,
        observed_at=req.decided_at - timedelta(seconds=2),
    )
    assert not ClanMembership.objects.filter(player=soldier).exists()
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "member"}),
    )
    assert finalize_accepted_request(req).state == ClanJoinRequest.State.ACTIVATED


@pytest.mark.django_db
def test_bot_membership_actor_is_discovery_candidate_but_registration_reverifies(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import eligible_groups, register_clan
    from apps.telegram.models import TelegramInboundUpdate, TelegramUserMembership
    from apps.telegram.services import process_inbound_update

    now, _, season, resolved, player, _ = clan_world
    captain = player(ClanRole.CAPTAIN)
    settings.TELEGRAM_BOT_ID = 999
    inbound = TelegramInboundUpdate.objects.create(
        update_id=990,
        payload={
            "update_id": 990,
            "my_chat_member": {
                "date": int(now.timestamp()),
                "from": {"id": captain.telegram_user_id},
                "chat": {"id": -9990, "type": "supergroup", "title": "Candidate"},
                "new_chat_member": {
                    "status": "administrator",
                    "can_invite_users": True,
                    "user": {"id": 999},
                },
            },
        },
        payload_expires_at=now + timedelta(days=1),
    )
    process_inbound_update(inbound.pk)
    assert not TelegramUserMembership.objects.filter(player=captain).exists()
    group = eligible_groups(captain, season, resolved).get(telegram_chat_id=-9990)
    calls: list[int] = []

    def verify(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(True, {"status": "administrator", "can_invite_users": True})

    monkeypatch.setattr("apps.clans.services.bot_api.get_chat_member", verify)
    registered = register_clan(
        captain, season, resolved, group_id=group.pk, name="Candidate", tag="CAND", motto=""
    )
    assert len(calls) == 2
    membership = ClanMembership.objects.get(player=captain, clan_season=registered)
    assert membership.telegram_verified_at is not None
    reconcile_telegram_membership(
        player=captain,
        group=group,
        present=False,
        telegram_update_id=991,
        observed_at=membership.telegram_verified_at - timedelta(seconds=1),
    )
    membership.refresh_from_db()
    assert membership.access_state == ClanMembership.Access.ACTIVE


@pytest.mark.django_db
def test_two_complete_rejoin_cycles_recycle_only_confirmed_safe_invite(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import get_or_create_rejoin_invite, revoke_invite_remote

    now, config, season, _, player, clan = clan_world
    member, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    links = iter(["cycle-one", "cycle-two"])
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(True, {"invite_link": next(links)}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link",
        lambda *_: TelegramResult(True, {}),
    )
    for cycle in (1, 2):
        reconcile_telegram_membership(
            player=member,
            group=cs.clan.telegram_group,
            present=False,
            telegram_update_id=cycle * 10,
            observed_at=now + timedelta(seconds=cycle * 10),
        )
        membership.refresh_from_db()
        invite = get_or_create_rejoin_invite(member, membership)
        assert invite.recovery_cycle == cycle and invite.state == ClanInvite.State.ACTIVE
        reconcile_telegram_membership(
            player=member,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=cycle * 10 + 1,
            observed_at=now + timedelta(seconds=cycle * 10 + 1),
        )
        invite = revoke_invite_remote(invite.pk)
        assert invite.state == ClanInvite.State.USED and invite.invite_link == ""
    assert ClanMembership.objects.filter(player=member).count() == 1


@pytest.mark.django_db
def test_rejoin_create_in_flight_restoration_tracks_known_success_for_revoke(
    clan_world: Any, monkeypatch: Any
) -> None:
    now, config, season, _, player, clan = clan_world
    member, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    invite = ClanInvite.objects.create(membership=membership, purpose=ClanInvite.Purpose.REJOIN)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)

    def create(*_args: object) -> TelegramResult:
        reconcile_telegram_membership(
            player=member,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=10,
            observed_at=now + timedelta(seconds=1),
        )
        return TelegramResult(True, {"invite_link": "tracked-rejoin"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", create)
    result = create_personal_invite(invite)
    assert result.state == ClanInvite.State.REVOKE_PENDING
    assert result.invite_link == "tracked-rejoin"
    assert result.desired_terminal_state == ClanInvite.State.USED


@pytest.mark.django_db
def test_rejoin_active_credential_restoration_uses_remote_revoke_lifecycle(
    clan_world: Any, monkeypatch: Any
) -> None:
    now, config, season, _, player, clan = clan_world
    member, cs = player(ClanRole.SOLDIER), clan()
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    invite = ClanInvite.objects.create(
        membership=membership,
        purpose=ClanInvite.Purpose.REJOIN,
        state=ClanInvite.State.ACTIVE,
        invite_link="known-recovery-link",
    )
    dispatched: list[int] = []
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", dispatched.append)

    reconcile_telegram_membership(
        player=member,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=10,
        observed_at=now + timedelta(seconds=1),
    )

    membership.refresh_from_db()
    invite.refresh_from_db()
    assert membership.access_state == ClanMembership.Access.ACTIVE
    assert invite.state == ClanInvite.State.REVOKE_PENDING
    assert invite.invite_link == "known-recovery-link"
    assert invite.desired_terminal_state == ClanInvite.State.USED
    assert dispatched == [invite.pk]


@pytest.mark.django_db
def test_interactive_actions_fail_closed_after_season_end(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import get_or_create_rejoin_invite, recover_invite_creations

    now, config, season, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    season.ends_at = now - timedelta(seconds=1)
    season.save(update_fields=("ends_at",))
    with pytest.raises(ClanConflict):
        decide_request(cs.captain, req.pk, accept=True)
    req.state = ClanJoinRequest.State.ACCEPTED
    req.decided_at = now - timedelta(minutes=1)
    req.save()
    invite = ClanInvite.objects.create(join_request=req, purpose=ClanInvite.Purpose.JOIN)
    calls: list[int] = []
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link", lambda *_: calls.append(1)
    )
    recover_invite_creations()
    assert not calls
    member = player(ClanRole.SOLDIER)
    membership = ClanMembership.objects.create(
        player=member,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        access_state=ClanMembership.Access.ACCESS_LOST,
        started_at=now,
        config_version=config,
    )
    with pytest.raises(ClanConflict):
        get_or_create_rejoin_invite(member, membership)
    invite.refresh_from_db()
    assert invite.state == ClanInvite.State.REVOKED


@pytest.mark.django_db
def test_pending_join_recovery_fresh_verifies_existing_member_without_invite(
    clan_world: Any, monkeypatch: Any
) -> None:
    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    creates: list[int] = []
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "member"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link", lambda *_: creates.append(1)
    )
    assert recover_invite_creations() == 1
    req.refresh_from_db()
    assert req.state == ClanJoinRequest.State.ACTIVATED
    assert ClanMembership.objects.filter(player=soldier, ended_at__isnull=True).count() == 1
    assert not creates


@pytest.mark.django_db
def test_revoke_exhaustion_is_observable_and_preserves_known_credential(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from apps.clans.services import revoke_invite_remote

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(pk=req.invite.pk).update(
        state=ClanInvite.State.REVOKE_FAILED,
        invite_link="still-live",
        desired_terminal_state=ClanInvite.State.REVOKED,
        revoke_attempts=settings.CLAN_INVITE_MAX_REVOKE_ATTEMPTS,
        next_revoke_retry_at=timezone.now(),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link", lambda *_: calls.append(1)
    )
    invite = revoke_invite_remote(req.invite.pk)
    assert invite.state == ClanInvite.State.MANUAL_REVIEW
    assert invite.unknown_credential_risk
    assert invite.invite_link == "still-live"
    assert not calls
