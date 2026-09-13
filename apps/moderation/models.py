import uuid

from django.conf import settings
from django.db import models


class ModerationCase(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reporter = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="moderation_reports"
    )
    target = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="moderation_cases"
    )
    clan_season = models.ForeignKey(
        "clans.ClanSeason", on_delete=models.PROTECT, null=True, blank=True
    )
    telegram_group = models.ForeignKey(
        "telegram.TelegramGroup", on_delete=models.PROTECT, null=True, blank=True
    )
    category = models.CharField(max_length=32)
    reason = models.TextField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    evidence_retain_until = models.DateTimeField(null=True, blank=True, db_index=True)


class ModerationEvidence(models.Model):
    class Kind(models.TextChoices):
        NETWORK_ACTIVITY = "NETWORK_ACTIVITY", "Network activity"
        MEMBERSHIP_EVENT = "MEMBERSHIP_EVENT", "Membership event"
        MEMBERSHIP_INCIDENT = "MEMBERSHIP_INCIDENT", "Membership incident"
        RISK_FACT = "RISK_FACT", "Server risk fact"
        MANUAL_NOTE = "MANUAL_NOTE", "Manual moderator note"

    case = models.ForeignKey(ModerationCase, on_delete=models.PROTECT, related_name="evidence")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    manual_note = models.TextField(blank=True)
    verified_server_observation = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("view_sensitive_evidence", "Can view sensitive moderation evidence")]


class ModerationAction(models.Model):
    class Type(models.TextChoices):
        DISMISS = "DISMISS", "Dismiss"
        WARN = "WARN", "Warn"
        FLAG = "FLAG", "Flag"
        SUSPEND = "SUSPEND", "Suspend"
        BAN = "BAN", "Ban"
        REMOVE_FROM_CLAN = "REMOVE_FROM_CLAN", "Remove from Clan"

    case = models.ForeignKey(ModerationCase, on_delete=models.PROTECT, related_name="actions")
    actor = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moderation_actions_taken",
    )
    staff_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moderation_actions_taken",
    )
    system_actor = models.CharField(max_length=64, blank=True)
    target = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="moderation_actions_received"
    )
    action_type = models.CharField(max_length=32, choices=Type.choices)
    reason = models.TextField()
    previous_state = models.JSONField(default=dict)
    resulting_state = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128, unique=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    suspension_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("apply_moderation_actions", "Can apply moderation actions")]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(action_type="SUSPEND")
                | models.Q(suspension_expires_at__isnull=False),
                name="moderation_suspend_has_expiry",
            )
        ]


class ModerationEvidenceAccess(models.Model):
    evidence = models.ForeignKey(ModerationEvidence, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)
    viewed_at = models.DateTimeField(auto_now_add=True)
