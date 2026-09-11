from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import Any

import pytest
from django.db import close_old_connections

from apps.clans.models import ClanInvite, ClanJoinRequest, ClanMembership, ClanRole
from apps.clans.services import (
    choose_role,
    create_personal_invite,
    decide_request,
    request_join,
)
from apps.telegram.bot_api import TelegramResult


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
        return [f.result(timeout=15) for f in [pool.submit(run, c) for c in calls]]


@pytest.mark.django_db(transaction=True)
def test_conflicting_role_choice_concurrency_has_one_result(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, season, _, player, _ = clan_world
    target = player()
    results = parallel(
        [
            lambda: choose_role(target, season, ClanRole.CAPTAIN),
            lambda: choose_role(target, season, ClanRole.SOLDIER),
        ]
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1


@pytest.mark.django_db(transaction=True)
def test_two_clans_accepting_one_soldier_has_one_winner(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    soldier = player(ClanRole.SOLDIER)
    first, second = clan(), clan()
    one = request_join(soldier, first, resolved)
    two = request_join(soldier, second, resolved)
    results = parallel(
        [
            lambda: decide_request(first.captain, one.pk, accept=True),
            lambda: decide_request(second.captain, two.pk, accept=True),
        ]
    )
    assert (
        ClanJoinRequest.objects.filter(
            applicant=soldier, state=ClanJoinRequest.State.ACCEPTED
        ).count()
        == 1
    )
    assert all(isinstance(result, ClanJoinRequest) for result in results)
    assert {result.state for result in results} == {
        ClanJoinRequest.State.ACCEPTED,
        ClanJoinRequest.State.CANCELLED,
    }
    assert not ClanMembership.objects.filter(player=soldier).exists()
    assert (
        ClanInvite.objects.filter(
            join_request__applicant=soldier,
            join_request__state=ClanJoinRequest.State.ACCEPTED,
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_last_seat_capacity_race_has_one_winner(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    cs = clan(capacity=2)
    one, two = player(ClanRole.SOLDIER), player(ClanRole.SOLDIER)
    r1 = request_join(one, cs, resolved)
    r2 = request_join(two, cs, resolved)
    parallel(
        [
            lambda: decide_request(cs.captain, r1.pk, accept=True),
            lambda: decide_request(cs.captain, r2.pk, accept=True),
        ]
    )
    assert (
        ClanJoinRequest.objects.filter(clan_season=cs, state=ClanJoinRequest.State.ACCEPTED).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_invite_creation_has_single_external_caller(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    calls = []
    lock = Lock()

    def telegram(*_args):  # type: ignore[no-untyped-def]
        with lock:
            calls.append(1)
        return TelegramResult(True, {"invite_link": "secret"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", telegram)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    parallel(
        [
            lambda: create_personal_invite(ClanInvite.objects.get(pk=req.invite.pk)),
            lambda: create_personal_invite(ClanInvite.objects.get(pk=req.invite.pk)),
        ]
    )
    assert len(calls) == 1
    assert ClanInvite.objects.get(pk=req.invite.pk).state == ClanInvite.State.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_concurrent_invite_recovery_claims_one_external_attempt(
    clan_world: Any, monkeypatch: Any, settings: Any
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.clans.services import recover_invite_creations

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(pk=req.invite.pk).update(
        state=ClanInvite.State.FAILED,
        next_create_retry_at=timezone.now() - timedelta(seconds=1),
    )
    calls = []
    lock = Lock()

    def telegram(*_args: object) -> TelegramResult:
        with lock:
            calls.append(1)
        return TelegramResult(True, {"invite_link": "recovered"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", telegram)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.send_message", lambda *_: TelegramResult(True, {})
    )
    parallel(
        [
            lambda: recover_invite_creations(limit=1),
            lambda: recover_invite_creations(limit=1),
        ]
    )
    invite = ClanInvite.objects.get(pk=req.invite.pk)
    assert calls == [1]
    assert invite.state == ClanInvite.State.ACTIVE
    assert invite.attempt_count == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_remote_revoke_claims_one_external_attempt(
    clan_world: Any, monkeypatch: Any
) -> None:
    from django.utils import timezone

    from apps.clans.services import queue_invite_revoke, revoke_invite_remote

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(pk=req.invite.pk).update(
        state=ClanInvite.State.ACTIVE, invite_link="known"
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)
    queue_invite_revoke(req.invite.pk, terminal=ClanInvite.State.REVOKED)
    ClanInvite.objects.filter(pk=req.invite.pk).update(next_revoke_retry_at=timezone.now())
    calls: list[int] = []
    lock = Lock()

    def revoke(*_args: object) -> TelegramResult:
        with lock:
            calls.append(1)
        return TelegramResult(True, {})

    monkeypatch.setattr("apps.clans.services.bot_api.revoke_chat_invite_link", revoke)
    parallel(
        [
            lambda: revoke_invite_remote(req.invite.pk),
            lambda: revoke_invite_remote(req.invite.pk),
        ]
    )
    invite = ClanInvite.objects.get(pk=req.invite.pk)
    assert calls == [1]
    assert invite.state == ClanInvite.State.REVOKED
    assert invite.invite_link == ""
