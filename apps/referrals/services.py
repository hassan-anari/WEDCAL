import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.game_config.services import (
    ResolvedGameConfig,
    resolve_effective_game_config,
    resolve_game_config_version,
)
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.referrals.models import ReferralCode, ReferralRelation, ReferralShareOutcome
from apps.rewards.models import RewardLedger
from apps.rewards.services import settle_player_reward
from apps.seasons.services import is_reward_earning_time, resolve_current_season


class ReferralCodeUnavailable(RuntimeError):
    pass


class DeferredReferralSettlement(RuntimeError):
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
    if not parameter:
        return None
    try:
        config = resolve_effective_game_config(at or timezone.now())
    except LookupError:
        return None
    if not config.referral.enabled:
        return None
    code = ReferralCode.objects.select_related("player").filter(code=parameter).first()
    if not code or code.player_id == invitee.pk:
        return None
    try:
        with transaction.atomic():
            # Attribution mutates one logical graph. A transaction advisory lock makes cycle
            # evaluation serializable, including two-node and longer concurrent cycles.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [0x524546455252414C])
            if ReferralRelation.objects.filter(invitee=invitee).exists():
                return ReferralRelation.objects.get(invitee=invitee)
            ancestor = code.player
            seen = {invitee.pk}
            while ancestor.pk not in seen:
                seen.add(ancestor.pk)
                relation = (
                    ReferralRelation.objects.select_related("inviter")
                    .filter(invitee=ancestor)
                    .first()
                )
                if not relation:
                    break
                ancestor = relation.inviter
            else:
                return None
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


def record_typing_credit_performance(
    *,
    invitee: Player,
    season_id: int,
    config_version_id: int,
    attempt_source_id: str,
    source_time: datetime,
    credit_amount: int,
) -> None:
    """Freeze-worthy referral provenance at Typing completion, not wallet claim time."""
    from apps.arena.models import PerformanceSourceFact
    from apps.moderation.services import moderation_state_at

    config = resolve_game_config_version(config_version_id)
    if not config.referral.enabled or "TYPING_CLAIM" not in config.referral.eligible_reward_sources:
        return
    relation = (
        ReferralRelation.objects.select_related("inviter")
        .filter(
            invitee=invitee,
            status=ReferralRelation.Status.VERIFIED,
            verified_at__lt=source_time,
        )
        .first()
    )
    if relation is None:
        return
    blocked = set(config.referral.blocked_moderation_states) | {
        Player.ModerationState.SUSPENDED,
        Player.ModerationState.BANNED,
    }
    if (
        moderation_state_at(invitee, source_time) in blocked
        or moderation_state_at(relation.inviter, source_time) in blocked
    ):
        return
    tier = _tier(config, relation.inviter, source_time)
    if tier is None:
        return
    tier_key, share_bps = tier
    referral_amount = credit_amount * share_bps // 10000
    if referral_amount <= 0:
        return
    PerformanceSourceFact.objects.create(
        season_id=season_id,
        player=relation.inviter,
        category="VERIFIED_REFERRAL",
        source_type="TYPING_REFERRAL_CREDIT",
        source_id=attempt_source_id,
        source_time=source_time,
        amount=referral_amount,
        config_version_id=config_version_id,
        metadata={
            "invitee_id": invitee.pk,
            "relation_id": relation.pk,
            "tier_key": tier_key,
            "share_bps": share_bps,
            "typing_credit_amount": credit_amount,
        },
    )


def activate_relation(relation_id: int, *, at: datetime | None = None) -> bool:
    now = at or timezone.now()
    season = resolve_current_season(at=now, required=False)
    if season is None or not is_reward_earning_time(season, now):
        ReferralRelation.objects.filter(
            pk=relation_id, status=ReferralRelation.Status.PENDING
        ).update(next_evaluation_at=now + timedelta(hours=1))
        return False
    config = resolve_effective_game_config(now)
    if not config.referral.enabled:
        ReferralRelation.objects.filter(
            pk=relation_id, status=ReferralRelation.Status.PENDING
        ).update(next_evaluation_at=now + timedelta(hours=1))
        return False
    with transaction.atomic():
        relation = (
            ReferralRelation.objects.select_for_update()
            .select_related("invitee", "inviter")
            .get(pk=relation_id)
        )
        if relation.status == ReferralRelation.Status.VERIFIED:
            return False
        if not referral_activation_is_safe(relation, config, now):
            relation.next_evaluation_at = now + timedelta(hours=1)
            relation.save(update_fields=("next_evaluation_at", "updated_at"))
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


def referral_activation_is_safe(
    relation: ReferralRelation, config: ResolvedGameConfig, at: datetime
) -> bool:
    """Evaluate only durable structural, progression, age and moderation facts."""
    if relation.inviter_id == relation.invitee_id:
        return False
    progression = PlayerProgression.objects.filter(player=relation.invitee).first()
    level = (
        calculate_progression(progression.lifetime_points, config.progression).level
        if progression
        else 1
    )
    anti_sybil_ok = True
    if config.referral.anti_sybil_pass_required:
        from apps.arena.qualification import is_player_qualified

        predicate = (
            config.dlx_distribution.qualified_player_predicate if config.dlx_distribution else {}
        )
        anti_sybil_ok = is_player_qualified(
            relation.invitee,
            policy_key=str(predicate.get("policy_key", "QUALIFIED_PLAYER")),
            policy_version=int(predicate.get("policy_version", 1)),
            at=at,
        )
    return bool(
        anti_sybil_ok
        and progression
        and level >= config.referral.min_level
        and relation.invitee.first_seen_at
        <= at - timedelta(hours=config.referral.min_account_age_hours)
        and relation.invitee.moderation_state not in config.referral.blocked_moderation_states
        and relation.inviter.moderation_state not in config.referral.blocked_moderation_states
        and relation.invitee.moderation_state
        not in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
        and relation.inviter.moderation_state
        not in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
    )


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
    config = resolve_game_config_version(source.config_version_id)
    if (
        not config.referral.enabled
        or source.source_type in {"REFERRAL", "CREATOR", "NETWORK_SPEAKER", "BASE_TAP"}
        or source.season.status != "ACTIVE"
        or source.player.moderation_state in config.referral.blocked_moderation_states
        or source.player.moderation_state
        in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
        or (
            relation
            and (
                relation.inviter.moderation_state in config.referral.blocked_moderation_states
                or relation.inviter.moderation_state
                in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
            )
        )
    ):
        if source.season.status != "ACTIVE":
            raise DeferredReferralSettlement("Source Season is closed")
        return ReferralShareOutcome.objects.create(source_reward=source)
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
