import uuid

from django.db import models
from django.db.models import Q


class TypingPhrase(models.Model):
    key = models.SlugField(max_length=100)
    version = models.PositiveIntegerField()
    text = models.TextField()
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("key", "version"), name="typing_phrase_key_version")
        ]

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk and TypingAttempt.objects.filter(phrase=self).exists():
            original = TypingPhrase.objects.get(pk=self.pk)
            if (
                original.text != self.text
                or original.key != self.key
                or original.version != self.version
            ):
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    "Create a new phrase version instead of changing used content"
                )
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class TypingAttempt(models.Model):
    class Status(models.TextChoices):
        STARTED = "STARTED", "Started"
        VALID = "VALID", "Valid"
        SUSPICIOUS = "SUSPICIOUS", "Suspicious"
        REJECTED = "REJECTED", "Rejected"
        EXPIRED = "EXPIRED", "Expired"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    phrase = models.ForeignKey(TypingPhrase, on_delete=models.PROTECT, related_name="attempts")
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    reset_period = models.CharField(max_length=64)
    start_idempotency_key = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STARTED)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True)
    submitted_text = models.TextField(blank=True)
    accuracy_bps = models.PositiveIntegerField(null=True)
    wpm = models.PositiveIntegerField(null=True)
    reward_amount = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "start_idempotency_key"), name="typing_start_idempotency"
            ),
            models.UniqueConstraint(
                fields=("player",), condition=Q(status="STARTED"), name="typing_one_active_attempt"
            ),
        ]
        indexes = [
            models.Index(fields=("player", "reset_period", "status"), name="typing_player_period")
        ]


class TypingWallet(models.Model):
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    claimable_balance = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "season"), name="typing_wallet_player_season"
            ),
            models.CheckConstraint(
                condition=Q(claimable_balance__gte=0), name="typing_wallet_nonnegative"
            ),
        ]


class TypingClaim(models.Model):
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=100)
    amount = models.PositiveBigIntegerField(default=0)
    claimed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "idempotency_key"), name="typing_claim_idempotency"
            )
        ]


class TypingWalletTransaction(models.Model):
    class Kind(models.TextChoices):
        CREDIT = "CREDIT", "Credit"
        DEBIT = "DEBIT", "Debit"

    wallet = models.ForeignKey(TypingWallet, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = models.PositiveBigIntegerField()
    attempt = models.OneToOneField(
        TypingAttempt, on_delete=models.PROTECT, null=True, related_name="wallet_credit"
    )
    claim = models.ForeignKey(
        TypingClaim, on_delete=models.PROTECT, null=True, related_name="wallet_debits"
    )
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=255, unique=True)
    claimed_by = models.ForeignKey(
        TypingClaim, on_delete=models.PROTECT, null=True, related_name="claimed_credits"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            from django.core.exceptions import ValidationError

            original = TypingWalletTransaction.objects.get(pk=self.pk)
            immutable = (
                "wallet_id",
                "kind",
                "amount",
                "attempt_id",
                "claim_id",
                "season_id",
                "config_version_id",
                "idempotency_key",
            )
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Typing wallet transactions are append-only")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        from django.core.exceptions import ValidationError

        raise ValidationError("Typing wallet transactions are append-only")
