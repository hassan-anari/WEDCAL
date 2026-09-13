# mypy: disable-error-code="no-untyped-def"
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import Permission, User
from django.db import close_old_connections

from apps.clans.models import ClanInvite, ClanJoinRequest
from apps.clans.services import ClanConflict, queue_invite_revoke, resolve_invite_credential_risk
from apps.notifications.models import OperatorAuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


def operator(name: str) -> User:
    user = User.objects.create_user(name, is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="resolve_invite_risk"))
    return user


def risky_invite(clan_world) -> ClanInvite:
    applicant = clan_world[-2]()
    clan_season = clan_world[-1]()
    request = ClanJoinRequest.objects.create(
        clan_season=clan_season,
        applicant=applicant,
        state=ClanJoinRequest.State.ACCEPTED,
    )
    return ClanInvite.objects.create(
        join_request=request,
        state=ClanInvite.State.MANUAL_REVIEW,
        unknown_credential_risk=True,
    )


def test_two_operators_serialize_conflicting_invite_risk_outcomes(clan_world):
    invite = risky_invite(clan_world)
    first, second = operator("risk-first"), operator("risk-second")

    def resolve(user_id: int, outcome: str):
        close_old_connections()
        try:
            resolve_invite_credential_risk(
                invite_id=invite.pk,
                outcome=outcome,
                actor=User.objects.get(pk=user_id),
                reason=f"review {outcome}",
            )
            result = "ok"
        except ClanConflict:
            result = "conflict"
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = (
            pool.submit(resolve, first.pk, "CONFIRMED_REVOKED"),
            pool.submit(resolve, second.pk, "STILL_UNSAFE"),
        )
        outcomes = [future.result() for future in results]
    invite.refresh_from_db()
    assert invite.state in (ClanInvite.State.MANUAL_REVIEW, ClanInvite.State.REVOKED)
    assert invite.unknown_credential_risk is (invite.state == ClanInvite.State.MANUAL_REVIEW)
    assert outcomes.count("ok") >= 1
    assert OperatorAuditEvent.objects.filter(
        target_type="ClanInvite", target_id=str(invite.pk)
    ).exists()


def test_operator_resolution_vs_revoke_transition_never_resurrects_credential(clan_world):
    invite = risky_invite(clan_world)
    reviewer = operator("risk-revoke")

    def resolve():
        close_old_connections()
        resolve_invite_credential_risk(
            invite_id=invite.pk,
            outcome="CONFIRMED_REVOKED",
            actor=User.objects.get(pk=reviewer.pk),
            reason="Telegram confirms revocation",
        )
        close_old_connections()

    def revoke():
        close_old_connections()
        queue_invite_revoke(invite.pk, terminal=ClanInvite.State.REVOKED)
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(resolve), pool.submit(revoke))
        for future in futures:
            future.result()
    invite.refresh_from_db()
    assert invite.state == ClanInvite.State.REVOKED
    assert not invite.unknown_credential_risk
    assert invite.invite_link == ""
