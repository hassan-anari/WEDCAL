# mypy: disable-error-code="no-untyped-def"
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.moderation.models import ModerationAction, ModerationEvidence, ModerationEvidenceAccess
from apps.moderation.services import (
    ModerationIdempotencyConflict,
    apply_action,
    create_report,
    expire_suspensions,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db(transaction=True)


def staff_with(permission: str) -> User:
    user = User.objects.create_user(permission, password="secret", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename=permission))
    return user


def test_suspend_requires_expiry_and_expiry_is_idempotency_semantics(clan_world):
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    with pytest.raises(ValidationError):
        apply_action(
            case_id=case.pk,
            action_type="SUSPEND",
            reason="review",
            idempotency_key="suspend",
            system_actor="test",
        )
    expiry = timezone.now() + timedelta(days=1)
    apply_action(
        case_id=case.pk,
        action_type="SUSPEND",
        reason="review",
        idempotency_key="suspend",
        system_actor="test",
        suspension_expires_at=expiry,
    )
    with pytest.raises(ModerationIdempotencyConflict):
        apply_action(
            case_id=case.pk,
            action_type="SUSPEND",
            reason="review",
            idempotency_key="suspend",
            system_actor="test",
            suspension_expires_at=expiry + timedelta(hours=1),
        )


def test_old_suspension_expiry_does_not_overwrite_ban(clan_world):
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    expiry = timezone.now() + timedelta(minutes=1)
    apply_action(
        case_id=case.pk,
        action_type="SUSPEND",
        reason="review",
        idempotency_key="suspend-race",
        system_actor="test",
        suspension_expires_at=expiry,
    )
    apply_action(
        case_id=case.pk,
        action_type="BAN",
        reason="later",
        idempotency_key="ban-race",
        system_actor="test",
    )
    assert expire_suspensions(now=expiry + timedelta(seconds=1)) == 0
    target.refresh_from_db()
    assert target.moderation_state == Player.ModerationState.BANNED


def test_admin_evidence_view_requires_permission_and_audits(client, clan_world):
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    evidence = ModerationEvidence.objects.create(
        case=case, kind=ModerationEvidence.Kind.MANUAL_NOTE, manual_note="sensitive"
    )
    denied = User.objects.create_user("denied", password="secret", is_staff=True)
    client.force_login(denied)
    url = reverse("admin:moderation_evidence_secure_view", args=(evidence.pk,))
    assert client.get(url).status_code == 403
    allowed = staff_with("view_sensitive_evidence")
    client.force_login(allowed)
    response = client.post(url, {"reason": "case review"})
    assert response.status_code == 200 and b"sensitive" in response.content
    assert ModerationEvidenceAccess.objects.filter(evidence=evidence, actor=allowed).count() == 1


def test_admin_moderation_action_records_staff_actor(client, clan_world):
    reporter, target = clan_world[-2](), clan_world[-2]()
    case = create_report(reporter=reporter, target=target, category="risk", reason="reason")
    staff = staff_with("apply_moderation_actions")
    client.force_login(staff)
    url = reverse("admin:moderation_case_apply_action", args=(case.pk,))
    response = client.post(
        url,
        {
            "action_type": "WARN",
            "reason": "operator reason",
            "idempotency_key": "admin-warn",
            "correlation_id": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert response.status_code == 302
    action = ModerationAction.objects.get(idempotency_key="admin-warn")
    assert action.staff_actor == staff and action.actor is None
