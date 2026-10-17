# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.clans.missions import process_mission_signals, queue_mission_signal
from apps.clans.models import ClanMembershipEvent, ClanMissionDefinition, ClanMissionProgress
from apps.missions.models import MissionDefinition
from apps.notifications.models import OperatorAuditEvent, TelegramDelivery

pytestmark = pytest.mark.django_db(transaction=True)


def clan_data(config, now, **changes):
    return {
        "key": "b6-group",
        "version": 1,
        "title": "Together",
        "description": "Verified work",
        "kind": "GROUP",
        "requirement": "VALID_TYPING_RUN",
        "target_units": 1,
        "reward_band": "GROUP",
        "config_version": config.pk,
        "starts_at_0": (now - timedelta(minutes=1)).date().isoformat(),
        "starts_at_1": (now - timedelta(minutes=1)).time().isoformat(),
        "ends_at_0": (now + timedelta(hours=2)).date().isoformat(),
        "ends_at_1": (now + timedelta(hours=2)).time().isoformat(),
        **changes,
    }


def control(client, model, row, *, reason="B6 isolated review"):
    path = f"/admin/{model}/{row.pk}/control/"
    preview = client.get(path)
    assert preview.status_code == 200
    token = preview.context["form"].initial["confirmation"]
    return client.post(path, {"confirmation": token, "reason": reason, "confirm": "on"})


def test_admin_creates_activates_both_clan_kinds_and_automatic_settlement(client, clan_world):
    now, config, _, _, _, clan = clan_world
    actor = User.objects.create_superuser("b6-admin", password="synthetic")
    client.force_login(actor)
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    for kind, band in [("GROUP", "GROUP"), ("INDIVIDUAL", "SMALL")]:
        key = f"b6-{kind.lower()}"
        response = client.post(
            "/admin/clans/clanmissiondefinition/add/",
            clan_data(config, now, key=key, kind=kind, reward_band=band, enabled="on"),
        )
        assert response.status_code == 302, response.context and response.context.get("adminform")
        row = ClanMissionDefinition.objects.get(key=key)
        assert not row.enabled
        preview = client.get(f"/admin/clans/clanmissiondefinition/{row.pk}/control/")
        actual = preview.context["preview"]
        expected = next(
            b for b in config.payload["clan"]["missions"]["reward_bands"].items() if b[0] == band
        )[1]
        assert actual["participant_gross_points"] == expected["participant_gross_points"]
        assert control(client, "clans/clanmissiondefinition", row).status_code == 302
        row.refresh_from_db()
        assert row.enabled and row.activated_at
    assert not TelegramDelivery.objects.exists()
    queue_mission_signal(
        membership=member,
        source_key="b6-admin-valid-run",
        requirement="VALID_TYPING_RUN",
        units=1,
        occurred_at=timezone.now(),
    )
    assert process_mission_signals() == 1
    assert process_mission_signals() == 0
    assert ClanMissionProgress.objects.filter(completed_at__isnull=False).count() == 2
    assert all(p.reward_settlement for p in ClanMissionProgress.objects.all())
    assert OperatorAuditEvent.objects.filter(action="MISSION_ACTIVATE").count() == 2
    row = ClanMissionDefinition.objects.order_by("pk")[0]
    assert control(client, "clans/clanmissiondefinition", row).status_code == 302
    row.refresh_from_db()
    assert not row.enabled
    assert ClanMissionProgress.objects.count() == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"requirement": "ARBITRARY"},
        {"target_units": 0},
        {"reward_band": "MINT_ANYTHING"},
        {"title": ""},
        {"ends_at_0": "2000-01-01"},
    ],
)
def test_admin_invalid_clan_drafts_do_not_persist(client, clan_world, changes):
    now, config, *_ = clan_world
    client.force_login(User.objects.create_superuser("bad-draft"))
    result = client.post(
        "/admin/clans/clanmissiondefinition/add/", clan_data(config, now, **changes)
    )
    assert result.status_code == 200
    assert not ClanMissionDefinition.objects.exists()


