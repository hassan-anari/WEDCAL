from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier
from typing import Any

import pytest
from django.db import close_old_connections

from apps.clans.economy import activate_item, purchase_item, settle_clan_reward
from apps.clans.models import ClanInventory, ClanMembershipEvent


def parallel(calls: list[Callable[[], Any]]) -> list[Any]:
    barrier = Barrier(len(calls))

    def run(call: Callable[[], Any]) -> Any:
        close_old_connections()
        try:
            barrier.wait()
            return call()
        except Exception as exc:
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [future.result(timeout=20) for future in [pool.submit(run, call) for call in calls]]


def activated(member) -> None:  # type: ignore[no-untyped-def]
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )


@pytest.mark.django_db(transaction=True)
def test_duplicate_economic_source_concurrency_settles_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    results = parallel(
        [
            lambda: settle_clan_reward(
                membership=member,
                source_type="CLAN_ACTIVITY",
                source_id="race",
                gross_amount=10,
                idempotency_key="economic-race",
                occurred_at=now,
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    assert {result[0].pk for result in results}.__len__() == 1


@pytest.mark.django_db(transaction=True)
def test_cross_clan_global_idempotency_conflicts_safely(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, _, _, resolved, _, clan = clan_world
    first, second = clan(), clan()
    for cs in (first, second):
        activated(cs.memberships.get())
    results = parallel(
        [
            lambda: settle_clan_reward(
                membership=first.memberships.get(),
                source_type="CLAN_ACTIVITY",
                source_id="one",
                gross_amount=10,
                idempotency_key="global-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
            lambda: settle_clan_reward(
                membership=second.memberships.get(),
                source_type="CLAN_ACTIVITY",
                source_id="two",
                gross_amount=10,
                idempotency_key="global-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1
    assert all(type(x).__name__ != "IntegrityError" for x in results)


@pytest.mark.django_db(transaction=True)
def test_duplicate_shop_purchase_concurrency_debits_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.treasury_balance = 5000
    cs.save()
    results = parallel(
        [
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="purchase-race",
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    cs.refresh_from_db()
    assert cs.treasury_balance == 2600
    assert ClanInventory.objects.get(clan_season=cs, item_id="rally-beacon").quantity == 1


@pytest.mark.django_db(transaction=True)
def test_duplicate_inventory_activation_concurrency_consumes_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    ClanInventory.objects.create(clan_season=cs, item_id="rally-beacon", quantity=1)
    results = parallel(
        [
            lambda: activate_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="activation-race",
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 2
    assert ClanInventory.objects.get(clan_season=cs, item_id="rally-beacon").quantity == 0


@pytest.mark.django_db(transaction=True)
def test_duplicate_clan_window_finalizers_concurrency(clan_world) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from apps.clans.models import ClanActivity
    from apps.clans.windowing import finalize_due_windows
    from apps.core.activity import window_start_for

    now, config, _, _, _, clan = clan_world
    cs = clan()
    start = window_start_for(now - timedelta(hours=2), 900)
    type(cs).objects.filter(pk=cs.pk).update(created_at=start - timedelta(minutes=1))
    cs.refresh_from_db()
    member = cs.memberships.get()
    member.started_at = start
    member.save()
    activated(member)
    ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=member.player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=config,
        telegram_message_id=1201,
        telegram_update_id=1201,
        observed_at=start + timedelta(minutes=1),
        character_count=150,
        rewardable_chars=150,
        classification="VALID",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
        captain_bonus_eligible_at_source=True,
    )
    results = parallel([lambda: finalize_due_windows(now=now) for _ in range(2)])
    assert all(not isinstance(x, Exception) for x in results)
    assert cs.economic_settlements.count() == 1


@pytest.mark.django_db(transaction=True)
def test_different_key_treasury_purchases_cannot_overspend(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.treasury_balance = 3000
    cs.save()
    results = parallel(
        [
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="rally-beacon",
                idempotency_key="different-a",
                resolved_config=resolved,
            ),
            lambda: purchase_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="contribution-booster",
                idempotency_key="different-b",
                resolved_config=resolved,
            ),
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1
    cs.refresh_from_db()
    assert cs.treasury_balance >= 0


@pytest.mark.django_db(transaction=True)
def test_same_mission_source_event_concurrency_progresses_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from apps.clans.missions import record_mission_progress
    from apps.clans.models import ClanMissionDefinition, ClanMissionSourceEvent

    now, config, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    definition = ClanMissionDefinition.objects.create(
        key="source-race",
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
    results = parallel(
        [
            lambda: record_mission_progress(
                definition=definition,
                membership=member,
                source_id="same-domain-event",
                units=1,
                occurred_at=now,
                resolved_config=resolved,
            )
            for _ in range(2)
        ]
    )
    assert all(not isinstance(x, Exception) for x in results)
    assert ClanMissionSourceEvent.objects.filter(definition=definition).count() == 1
    assert results[0].__class__.objects.get(pk=results[0].pk).units == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("captain_cancels", [False, True])
def test_accepted_cancellation_vs_join_observation_is_consistent(
    clan_world: Any, monkeypatch: pytest.MonkeyPatch, captain_cancels: bool
) -> None:
    from apps.clans.models import ClanJoinRequest, ClanMembership, ClanRole, PlayerSeasonClanState
    from apps.clans.services import (
        cancel_request,
        captain_cancel_accepted_request,
        decide_request,
        reconcile_telegram_membership,
        request_join,
    )

    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    req.refresh_from_db()
    assert req.decided_at is not None
    cancel = (
        (lambda: captain_cancel_accepted_request(cs.captain, req.pk))
        if captain_cancels
        else (lambda: cancel_request(soldier, req.pk))
    )

    observed_at = req.decided_at
    assert observed_at is not None

    def observe() -> Any:
        return reconcile_telegram_membership(
            player=soldier,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=5001,
            observed_at=observed_at,
        )

    results = parallel([cancel, observe])
    assert not any(type(x).__name__ in {"IntegrityError", "OperationalError"} for x in results)
    assert sum(not isinstance(x, Exception) for x in results) >= 1
    req.refresh_from_db()
    membership = ClanMembership.objects.filter(player=soldier).first()
    assert (req.state == ClanJoinRequest.State.ACTIVATED) == (membership is not None)
    state = PlayerSeasonClanState.objects.get(player=soldier, season=cs.season)
    assert state.voluntary_leave_count == 0 and state.leave_cooldown_until is None


@pytest.mark.django_db(transaction=True)
def test_captain_and_soldier_activity_settlement_share_lock_order(clan_world) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.models import ClanCaptainBonusSource, ClanMembership, ClanRole

    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    captain_member = cs.memberships.get()
    activated(captain_member)
    soldier_player = player(ClanRole.SOLDIER)
    soldier_member = ClanMembership.objects.create(
        player=soldier_player,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    activated(soldier_member)
    results = parallel(
        [
            lambda: settle_clan_reward(
                membership=captain_member,
                source_type="CLAN_ACTIVITY",
                source_id="captain-own-race",
                gross_amount=100,
                idempotency_key="captain-own-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
            lambda: settle_clan_reward(
                membership=soldier_member,
                source_type="CLAN_ACTIVITY",
                source_id="soldier-bonus-race",
                gross_amount=100,
                idempotency_key="soldier-bonus-race",
                occurred_at=now,
                resolved_config=resolved,
            ),
        ]
    )
    assert all(not isinstance(result, Exception) for result in results)
    assert ClanCaptainBonusSource.objects.filter(daily__clan_season=cs).count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_soldiers_contend_on_one_captain_daily_cap(clan_world) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.models import (
        ClanCaptainBonusDaily,
        ClanCaptainBonusSource,
        ClanMembership,
        ClanRole,
    )

    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    members = []
    for _ in range(2):
        soldier = player(ClanRole.SOLDIER)
        member = ClanMembership.objects.create(
            player=soldier,
            season=season,
            clan_season=cs,
            role=ClanRole.SOLDIER,
            started_at=now,
            config_version=config,
        )
        activated(member)
        members.append(member)
    results = parallel(
        [
            partial(
                settle_clan_reward,
                membership=member,
                source_type="CLAN_ACTIVITY",
                source_id=f"daily-cap-{index}",
                gross_amount=100000,
                idempotency_key=f"daily-cap-{index}",
                occurred_at=now,
                resolved_config=resolved,
            )
            for index, member in enumerate(members)
        ]
    )
    assert all(not isinstance(result, Exception) for result in results)
    daily = ClanCaptainBonusDaily.objects.get(clan_season=cs)
    assert resolved.clan is not None
    assert daily.awarded_amount <= resolved.clan.captain_bonus.daily_cap
    assert ClanCaptainBonusSource.objects.filter(daily=daily).count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_sources_complete_group_mission_once(clan_world) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from apps.clans.missions import record_mission_progress
    from apps.clans.models import ClanMissionDefinition, ClanMissionProgress

    now, config, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    definition = ClanMissionDefinition.objects.create(
        key="group-completion-race",
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
    results = parallel(
        [
            partial(
                record_mission_progress,
                definition=definition,
                membership=member,
                source_id=f"group-race-{index}",
                units=1,
                occurred_at=now + timedelta(microseconds=index),
                resolved_config=resolved,
            )
            for index in range(2)
        ]
    )
    assert all(not isinstance(result, Exception) for result in results)
    progress = ClanMissionProgress.objects.get(definition=definition, clan_season=cs)
    assert progress.completed_at is not None
    assert cs.economic_settlements.filter(source_type="CLAN_MISSION").count() == 1
    assert (
        cs.hq_xp_entries.filter(source_type="CLAN_MISSION", source_id=str(progress.pk)).count() == 1
    )


@pytest.mark.django_db(transaction=True)
def test_clan_registration_races_creator_token_consumption(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import parse_qs, urlparse

    from apps.clans.models import ClanRole, ClanSeason
    from apps.clans.services import register_clan
    from apps.network.models import CreatorGroupRegistration
    from apps.network.services import consume_registration_token, create_registration_intent
    from apps.telegram.bot_api import TelegramResult
    from apps.telegram.models import TelegramGroup, TelegramUserMembership

    now, _, season, resolved, player, _ = clan_world
    captain = player(ClanRole.CAPTAIN)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-99123,
        chat_type="supergroup",
        title="Ownership race",
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
    _, url = create_registration_intent(captain)
    raw = parse_qs(urlparse(url).query)["startgroup"][0]
    monkeypatch.setattr("apps.clans.services.settings.TELEGRAM_BOT_ID", 999)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "administrator", "can_invite_users": True}),
    )
    results = parallel(
        [
            lambda: register_clan(
                captain,
                season,
                resolved,
                group_id=group.pk,
                name="Race Clan",
                tag="RACE",
                motto="",
            ),
            lambda: consume_registration_token(raw, captain, group, observed_at=now),
        ]
    )
    assert not any(
        type(result).__name__ in {"IntegrityError", "OperationalError"} for result in results
    )
    live_clan = ClanSeason.objects.filter(
        clan__telegram_group=group, state__in=["ACTIVE", "RECOVERY"]
    ).exists()
    live_creator = (
        CreatorGroupRegistration.objects.filter(group=group).exclude(state="INACTIVE").exists()
    )
    assert live_clan != live_creator


@pytest.mark.django_db(transaction=True)
def test_final_seat_acceptance_races_capacity_expansion(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.clans.economy import effective_capacity
    from apps.clans.models import ClanInventory, ClanMembership, ClanRole
    from apps.clans.services import decide_request, request_join

    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    for _ in range(18):
        soldier = player(ClanRole.SOLDIER)
        member = ClanMembership.objects.create(
            player=soldier,
            season=season,
            clan_season=cs,
            role=ClanRole.SOLDIER,
            started_at=now,
            config_version=config,
        )
        activated(member)
    applicant = player(ClanRole.SOLDIER)
    request = request_join(applicant, cs, resolved)
    ClanInventory.objects.create(clan_season=cs, item_id="capacity-expansion-module", quantity=1)
    results = parallel(
        [
            lambda: decide_request(cs.captain, request.pk, accept=True),
            lambda: activate_item(
                clan_season=cs,
                actor=cs.captain,
                item_id="capacity-expansion-module",
                idempotency_key="capacity-final-seat-race",
                resolved_config=resolved,
            ),
        ]
    )
    assert all(not isinstance(result, Exception) for result in results)
    cs.refresh_from_db()
    assert resolved.clan is not None
    occupied = (
        cs.memberships.filter(ended_at__isnull=True).count()
        + cs.join_requests.filter(state="ACCEPTED").count()
    )
    assert occupied <= effective_capacity(cs, resolved.clan)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("revoke_ok", [True, False])
def test_remote_revoke_races_presence_observation_without_releasing_unsafely(
    clan_world: Any, monkeypatch: pytest.MonkeyPatch, revoke_ok: bool
) -> None:
    from apps.clans.models import ClanInvite, ClanJoinRequest, ClanMembership, ClanRole
    from apps.clans.services import (
        decide_request,
        reconcile_telegram_membership,
        request_join,
        revoke_invite_remote,
    )
    from apps.telegram.bot_api import TelegramResult

    now, _, _, resolved, player, clan = clan_world
    cs = clan()
    applicant = player(ClanRole.SOLDIER)
    request = request_join(applicant, cs, resolved)
    decide_request(cs.captain, request.pk, accept=True)
    request.refresh_from_db()
    invite = request.invite
    invite.state = ClanInvite.State.REVOKE_PENDING
    invite.desired_terminal_state = ClanInvite.State.REVOKED
    invite.invite_link = "https://t.me/+race"
    invite.unknown_credential_risk = not revoke_ok
    invite.save()
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link",
        lambda *_: TelegramResult(revoke_ok, {} if revoke_ok else None, category="timeout"),
    )
    results = parallel(
        [
            lambda: revoke_invite_remote(invite.pk),
            lambda: reconcile_telegram_membership(
                player=applicant,
                group=cs.clan.telegram_group,
                present=True,
                telegram_update_id=99001,
                observed_at=now,
            ),
        ]
    )
    assert not any(
        type(result).__name__ in {"IntegrityError", "OperationalError"} for result in results
    )
    request.refresh_from_db()
    invite.refresh_from_db()
    membership_exists = ClanMembership.objects.filter(
        player=applicant, clan_season=cs, ended_at__isnull=True
    ).exists()
    assert membership_exists == (request.state == ClanJoinRequest.State.ACTIVATED)
    if not membership_exists and not revoke_ok:
        assert request.state == ClanJoinRequest.State.ACCEPTED
        assert invite.unknown_credential_risk
    state = applicant.clan_states.get(season=cs.season)
    assert state.voluntary_leave_count == 0 and state.leave_cooldown_until is None
