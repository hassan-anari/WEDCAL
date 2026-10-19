import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import Max, Sum

from apps.core.b3 import B3Error
from apps.core.periods import resolve_daily_period
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed
from apps.rewards.services import settle_player_reward
from apps.seasons.services import require_reward_earning_time, resolve_current_season
from apps.typing_game.calculations import accuracy_bps, calculate_reward, calculate_wpm
from apps.typing_game.models import (
    TypingAttempt,
    TypingClaim,
    TypingPhrase,
    TypingWallet,
    TypingWalletTransaction,
)

logger = structlog.get_logger(__name__)


class TypingUnavailable(B3Error, LookupError):
    def __init__(self, detail: str):
        super().__init__("TYPING_UNAVAILABLE")


class TypingQuotaExceeded(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TypingState:
    attempts_used: int
    attempts_remaining: int
    attempt_limit: int
    next_reset: datetime
    claimable_balance: int
    active_attempt: TypingAttempt | None
    best_wpm: int
    best_accuracy_bps: int


def _period(at: datetime, timezone_name: str):  # type: ignore[no-untyped-def]
    return resolve_daily_period(at, timezone_name)


def start_attempt(player: Player, idempotency_key: str, at: datetime) -> tuple[TypingAttempt, bool]:
    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("A bounded Idempotency-Key is required")
    resolved = resolve_effective_game_config(at)
    if not resolved.typing.enabled:
        raise TypingUnavailable("Typing is unavailable in the effective configuration")
    season = resolve_current_season(at=at)
    assert season is not None
    require_reward_earning_time(season, at)
    period = _period(at, resolved.typing.reset_timezone)
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(season.pk)
        player = Player.objects.select_for_update().get(pk=player.pk)
        if not reward_gameplay_allowed(player):
            raise B3Error("PLAYER_RESTRICTED", 403)
        existing = TypingAttempt.objects.filter(
            player=player, start_idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing, False
        TypingAttempt.objects.filter(
            player=player, status=TypingAttempt.Status.STARTED, expires_at__lte=at
        ).update(status=TypingAttempt.Status.EXPIRED)
        active = TypingAttempt.objects.filter(
            player=player, status=TypingAttempt.Status.STARTED
        ).first()
        if active:
            return active, False
        used = (
            TypingAttempt.objects.filter(player=player, reset_period=period.key)
            .exclude(status__in=[TypingAttempt.Status.STARTED, TypingAttempt.Status.EXPIRED])
            .count()
        )
        if used >= resolved.typing.attempts_per_day:
            raise TypingQuotaExceeded("Typing attempt quota exhausted")
        phrase = TypingPhrase.objects.filter(enabled=True).order_by("key", "-version").first()
        if phrase is None:
            raise TypingUnavailable("No published typing phrase is available")
        attempt = TypingAttempt.objects.create(
            player=player,
            phrase=phrase,
            season=season,
            config_version=GameConfigVersion.objects.get(pk=resolved.id),
            reset_period=period.key,
            start_idempotency_key=idempotency_key,
            started_at=at,
            expires_at=at + timedelta(seconds=resolved.typing.challenge_seconds),
        )
    logger.info("typing_attempt_started", player_id=player.pk, attempt_id=str(attempt.public_id))
    return attempt, True


def submit_attempt(
    player: Player, public_id: UUID, submitted_text: str, at: datetime
) -> tuple[TypingAttempt, bool]:
    if len(submitted_text) > 4096:
        raise ValueError("Typed result is too large")
    attempt_season_id = TypingAttempt.objects.values_list("season_id", flat=True).get(
        player=player, public_id=public_id
    )
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(attempt_season_id)
        player = Player.objects.select_for_update().get(pk=player.pk)
        attempt = (
            TypingAttempt.objects.select_for_update()
            .select_related("phrase", "season", "config_version")
            .get(player=player, public_id=public_id)
        )
        if attempt.status != TypingAttempt.Status.STARTED:
            return attempt, False
        require_reward_earning_time(attempt.season, at)
        resolved = resolve_effective_game_config(attempt.started_at)
        elapsed_ms = int((at - attempt.started_at).total_seconds() * 1000)
        normalized_target = unicodedata.normalize("NFC", attempt.phrase.text)
        normalized_submitted = unicodedata.normalize("NFC", submitted_text)
        normalized_length = len(normalized_target)
        accuracy = accuracy_bps(attempt.phrase.text, submitted_text)
        try:
            wpm = calculate_wpm(normalized_length, elapsed_ms)
        except ValueError:
            wpm = 0
        full = len(normalized_submitted) >= normalized_length
        if (
            not reward_gameplay_allowed(player)
            or at > attempt.expires_at
            or elapsed_ms <= 0
            or wpm >= resolved.typing.reject_wpm
        ):
            status = TypingAttempt.Status.REJECTED
        elif wpm >= resolved.typing.suspicious_wpm:
            status = TypingAttempt.Status.SUSPICIOUS
        elif (
            not full or accuracy < resolved.typing.min_accuracy_bps or wpm < resolved.typing.min_wpm
        ):
            status = TypingAttempt.Status.REJECTED
        else:
            status = TypingAttempt.Status.VALID
        reward = (
            calculate_reward(accuracy, wpm, resolved.typing)
            if status == TypingAttempt.Status.VALID
            else 0
        )
        attempt.status, attempt.submitted_at, attempt.submitted_text = status, at, submitted_text
        attempt.accuracy_bps, attempt.wpm, attempt.reward_amount = accuracy, wpm, reward
        attempt.save(
            update_fields=[
                "status",
                "submitted_at",
                "submitted_text",
                "accuracy_bps",
                "wpm",
                "reward_amount",
                "updated_at",
            ]
        )
        if reward:
            from apps.arena.models import PerformanceSourceFact
            from apps.referrals.services import record_typing_credit_performance

            wallet, _ = TypingWallet.objects.select_for_update().get_or_create(
                player=player, season=attempt.season
            )
            credit = TypingWalletTransaction.objects.create(
                wallet=wallet,
                kind=TypingWalletTransaction.Kind.CREDIT,
                amount=reward,
                attempt=attempt,
                season=attempt.season,
                config_version=attempt.config_version,
                idempotency_key=f"typing-credit:{attempt.public_id}",
            )
            # Weekly performance follows completion source time, never voluntary claim time.
            # The attempt one-to-one credit and fact source identity make retries idempotent.
            performance_fact = PerformanceSourceFact.objects.create(
                season=attempt.season,
                player=player,
                category="CORE_GAMEPLAY",
                source_type="TYPING_CREDIT",
                source_id=str(attempt.public_id),
                source_time=at,
                amount=reward,
                config_version=attempt.config_version,
                metadata={
                    "attempt_id": attempt.pk,
                    "credit_transaction_id": credit.pk,
                    "wpm": attempt.wpm,
                    "accuracy_bps": attempt.accuracy_bps,
                },
            )
            from apps.arena.recognition import record_core_fact

            record_core_fact(performance_fact)
            record_typing_credit_performance(
                invitee=player,
                season_id=attempt.season_id,
                config_version_id=attempt.config_version_id,
                attempt_source_id=str(attempt.public_id),
                source_time=at,
                credit_amount=reward,
            )
            wallet.claimable_balance += reward
            wallet.save(update_fields=["claimable_balance", "updated_at"])
            logger.info(
                "typing_wallet_credited",
                player_id=player.pk,
                attempt_id=str(attempt.public_id),
                amount=reward,
            )
        if status == TypingAttempt.Status.VALID:
            from apps.clans.missions import queue_valid_typing_run

            queue_valid_typing_run(attempt.pk)
    logger.info(
        "typing_attempt_submitted",
        player_id=player.pk,
        attempt_id=str(attempt.public_id),
        status=status,
    )
    return attempt, True


def claim_wallet(player: Player, idempotency_key: str, at: datetime) -> tuple[TypingClaim, bool]:
    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("A bounded Idempotency-Key is required")
    season = resolve_current_season(at=at)
    assert season is not None
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        existing = TypingClaim.objects.filter(
            player=player, idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing, False
        wallet, _ = TypingWallet.objects.select_for_update().get_or_create(
            player=player, season=season
        )
        claim = TypingClaim.objects.create(
            player=player,
            season=season,
            idempotency_key=idempotency_key,
            amount=wallet.claimable_balance,
            claimed_at=at,
        )
        if not wallet.claimable_balance:
            return claim, True
        credits = list(
            TypingWalletTransaction.objects.select_for_update()
            .filter(
                wallet=wallet, kind=TypingWalletTransaction.Kind.CREDIT, claimed_by__isnull=True
            )
            .select_related("config_version")
        )
        groups: dict[int, list[TypingWalletTransaction]] = defaultdict(list)
        for credit in credits:
            groups[credit.config_version_id].append(credit)
        for config_id, rows in groups.items():
            amount = sum(row.amount for row in rows)
            occurred_at = (
                rows[0].attempt.started_at if rows[0].attempt is not None else rows[0].created_at
            )
            resolved = resolve_effective_game_config(occurred_at)
            if resolved.id != config_id:
                raise ValueError("Typing credit config history is inconsistent")
            settle_player_reward(
                player=player,
                source_type="TYPING_CLAIM",
                source_id=f"{claim.pk}:{config_id}",
                context="TYPING",
                amount=amount,
                idempotency_key=f"typing-claim:{player.pk}:{idempotency_key}:{config_id}",
                occurred_at=occurred_at,
                resolved_config=resolved,
                metadata={"typing_claim_id": claim.pk},
            )
        TypingWalletTransaction.objects.filter(pk__in=[row.pk for row in credits]).update(
            claimed_by=claim
        )
        TypingWalletTransaction.objects.create(
            wallet=wallet,
            kind=TypingWalletTransaction.Kind.DEBIT,
            amount=wallet.claimable_balance,
            claim=claim,
            season=season,
            config_version=GameConfigVersion.objects.get(pk=credits[-1].config_version_id),
            idempotency_key=f"typing-debit:{claim.pk}",
        )
        wallet.claimable_balance = 0
        wallet.save(update_fields=["claimable_balance", "updated_at"])
    logger.info("typing_claimed", player_id=player.pk, claim_id=claim.pk, amount=claim.amount)
    return claim, True


def auto_settle_wallet_for_season(
    wallet_id: int, *, close_run_id: int, at: datetime
) -> tuple[TypingClaim, bool]:
    """Settle one wallet through the canonical claim ledger path using source provenance."""
    with transaction.atomic():
        wallet = (
            TypingWallet.objects.select_for_update()
            .select_related("player", "season")
            .get(pk=wallet_id)
        )
        key = f"season-close:{close_run_id}:typing:{wallet.pk}"
        existing = TypingClaim.objects.filter(player=wallet.player, idempotency_key=key).first()
        if existing:
            return existing, False
        credits = list(
            TypingWalletTransaction.objects.select_for_update()
            .filter(
                wallet=wallet, kind=TypingWalletTransaction.Kind.CREDIT, claimed_by__isnull=True
            )
            .select_related("attempt", "config_version")
            .order_by("pk")
        )
        amount = sum(row.amount for row in credits)
        claim = TypingClaim.objects.create(
            player=wallet.player,
            season=wallet.season,
            idempotency_key=key,
            amount=amount,
            claimed_at=at,
        )
        if not credits:
            if wallet.claimable_balance:
                raise ValueError("Typing wallet projection has no supporting unclaimed credits")
            return claim, True
        if amount != wallet.claimable_balance:
            raise ValueError("Typing wallet projection does not reconcile to source credits")
        groups: dict[int, list[TypingWalletTransaction]] = defaultdict(list)
        for credit in credits:
            groups[credit.config_version_id].append(credit)
        for config_id, rows in groups.items():
            occurred_at = min(
                row.attempt.submitted_at or row.attempt.started_at
                for row in rows
                if row.attempt is not None
            )
            resolved = resolve_effective_game_config(occurred_at)
            if resolved.id != config_id:
                raise ValueError("Typing credit config history is inconsistent")
            settle_player_reward(
                player=wallet.player,
                source_type="TYPING_CLAIM",
                source_id=f"close:{close_run_id}:{wallet.pk}:{config_id}",
                context="TYPING",
                amount=sum(row.amount for row in rows),
                idempotency_key=f"typing-close:{close_run_id}:{wallet.pk}:{config_id}",
                occurred_at=occurred_at,
                resolved_config=resolved,
                metadata={"typing_claim_id": claim.pk, "season_close_run_id": close_run_id},
                source_season=wallet.season,
                preserve_config_version=True,
                source_eligibility_verified=True,
            )
        TypingWalletTransaction.objects.filter(pk__in=[row.pk for row in credits]).update(
            claimed_by=claim
        )
        TypingWalletTransaction.objects.create(
            wallet=wallet,
            kind=TypingWalletTransaction.Kind.DEBIT,
            amount=amount,
            claim=claim,
            season=wallet.season,
            config_version=credits[-1].config_version,
            idempotency_key=f"typing-close-debit:{close_run_id}:{wallet.pk}",
        )
        wallet.claimable_balance = 0
        wallet.save(update_fields=("claimable_balance", "updated_at"))
        return claim, True


def rebuild_wallet(wallet: TypingWallet) -> TypingWallet:
    with transaction.atomic():
        wallet = TypingWallet.objects.select_for_update().get(pk=wallet.pk)
        totals = wallet.transactions.values("kind").annotate(total=Sum("amount"))
        amounts = {row["kind"]: row["total"] for row in totals}
        wallet.claimable_balance = int(amounts.get("CREDIT", 0) or 0) - int(
            amounts.get("DEBIT", 0) or 0
        )
        if wallet.claimable_balance < 0:
            raise ValueError("Typing transaction ledger has negative balance")
        wallet.save(update_fields=["claimable_balance", "updated_at"])
    logger.info("typing_wallet_rebuilt", wallet_id=wallet.pk)
    return wallet


def get_typing_state(player: Player, at: datetime) -> TypingState:
    resolved = resolve_effective_game_config(at)
    if not resolved.typing.enabled:
        raise TypingUnavailable("Typing is unavailable in the effective configuration")
    season = resolve_current_season(at=at)
    period = _period(at, resolved.typing.reset_timezone)
    attempts = TypingAttempt.objects.filter(player=player, reset_period=period.key)
    consumed = attempts.exclude(
        status__in=[TypingAttempt.Status.STARTED, TypingAttempt.Status.EXPIRED]
    )
    best = consumed.aggregate(best_wpm=Max("wpm"), best_accuracy=Max("accuracy_bps"))
    wallet = TypingWallet.objects.filter(player=player, season=season).first()
    return TypingState(
        consumed.count(),
        max(resolved.typing.attempts_per_day - consumed.count(), 0),
        resolved.typing.attempts_per_day,
        period.end,
        wallet.claimable_balance if wallet else 0,
        attempts.filter(status=TypingAttempt.Status.STARTED, expires_at__gt=at)
        .select_related("phrase")
        .first(),
        best["best_wpm"] or 0,
        best["best_accuracy"] or 0,
    )
