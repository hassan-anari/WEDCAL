from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.game_config.services import resolve_effective_game_config
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
    verified_count = relations.filter(status=ReferralRelation.Status.VERIFIED).count()
    policy = resolve_effective_game_config(timezone.now()).referral
    achieved = [tier for tier in policy.tiers if tier.min_verified_referrals <= verified_count]
    current = achieved[-1] if policy.enabled and achieved else None
    upcoming = next(
        (tier for tier in policy.tiers if tier.min_verified_referrals > verified_count), None
    )
    return {
        "code": code.code,
        "link": referral_link(code),
        "pending_count": relations.filter(status=ReferralRelation.Status.PENDING).count(),
        "verified_count": verified_count,
        "current_tier": current.key if current else None,
        "current_share_bps": current.share_bps if current else 0,
        "next_tier": (
            {
                "key": upcoming.key,
                "min_verified_referrals": upcoming.min_verified_referrals,
                "share_bps": upcoming.share_bps,
            }
            if policy.enabled and upcoming
            else None
        ),
    }
