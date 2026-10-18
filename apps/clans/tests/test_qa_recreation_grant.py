# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from io import StringIO

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction

from apps.clans.governance import request_dissolution
from apps.clans.models import ClanRecreationGrant, ClanSeason
from apps.clans.qa_recreation import grant_qa_recreation, recreation_grant_available
from apps.clans.services import ClanConflict, eligible_groups, register_clan
from apps.clans.tests.test_b5_integration import b5_http as b5_http
from apps.clans.tests.test_phase7a_concurrency import parallel
from apps.notifications.models import OperatorAuditEvent
from apps.seasons.models import Season
from apps.telegram.models import TelegramGroup, TelegramGroupCandidate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def qa_world(clan_world, monkeypatch):
    now, _, season, resolved, _, make_clan = clan_world
    cs = make_clan()
    monkeypatch.setattr("apps.arena.services.dispatch_projection_safely", lambda *_: None)
    request_dissolution(cs.captain, cs)
    operator = User.objects.create_user("qa-grant-operator")
    operator.user_permissions.add(Permission.objects.get(codename="grant_qa_clan_recreation"))
    monkeypatch.setattr("apps.clans.services._fresh_authority", lambda *_: (now, 0))

    def group():
        seq = TelegramGroup.objects.count() + 1
        row = TelegramGroup.objects.create(
            telegram_chat_id=-900000 - seq,
            chat_type="supergroup",
            title=f"Synthetic QA group {seq}",
            bot_is_admin=True,
            bot_can_invite_users=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        TelegramGroupCandidate.objects.create(
            group=row,
            player=cs.captain,
            telegram_user_id=cs.captain.telegram_user_id,
            observed_at=now,
        )
        return row

    def grant(actor=operator, selected=season, reason="Owner-approved synthetic QA test"):
        return grant_qa_recreation(
            actor=actor,
            player=cs.captain,
            season=selected,
            reason=reason,
            ticket_reference="synthetic-owner-approval",
        )

    def create(row):
        return register_clan(
            cs.captain,
            season,
            resolved,
            group_id=row.pk,
            name=f"New QA Clan {row.pk}",
            tag=f"Q{row.pk}",
            motto="",
        )

    return cs, operator, group, grant, create


def test_default_policy_and_old_group_binding_remain(qa_world, clan_world):
    cs, _, group, grant, create = qa_world
    fresh = group()
    with pytest.raises(ClanConflict, match="recreation"):
        create(fresh)
    permission, _ = grant()
    assert recreation_grant_available(cs.captain, cs.season)
    # A grant never bypasses the historical Clan -> TelegramGroup one-to-one ownership.
    assert (
        not eligible_groups(cs.captain, cs.season, clan_world[3])
        .filter(pk=cs.clan.telegram_group_id)
        .exists()
    )
    with pytest.raises(PermissionDenied):
        create(cs.clan.telegram_group)
    permission.refresh_from_db()
    assert permission.consumed_at is None
    assert cs.clan.telegram_group.clan.pk == cs.clan_id


def test_grant_single_use_registration_replay_audit_and_history(qa_world, b5_http):
    cs, operator, group, grant, create = qa_world
    old_members = list(cs.memberships.values())
    old_events = list(cs.events.values())
    permission, created = grant()
    assert created and grant()[1] is False
    assert (
        b5_http(cs.captain)
        .get("/api/v1/clans/state")
        .json()["qa_recreation_authorization_available"]
    )
    fresh = group()
    new = create(fresh)
    assert create(fresh).pk == new.pk
    permission.refresh_from_db()
    assert permission.consumed_clan_season_id == new.pk and permission.consumed_at is not None
    assert not recreation_grant_available(cs.captain, cs.season)
    assert list(cs.memberships.values()) == old_members
    assert list(cs.events.values()) == old_events
    cs.refresh_from_db()
    assert cs.state == "ENDED" and new.clan_id != cs.clan_id
    assert (
        OperatorAuditEvent.objects.filter(
            target_type="ClanRecreationGrant", target_id=str(permission.pk)
        ).count()
        == 2
    )
    assert grant()[0].pk == permission.pk  # Operator command replay cannot replenish it.
    with pytest.raises(ValidationError, match="reissued"):
        grant(reason="Attempt second permission")
    # API captain selection remains unambiguous with an old ended + new ClanSeason.
    assert (
        b5_http(cs.captain)
        .get("/api/v1/clans/recovery")
        .json()["governance"]["dissolution"]["outcome"]
        == "IMMEDIATE"
    )
    assert b5_http(cs.captain).post("/api/v1/clans/dissolution").status_code == 204
    with pytest.raises(ClanConflict, match="recreation"):
        create(group())
    assert ClanRecreationGrant.objects.count() == 1


def test_unauthorized_inactive_operator_and_player_season_scope(qa_world, clan_world):
    cs, operator, _, grant, _ = qa_world
    unprivileged = User.objects.create_user("unauthorized")
    with pytest.raises(PermissionDenied):
        grant(actor=unprivileged)
    operator.is_active = False
    operator.save(update_fields=("is_active",))
    with pytest.raises(PermissionDenied):
        grant()
    operator.is_active = True
    operator.save(update_fields=("is_active",))
    other_season = Season.objects.create(
        code="synthetic-other",
        title="Synthetic future",
        starts_at=cs.season.starts_at,
        ends_at=cs.season.ends_at,
        baseline_config=clan_world[1],
    )
    with pytest.raises(ValidationError, match="current active"):
        grant(selected=other_season)
    permission, _ = grant()
    assert not recreation_grant_available(cs.captain, other_season)
    other_player = clan_world[-2]()
    assert not recreation_grant_available(other_player, cs.season)
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanRecreationGrant.objects.filter(pk=permission.pk).update(player=other_player)


def test_failed_registration_keeps_grant_and_concurrent_creation_consumes_once(
    qa_world, monkeypatch
):
    cs, _, group, grant, create = qa_world
    permission, _ = grant()
    first, second = group(), group()
    # A failed normal registration transaction must roll back the consumption audit too.
    original = OperatorAuditEvent.objects.create

    def fail_consumption(**kwargs):
        if kwargs.get("action") == "QA_CLAN_RECREATION_CONSUMED":
            raise RuntimeError("Synthetic registration rollback")
        return original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(OperatorAuditEvent.objects, "create", fail_consumption)
        with pytest.raises(RuntimeError):
            create(first)
    permission.refresh_from_db()
    assert permission.consumed_at is None
    assert not ClanSeason.objects.filter(captain=cs.captain).exclude(pk=cs.pk).exists()
    results = parallel([lambda: create(first), lambda: create(second)])
    assert sum(isinstance(x, ClanSeason) for x in results) == 1
    permission.refresh_from_db()
    assert permission.consumed_at is not None
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanRecreationGrant.objects.filter(pk=permission.pk).update(
            consumed_at=None, consumed_clan_season=None
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanRecreationGrant.objects.filter(pk=permission.pk).delete()


def test_exact_operator_command_stdin_and_replay(qa_world, monkeypatch):
    cs, operator, _, _, _ = qa_world
    options = dict(
        actor=operator.username,
        season=cs.season.code,
        reason="Owner-approved command test",
        ticket_reference="synthetic-approval",
        telegram_user_id_stdin=True,
    )
    for expected in ["created=True", "created=False"]:
        monkeypatch.setattr("sys.stdin", StringIO(str(cs.captain.telegram_user_id) + "\n"))
        out = StringIO()
        call_command("grant_qa_clan_recreation", **options, stdout=out)
        assert expected in out.getvalue()
        assert str(cs.captain.telegram_user_id) not in out.getvalue()
    monkeypatch.setattr("sys.stdin", StringIO("invalid"))
    with pytest.raises(CommandError):
        call_command("grant_qa_clan_recreation", **options)


def test_grant_does_not_bypass_fresh_telegram_authority(qa_world, monkeypatch):
    _, _, group, grant, create = qa_world
    permission, _ = grant()
    fresh = group()

    def denied(*_):
        raise PermissionDenied("Fresh Telegram administrator authority required")

    monkeypatch.setattr("apps.clans.services._fresh_authority", denied)
    with pytest.raises(PermissionDenied):
        create(fresh)
    permission.refresh_from_db()
    assert permission.consumed_at is None
