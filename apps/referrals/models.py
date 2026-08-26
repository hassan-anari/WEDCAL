from django.db import models

from apps.game_config.models import GameConfigVersion
from apps.players.models import Player
from apps.rewards.models import RewardLedger


class ReferralCode(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="referral_code")
    code = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk and ReferralCode.objects.get(pk=self.pk).code != self.code:
            raise ValueError("Referral codes are immutable")
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class ReferralRelation(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"

    inviter = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="invited_referrals")
    invitee = models.OneToOneField(
        Player, on_delete=models.PROTECT, related_name="referral_relation"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attributed_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_config = models.ForeignKey(GameConfigVersion, on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(inviter=models.F("invitee")), name="referral_not_self"
            )
        ]


class ReferralShareOutcome(models.Model):
    source_reward = models.OneToOneField(
        RewardLedger, on_delete=models.PROTECT, related_name="referral_share_outcome"
    )
    relation = models.ForeignKey(ReferralRelation, on_delete=models.PROTECT, null=True)
    share_reward = models.OneToOneField(
        RewardLedger, on_delete=models.PROTECT, null=True, related_name="referral_share_source"
    )
    tier_key = models.CharField(max_length=32, blank=True)
    share_bps = models.PositiveIntegerField(default=0)
    amount = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
