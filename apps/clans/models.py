from django.db import models
from django.db.models import Q


class ClanRole(models.TextChoices):
    CAPTAIN = "CAPTAIN", "Captain"
    SOLDIER = "SOLDIER", "Soldier"


class PlayerSeasonClanState(models.Model):
    player = models.ForeignKey(
        "players.Player", on_delete=models.CASCADE, related_name="clan_states"
    )
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    role = models.CharField(max_length=8, choices=ClanRole.choices)
    voluntary_leave_count = models.PositiveSmallIntegerField(default=0)
    leave_cooldown_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("player", "season"), name="clan_one_role_player_season")
        ]


class Clan(models.Model):
    telegram_group = models.OneToOneField(
        "telegram.TelegramGroup", on_delete=models.PROTECT, related_name="clan"
    )
    name = models.CharField(max_length=64)
    tag = models.CharField(max_length=12)
    normalized_tag = models.CharField(max_length=12, unique=True)
    motto = models.CharField(max_length=160, blank=True)
    creator = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="created_clans"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class ClanSeason(models.Model):
    class State(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        RECOVERY = "RECOVERY", "Recovery compatible"
        ENDED = "ENDED", "Ended"

    clan = models.ForeignKey(Clan, on_delete=models.PROTECT, related_name="seasons")
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    captain = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="captained_clan_seasons"
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    base_capacity = models.PositiveSmallIntegerField()
    accepting_members = models.BooleanField(default=True)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("clan", "season"), name="clan_unique_identity_season"),
            models.UniqueConstraint(
                fields=("captain", "season"),
                condition=Q(state__in=["ACTIVE", "RECOVERY"]),
                name="clan_one_live_captain_season",
            ),
        ]


class ClanMembership(models.Model):
    class Access(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ACCESS_LOST = "ACCESS_LOST", "Access lost"

    class EndReason(models.TextChoices):
        VOLUNTARY_LEAVE = "VOLUNTARY_LEAVE", "Voluntary leave"

    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="clan_memberships"
    )
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="memberships"
    )
    role = models.CharField(max_length=8, choices=ClanRole.choices)
    access_state = models.CharField(max_length=12, choices=Access.choices, default=Access.ACTIVE)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=32, choices=EndReason.choices, blank=True)
    access_lost_at = models.DateTimeField(null=True, blank=True)
    access_rejoin_deadline = models.DateTimeField(null=True, blank=True)
    last_telegram_update_id = models.BigIntegerField(default=0)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "season"),
                condition=Q(ended_at__isnull=True),
                name="clan_unique_current_membership_season",
            )
        ]


class ClanMembershipEvent(models.Model):
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=32)
    telegram_update_id = models.BigIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField()
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "kind", "telegram_update_id"),
                name="clan_unique_membership_event",
            )
        ]


class ClanJoinRequest(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"
        ACTIVATED = "ACTIVATED", "Activated"

    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="join_requests"
    )
    applicant = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="clan_join_requests"
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "applicant"),
                condition=Q(state__in=["PENDING", "ACCEPTED"]),
                name="clan_unique_live_request",
            ),
            models.UniqueConstraint(
                fields=("applicant",), condition=Q(state="ACCEPTED"), name="clan_one_accepted_path"
            ),
        ]


class ClanInvite(models.Model):
    class Purpose(models.TextChoices):
        JOIN = "JOIN", "Join"
        REJOIN = "REJOIN", "Rejoin"

    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CREATING = "CREATING", "Creating"
        ACTIVE = "ACTIVE", "Active"
        FAILED = "FAILED", "Failed"
        UNCERTAIN = "UNCERTAIN", "Uncertain"
        USED = "USED", "Used"
        REVOKED = "REVOKED", "Revoked"
        REVOKE_PENDING = "REVOKE_PENDING", "Revoke pending"
        REVOKE_FAILED = "REVOKE_FAILED", "Revoke failed"

    class Delivery(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    join_request = models.OneToOneField(
        ClanJoinRequest, on_delete=models.PROTECT, related_name="invite", null=True, blank=True
    )
    membership = models.OneToOneField(
        ClanMembership,
        on_delete=models.PROTECT,
        related_name="rejoin_invite",
        null=True,
        blank=True,
    )
    purpose = models.CharField(max_length=8, choices=Purpose.choices, default=Purpose.JOIN)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    invite_link = models.TextField(blank=True)
    telegram_invite_name = models.CharField(max_length=64, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    claim_token = models.UUIDField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    uncertain_since = models.DateTimeField(null=True, blank=True)
    revoke_attempts = models.PositiveSmallIntegerField(default=0)
    desired_terminal_state = models.CharField(max_length=8, blank=True)
    last_error_category = models.CharField(max_length=64, blank=True)
    delivery_state = models.CharField(
        max_length=8, choices=Delivery.choices, default=Delivery.PENDING
    )
    delivery_error_category = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(purpose="JOIN", join_request__isnull=False, membership__isnull=True)
                    | Q(purpose="REJOIN", join_request__isnull=True, membership__isnull=False)
                ),
                name="clan_invite_exactly_one_target",
            )
        ]
