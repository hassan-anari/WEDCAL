# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections

from apps.core.periods import resolve_daily_period
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason, Season
from apps.typing_game.models import (
    TypingAttempt,
    TypingClaim,
    TypingPhrase,
    TypingWallet,
    TypingWalletTransaction,
)
from apps.typing_game.services import (
    TypingQuotaExceeded,
    TypingUnavailable,
    claim_wallet,
    rebuild_wallet,
    start_attempt,
    submit_attempt,
)

BASE = datetime(2026, 1, 2, 12, tzinfo=UTC)
PHRASE = "Dexo typing arena phrase"


def phrase(text: str = PHRASE) -> TypingPhrase:
    return TypingPhrase.objects.create(key="arena", version=1, text=text, enabled=True)


def valid_attempt(player, key: str, at: datetime, elapsed: int = 5):
    attempt, _ = start_attempt(player, key, at)
    return submit_attempt(
        player, attempt.public_id, attempt.phrase.text, at + timedelta(seconds=elapsed)
    )[0]


@pytest.mark.django_db
def test_start_requires_season_config_and_phrase(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    player = player_factory()
    with pytest.raises(LookupError):
        start_attempt(player, "no-season", BASE)
    season_factory(config)
    with pytest.raises(TypingUnavailable):
        start_attempt(player, "no-phrase", BASE)


@pytest.mark.django_db
def test_start_captures_server_state_and_is_idempotent(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    selected = phrase()
    first, created = start_attempt(player, "same-key", BASE)
    retry, retry_created = start_attempt(player, "same-key", BASE + timedelta(seconds=1))
    active, active_created = start_attempt(player, "different-key", BASE + timedelta(seconds=1))
    expected_period = resolve_daily_period(BASE, config.payload["typing"]["reset_timezone"])
    assert created and not retry_created and not active_created
    assert first.pk == retry.pk == active.pk
    assert first.phrase == selected and first.season == season and first.config_version == config
    assert first.started_at == BASE
    assert first.expires_at == BASE + timedelta(
        seconds=config.payload["typing"]["challenge_seconds"]
    )
    assert first.reset_period == expected_period.key
    assert TypingAttempt.objects.filter(status=TypingAttempt.Status.STARTED).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_starts_create_one_active_attempt(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    barrier = Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def run(key: str) -> None:
        close_old_connections()
        try:
            barrier.wait()
            results.append(start_attempt(player, key, BASE)[0].pk)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run, args=(f"key-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(set(results)) == 1
    assert TypingAttempt.objects.filter(status=TypingAttempt.Status.STARTED).count() == 1


@pytest.mark.django_db
def test_stale_attempt_expires_and_quota_resets(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    selected = phrase()
    stale, _ = start_attempt(player, "stale", BASE)
    next_time = stale.expires_at + timedelta(seconds=1)
    fresh, _ = start_attempt(player, "fresh", next_time)
    stale.refresh_from_db()
    assert (
        stale.status == TypingAttempt.Status.EXPIRED
        and fresh.status == TypingAttempt.Status.STARTED
    )
    fresh.status = TypingAttempt.Status.REJECTED
    fresh.submitted_at = next_time
    fresh.save(update_fields=["status", "submitted_at"])
    period = fresh.reset_period
    for index in range(config.payload["typing"]["attempts_per_day"] - 1):
        TypingAttempt.objects.create(
            player=player,
            phrase=selected,
            season=season,
            config_version=config,
            reset_period=period,
            start_idempotency_key=f"used-{index}",
            status=TypingAttempt.Status.REJECTED,
            started_at=next_time,
            expires_at=next_time + timedelta(seconds=30),
            submitted_at=next_time + timedelta(seconds=1),
        )
    with pytest.raises(TypingQuotaExceeded):
        start_attempt(player, "over-quota", next_time)
    tomorrow, _ = start_attempt(player, "tomorrow", next_time + timedelta(days=1))
    assert tomorrow.status == TypingAttempt.Status.STARTED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("submitted", "elapsed", "expected_status"),
    [
        (PHRASE, 5, TypingAttempt.Status.VALID),
        (PHRASE[:-1] + "x", 5, TypingAttempt.Status.VALID),
        (PHRASE + "!", 5, TypingAttempt.Status.VALID),
        (PHRASE[:-1], 5, TypingAttempt.Status.REJECTED),
        (PHRASE, 31, TypingAttempt.Status.REJECTED),
    ],
)
def test_submission_accuracy_completion_deadline_and_wallet(
    phase4_config_factory, player_factory, season_factory, submitted, elapsed, expected_status
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt, _ = start_attempt(player, "submit", BASE)
    result, created = submit_attempt(
        player, attempt.public_id, submitted, BASE + timedelta(seconds=elapsed)
    )
    assert created and result.status == expected_status
    assert result.accuracy_bps == (10000 if submitted == PHRASE else result.accuracy_bps)
    credits = TypingWalletTransaction.objects.filter(kind=TypingWalletTransaction.Kind.CREDIT)
    assert credits.exists() is (expected_status == TypingAttempt.Status.VALID)


@pytest.mark.django_db
def test_unicode_completion_uses_same_nfc_semantics_as_accuracy(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase("café typing arena phrase")
    attempt, _ = start_attempt(player, "unicode", BASE)
    result, _ = submit_attempt(
        player, attempt.public_id, "cafe\u0301 typing arena phrase", BASE + timedelta(seconds=5)
    )
    assert result.accuracy_bps == 10000 and result.status == TypingAttempt.Status.VALID


@pytest.mark.django_db
def test_server_timing_classification_thresholds_and_no_player_ban(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    target_len = len(PHRASE)
    suspicious_ms = target_len * 12000 // config.payload["typing"]["anti_cheat"]["suspicious_wpm"]
    reject_ms = target_len * 12000 // config.payload["typing"]["anti_cheat"]["reject_wpm"]
    suspicious, _ = start_attempt(player, "suspicious", BASE)
    suspicious_result, _ = submit_attempt(
        player, suspicious.public_id, PHRASE, BASE + timedelta(milliseconds=suspicious_ms)
    )
    rejected, _ = start_attempt(player, "rejected", BASE + timedelta(seconds=1))
    rejected_result, _ = submit_attempt(
        player, rejected.public_id, PHRASE, rejected.started_at + timedelta(milliseconds=reject_ms)
    )
    assert suspicious_result.status == TypingAttempt.Status.SUSPICIOUS
    assert rejected_result.status == TypingAttempt.Status.REJECTED
    assert not TypingWalletTransaction.objects.exists()
    player.refresh_from_db()
    assert not hasattr(player, "banned")


@pytest.mark.django_db
def test_resubmit_preserves_original_result_and_single_credit(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt, _ = start_attempt(player, "retry", BASE)
    original, created = submit_attempt(
        player, attempt.public_id, PHRASE, BASE + timedelta(seconds=5)
    )
    conflicting, retry_created = submit_attempt(
        player, attempt.public_id, "wrong", BASE + timedelta(seconds=10)
    )
    assert created and not retry_created and conflicting.pk == original.pk
    assert conflicting.accuracy_bps == original.accuracy_bps == 10000
    assert conflicting.reward_amount == original.reward_amount
    assert TypingWalletTransaction.objects.filter(kind="CREDIT").count() == 1
    assert TypingWallet.objects.get(player=player).claimable_balance == original.reward_amount


@pytest.mark.django_db(transaction=True)
def test_concurrent_submit_credits_once(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt, _ = start_attempt(player, "concurrent-submit", BASE)
    barrier = Barrier(2)
    errors: list[BaseException] = []

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait()
            submit_attempt(player, attempt.public_id, PHRASE, BASE + timedelta(seconds=5))
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert TypingWalletTransaction.objects.filter(kind="CREDIT").count() == 1
    wallet = TypingWallet.objects.get(player=player)
    assert wallet.claimable_balance == TypingAttempt.objects.get(pk=attempt.pk).reward_amount


@pytest.mark.django_db
def test_submit_technical_failure_rolls_back_attempt_and_wallet(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt, _ = start_attempt(player, "rollback", BASE)
    with (
        patch.object(TypingWalletTransaction.objects, "create", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        submit_attempt(player, attempt.public_id, PHRASE, BASE + timedelta(seconds=5))
    attempt.refresh_from_db()
    assert attempt.status == TypingAttempt.Status.STARTED and attempt.submitted_at is None
    assert not TypingWallet.objects.exists() and not TypingWalletTransaction.objects.exists()


@pytest.mark.django_db
def test_wallet_ledger_rebuild_and_append_only_guards(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    first = valid_attempt(player, "first", BASE)
    second = valid_attempt(player, "second", BASE + timedelta(minutes=1))
    wallet = TypingWallet.objects.get(player=player)
    expected = first.reward_amount + second.reward_amount
    assert wallet.claimable_balance == expected
    wallet.claimable_balance = 1
    wallet.save(update_fields=["claimable_balance"])
    assert rebuild_wallet(wallet).claimable_balance == expected
    transaction = wallet.transactions.filter(kind="CREDIT").first()
    assert transaction is not None
    transaction.amount += 1
    with pytest.raises(ValidationError, match="append-only"):
        transaction.save()
    with pytest.raises(ValidationError, match="append-only"):
        transaction.delete()


@pytest.mark.django_db
def test_wallet_negative_rebuild_fails_safely(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    wallet = TypingWallet.objects.create(player=player, season=season)
    TypingWalletTransaction.objects.create(
        wallet=wallet,
        kind="DEBIT",
        amount=1,
        season=season,
        config_version=config,
        idempotency_key="bad-debit",
    )
    with pytest.raises(ValueError, match="negative"):
        rebuild_wallet(wallet)
    wallet.refresh_from_db()
    assert wallet.claimable_balance == 0


@pytest.mark.django_db
def test_claim_multiple_credits_idempotency_and_accounting(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    phrase()
    attempts = [
        valid_attempt(player, f"attempt-{index}", BASE + timedelta(minutes=index))
        for index in range(2)
    ]
    expected = sum(row.reward_amount for row in attempts)
    claim, created = claim_wallet(player, "claim-key", BASE + timedelta(hours=1))
    retry, retry_created = claim_wallet(player, "claim-key", BASE + timedelta(hours=1))
    wallet = TypingWallet.objects.get(player=player, season=season)
    assert created and not retry_created and retry.pk == claim.pk and claim.amount == expected
    assert wallet.claimable_balance == 0
    assert wallet.transactions.filter(kind="DEBIT", claim=claim, amount=expected).count() == 1
    assert RewardLedger.objects.count() == 1
    assert RewardLedger.objects.get().player_amount == expected
    assert PlayerProgression.objects.get(player=player).lifetime_points == expected
    assert PlayerSeason.objects.get(player=player, season=season).season_points == expected


@pytest.mark.django_db
def test_claim_reward_failure_is_atomic(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt = valid_attempt(player, "credit", BASE)
    wallet = TypingWallet.objects.get(player=player)
    with (
        patch("apps.typing_game.services.settle_player_reward", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        claim_wallet(player, "failed", BASE + timedelta(hours=1))
    wallet.refresh_from_db()
    assert wallet.claimable_balance == attempt.reward_amount
    assert not TypingClaim.objects.exists()
    assert not wallet.transactions.filter(kind="DEBIT").exists()
    assert wallet.transactions.filter(kind="CREDIT", claimed_by__isnull=True).count() == 1
    assert not RewardLedger.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_different_claim_keys_consume_balance_once(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    phrase()
    attempt = valid_attempt(player, "credit", BASE)
    barrier = Barrier(2)
    errors: list[BaseException] = []
    amounts: list[int] = []

    def run(key: str) -> None:
        close_old_connections()
        try:
            barrier.wait()
            amounts.append(claim_wallet(player, key, BASE + timedelta(hours=1))[0].amount)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run, args=(f"claim-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and sorted(amounts) == [0, attempt.reward_amount]
    assert RewardLedger.objects.count() == 1
    assert RewardLedger.objects.get().player_amount == attempt.reward_amount
    assert TypingWallet.objects.get(player=player).claimable_balance == 0


@pytest.mark.django_db
def test_claim_groups_original_config_versions_without_repricing(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config_v1 = phase4_config_factory(
        effective_at=datetime(2026, 1, 1, tzinfo=UTC), version="typing-v1"
    )
    season = season_factory(config_v1)
    player = player_factory()
    phrase()
    first = valid_attempt(player, "v1", BASE)

    def mutate(payload):
        payload["typing"]["accuracy_reward_brackets"][-1]["points"] -= 1

    switch = BASE + timedelta(minutes=1)
    config_v2 = phase4_config_factory(effective_at=switch, mutate=mutate, version="typing-v2")
    second = valid_attempt(player, "v2", switch)
    claim, _ = claim_wallet(player, "mixed", switch + timedelta(hours=1))
    ledgers = list(RewardLedger.objects.order_by("config_version_id"))
    assert claim.season == season and len(ledgers) == 2
    assert {(row.config_version_id, row.player_amount) for row in ledgers} == {
        (config_v1.pk, first.reward_amount),
        (config_v2.pk, second.reward_amount),
    }
    assert sum(row.player_amount for row in ledgers) == first.reward_amount + second.reward_amount


@pytest.mark.django_db
def test_old_season_credit_is_not_moved_to_new_season(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    old = season_factory(config, ends_at=BASE + timedelta(days=1))
    player = player_factory()
    phrase()
    attempt = valid_attempt(player, "old", BASE)
    old.status = Season.Status.CLOSED
    old.save(update_fields=["status"])
    season_factory(config, starts_at=BASE + timedelta(days=1), ends_at=BASE + timedelta(days=10))
    new_claim, _ = claim_wallet(player, "new-season", BASE + timedelta(days=2))
    assert new_claim.amount == 0
    assert (
        TypingWallet.objects.get(player=player, season=old).claimable_balance
        == attempt.reward_amount
    )
    assert not RewardLedger.objects.exists()