def test_activation_requires_extra_permission_signed_fresh_preview_and_reason(client, clan_world):
    now, config, *_ = clan_world
    author = User.objects.create_user("draft-author", is_staff=True)
    author.user_permissions.add(
        *Permission.objects.filter(
            codename__in=[
                "view_clanmissiondefinition",
                "change_clanmissiondefinition",
                "add_clanmissiondefinition",
            ]
        )
    )
    client.force_login(author)
    assert (
        client.post("/admin/clans/clanmissiondefinition/add/", clan_data(config, now)).status_code
        == 302
    )
    row = ClanMissionDefinition.objects.get()
    assert control(client, "clans/clanmissiondefinition", row).status_code == 403
    owner = User.objects.create_superuser("activation-owner")
    client.force_login(owner)
    path = f"/admin/clans/clanmissiondefinition/{row.pk}/control/"
    token = client.get(path).context["form"].initial["confirmation"]
    ClanMissionDefinition.objects.filter(pk=row.pk).update(target_units=2)
    result = client.post(path, {"confirmation": token, "reason": "stale", "confirm": "on"})
    assert result.status_code == 200
    assert not ClanMissionDefinition.objects.get().enabled
    for data in [
        {"confirmation": "bad", "reason": "reason", "confirm": "on"},
        {"confirmation": token, "reason": "", "confirm": "on"},
    ]:
        assert client.post(path, data).status_code == 200
    assert not OperatorAuditEvent.objects.filter(action="MISSION_ACTIVATE").exists()


def test_personal_mission_admin_preserves_policy_and_history(client, phase4_config_factory):
    phase4_config_factory()
    client.force_login(User.objects.create_superuser("personal-operator"))
    data = {
        "key": "visit-dalexio",
        "version": 1,
        "verification_family": "TIMED_EXTERNAL",
        "repeat_policy": "ONCE_PER_DEFINITION_VERSION",
        "title": "Visit",
        "verification_parameters": '{"destination_url":"https://example.com/"}',
        "enabled": "on",
    }
    response = client.post("/admin/missions/missiondefinition/add/", data)
    assert response.status_code == 302
    row = MissionDefinition.objects.get()
    assert not row.enabled
    assert control(client, "missions/missiondefinition", row).status_code == 302
    assert control(client, "missions/missiondefinition", row).status_code == 302
    row.refresh_from_db()
    assert not row.enabled and row.activated_at
    with pytest.raises(DatabaseError), transaction.atomic():
        MissionDefinition.objects.filter(pk=row.pk).update(
            verification_parameters={"destination_url": "https://changed.example/"}
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        MissionDefinition.objects.filter(pk=row.pk).update(version=2)


def test_clan_definition_history_rejects_direct_destructive_update(client, clan_world):
    now, config, *_ = clan_world
    client.force_login(User.objects.create_superuser("clan-operator"))
    assert (
        client.post("/admin/clans/clanmissiondefinition/add/", clan_data(config, now)).status_code
        == 302
    )
    row = ClanMissionDefinition.objects.get()
    assert control(client, "clans/clanmissiondefinition", row).status_code == 302
    assert control(client, "clans/clanmissiondefinition", row).status_code == 302
    changes_to_try: list[dict[str, object]] = [
        {"target_units": 9},
        {"version": 9},
        {"activated_at": None},
        {"reward_band": "LARGE"},
    ]
    for changes in changes_to_try:
        with pytest.raises(DatabaseError), transaction.atomic():
            ClanMissionDefinition.objects.filter(pk=row.pk).update(**changes)
    assert client.get(f"/admin/clans/clanmissiondefinition/{row.pk}/delete/").status_code == 403


def test_stale_admin_edit_cannot_reactivate_a_deactivated_definition(client, clan_world):
    from types import SimpleNamespace

    from django.contrib import admin
    from django.test import RequestFactory

    now, config, *_ = clan_world
    actor = User.objects.create_superuser("stale-admin-editor")
    client.force_login(actor)
    assert (
        client.post("/admin/clans/clanmissiondefinition/add/", clan_data(config, now)).status_code
        == 302
    )
    row = ClanMissionDefinition.objects.get()
    assert control(client, "clans/clanmissiondefinition", row).status_code == 302
    stale = ClanMissionDefinition.objects.get(pk=row.pk)
    assert stale.enabled
    assert control(client, "clans/clanmissiondefinition", row).status_code == 302
    stale.title = "Safe display correction"
    request = RequestFactory().post("/admin/clans/clanmissiondefinition/")
    request.user = actor
    admin.site._registry[ClanMissionDefinition].save_model(
        request, stale, SimpleNamespace(changed_data=["title"]), True
    )
    current = ClanMissionDefinition.objects.get(pk=row.pk)
    assert not current.enabled and current.title == "Safe display correction"
    assert current.activated_at == stale.activated_at
