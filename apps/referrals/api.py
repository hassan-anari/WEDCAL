from dataclasses import asdict
from typing import Annotated, Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router

from apps.core.b3 import b3_responses
from apps.game_config.services import resolve_effective_game_config
from apps.players.avatars import avatar_url, configured
from apps.players.models import Player
from apps.referrals.models import ReferralRelation
from apps.referrals.read_models import earnings, eligibility
from apps.referrals.schemas import InviteePage, ReferralOutput
from apps.referrals.services import ReferralCodeUnavailable, get_or_create_code, referral_link
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["referrals"], auth=mini_app_auth)


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    if not isinstance(player, Player):
        raise RuntimeError("Mini App authentication did not resolve a player")
    return player


@router.get("", response=b3_responses(ReferralOutput))
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
    attributed = (
        ReferralRelation.objects.select_related("inviter", "invitee").filter(invitee=player).first()
    )
    try:
        link = referral_link(code)
    except ReferralCodeUnavailable:
        link = None
    return {
        "enabled": policy.enabled,
        "earned_points": earnings(player),
        "tiers": [asdict(tier) for tier in policy.tiers],
        "attribution_status": attributed.status if attributed else None,
        "activation": eligibility(attributed, timezone.now()) if attributed else None,
        "inviter_bonus": policy.inviter_bonus,
        "invitee_bonus": policy.invitee_bonus,
        "code": code.code,
        "link": link,
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


@router.get("/invitees", response=b3_responses(InviteePage))
def invitees(
    request: HttpRequest,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    rows = list(
        ReferralRelation.objects.filter(inviter=_player(request))
        .select_related("inviter", "invitee")
        .order_by("id")[offset : offset + limit + 1]
    )
    return {
        "items": [
            {
                "name": row.invitee.first_name or "Explorer",
                "photo_url": avatar_url(row.invitee) if configured() else "",
                "status": row.status,
                "attributed_at": row.attributed_at,
                "verified_at": row.verified_at,
                "activation": eligibility(row, timezone.now()),
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }
