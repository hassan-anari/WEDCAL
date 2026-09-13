# mypy: disable-error-code="no-untyped-def"

import pytest
from django.contrib.auth.models import Permission, User
from django.urls import reverse

from apps.notifications.models import BroadcastCampaign

pytestmark = pytest.mark.django_db(transaction=True)


def staff(name: str, *permissions: str) -> User:
    user = User.objects.create_user(name, password="secret", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=permissions))
    return user


def campaign(actor: User) -> BroadcastCampaign:
    return BroadcastCampaign.objects.create(
        audience_type=BroadcastCampaign.Audience.ALL,
        body_snapshot="configured",
        created_by=actor,
        purpose="operator purpose",
    )


@pytest.mark.parametrize(
    "permissions",
    [(), ("view_broadcastcampaign", "change_broadcastcampaign", "add_broadcastcampaign")],
)
def test_broadcast_admin_direct_urls_deny_without_manage_permission(client, permissions):
    operator = staff(f"denied-{len(permissions)}", *permissions)
    row = campaign(operator)
    client.force_login(operator)
    assert (
        client.get(reverse("admin:notifications_broadcastcampaign_changelist")).status_code == 403
    )
    assert client.get(reverse("admin:notifications_broadcastcampaign_add")).status_code == 403
    assert (
        client.get(
            reverse("admin:notifications_broadcastcampaign_change", args=(row.pk,))
        ).status_code
        == 403
    )


def test_broadcast_admin_manage_permission_controls_get_and_post(client):
    operator = staff("broadcast-manager", "manage_broadcasts")
    row = campaign(operator)
    client.force_login(operator)
    changelist = reverse("admin:notifications_broadcastcampaign_changelist")
    assert client.get(changelist).status_code == 200
    assert client.get(reverse("admin:notifications_broadcastcampaign_add")).status_code == 200
    response = client.post(
        changelist,
        {
            "action": "queue_selected",
            "_selected_action": [str(row.pk)],
            "index": "0",
            "select_across": "0",
        },
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.state == BroadcastCampaign.State.MATERIALIZING
