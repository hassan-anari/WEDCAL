import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.referrals.models import ReferralCode, ReferralRelation, ReferralShareOutcome
from apps.rewards.models import RewardLedger
from apps.rewards.services import settle_player_reward
from apps.seasons.services import resolve_current_season


class ReferralCodeUnavailable(RuntimeError):
    pass


def get_or_create_code(player: Player, *, attempts: int = 8) -> ReferralCode:
    found = ReferralCode.objects.filter(player=player).first()
    if found:
        return found
    for _ in range(attempts):
        try:
            with transaction.atomic():
                return ReferralCode.objects.create(player=player, code=secrets.token_urlsafe(12))
        except IntegrityError:
            found = ReferralCode.objects.filter(player=player).first()
            if found:
                return found
    raise ReferralCodeUnavailable("Could not allocate a referral code")


def referral_link(code: ReferralCode) -> str:
    username = settings.TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    if not username:
        raise ReferralCodeUnavailable("Telegram bot username is unavailable")
    return f"https://t.me/{username}?start={code.code}"


def attribute_start(
    invitee: Player, parameter: str | None, *, at: datetime | None = None
) -> ReferralRelation | None:
    if not parameter or ReferralRelation.objects.filter(invitee=invitee).exists():
        return None
    code = ReferralCode.objects.select_related("player").filter(code=parameter).first()
    if not code or code.player_id == invitee.pk:
        return None
    # With one inviter edge per invitee, following ancestors is sufficient to reject cycles.
    ancestor = code.player
    seen = {invitee.pk}
    while ancestor.pk not in seen:
        seen.add(ancestor.pk)
        relation = (
            ReferralRelation.objects.select_related("inviter").filter(invitee=ancestor).first()
        )
        if not relation:
            break
        ancestor = relation.inviter
    else:
        return None
    try:
        with transaction.atomic():
            return ReferralRelation.objects.create(
                inviter=code.player, invitee=invitee, attributed_at=at or timezone.now()
            )
    except IntegrityError:
        return ReferralRelation.objects.filter(invitee=invitee).first()


def _tier(
    config: ResolvedGameConfig, inviter: Player, occurred_at: datetime
) -> tuple[str, int] | None:
    count = ReferralRelation.objects.filter(
        inviter=inviter, status=ReferralRelation.Status.VERIFIED, verified_at__lt=occurred_at
    ).count()
    eligible = [tier for tier in config.referral.tiers if tier.min_verified_referrals <= count]
    return (eligible[-1].key, eligible[-1].share_bps) if eligible else None


def activate_relation(relation_id: int, *, at: datetime | None = None) -> bool:
    now = at or timezone.now()
    if resolve_current_season(at=now, required=False) is None:
        return False
    config = resolve_effective_game_config(now)
    with transaction.atomic():
        relation = (
            ReferralRelation.objects.select_for_update()
            .select_related("invitee", "inviter")
            .get(pk=relation_id)
        )
        if relation.status == ReferralRelation.Status.VERIFIED:
            return False
        progression = PlayerProgression.objects.filter(player=relation.invitee).first()
        if not progression or progression.level < config.referral.min_level:
            return False
        if relation.invitee.first_seen_at > now - timedelta(
            hours=config.referral.min_account_age_hours
        ):
            return False
        if config.referral.anti_sybil_pass_required and not relation.invitee.anti_sybil_passed:
            return False
        if relation.invitee.moderation_state in config.referral.blocked_moderation_states:
            return False
        settle_player_reward(
            player=relation.inviter,
            source_type="REFERRAL",
            source_id=f"activation:{relation.pk}:inviter",
            context="REFERRAL",
            amount=config.referral.inviter_bonus,
            idempotency_key=f"referral:activation:{relation.pk}:inviter",
            occurred_at=now,
            resolved_config=config,
        )
        settle_player_reward(
            player=relation.invitee,
            source_type="REFERRAL",
            source_id=f"activation:{relation.pk}:invitee",
            context="REFERRAL",
            amount=config.referral.invitee_bonus,
            idempotency_key=f"referral:activation:{relation.pk}:invitee",
            occurred_at=now,
            resolved_config=config,
        )
        relation.status = ReferralRelation.Status.VERIFIED
        relation.verified_at = now
        relation.verified_config_id = config.id
        relation.save(update_fields=("status", "verified_at", "verified_config", "updated_at"))
    return True


def process_source_reward(source: RewardLedger) -> ReferralShareOutcome:
    existing = ReferralShareOutcome.objects.filter(source_reward=source).first()
    if existing:
        return existing
    relation = (
        ReferralRelation.objects.select_related("inviter")
        .filter(
            invitee=source.player,
            status=ReferralRelation.Status.VERIFIED,
            verified_at__lt=source.occurred_at,
        )
        .first()
    )
    config = resolve_effective_game_config(source.occurred_at)
    tier = (
        _tier(config, relation.inviter, source.occurred_at)
        if relation and source.source_type in config.referral.eligible_reward_sources
        else None
    )
    amount = source.player_amount * tier[1] // 10000 if tier else 0
    with transaction.atomic():
        outcome, created = ReferralShareOutcome.objects.get_or_create(
            source_reward=source,
            defaults={
                "relation": relation,
                "tier_key": tier[0] if tier else "",
                "share_bps": tier[1] if tier else 0,
                "amount": amount,
            },
        )
        if not created or not relation or amount <= 0:
            return outcome
        assert tier is not None
        reward, _ = settle_player_reward(
            player=relation.inviter,
            source_type="REFERRAL",
            source_id=f"share:{source.pk}",
            context=source.context,
            amount=amount,
            idempotency_key=f"referral:share:{source.pk}",
            occurred_at=source.occurred_at,
            resolved_config=config,
            metadata={"source_reward_id": source.pk, "tier": tier[0]},
        )
        outcome.share_reward = reward
        outcome.save(update_fields=("share_reward",))
    return outcome
