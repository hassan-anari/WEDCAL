from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.players.models import Player
from apps.referrals.models import ReferralRelation
from apps.referrals.services import get_or_create_code, referral_link
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["referrals"], auth=mini_app_auth)


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    if not isinstance(player, Player):
        raise RuntimeError("Mini App authentication did not resolve a player")
    return player


@router.get("", response=dict[str, Any])
def referrals(request: HttpRequest) -> dict[str, Any]:
    player = _player(request)
    code = get_or_create_code(player)
    relations = ReferralRelation.objects.filter(inviter=player)
    return {
        "code": code.code,
        "link": referral_link(code),
        "pending_count": relations.filter(status=ReferralRelation.Status.PENDING).count(),
        "verified_count": relations.filter(status=ReferralRelation.Status.VERIFIED).count(),
    }
