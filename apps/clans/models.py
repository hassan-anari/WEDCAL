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
    recovery_reason = models.CharField(max_length=32, blank=True)
    recovery_started_at = models.DateTimeField(null=True, blank=True)
    bot_admin_lost_at = models.DateTimeField(null=True, blank=True, db_index=True)
    bot_admin_restore_deadline = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("clan", "season"), name="clan_unique_identity_season"),
            models.UniqueConstraint(
                fields=("captain", "season"),
                condition=Q(state__in=["ACTIVE", "RECOVERY"]),
                name="clan_one_live_captain_season",
            ),
        ]


class ClanSeasonEvent(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=40)
    occurred_at = models.DateTimeField()
    source_type = models.CharField(max_length=32, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "kind", "source_type", "source_id"),
                name="clan_unique_season_event_source",
            )
        ]


class ClanMembership(models.Model):
    class Access(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ACCESS_LOST = "ACCESS_LOST", "Access lost"

    class EndReason(models.TextChoices):
        VOLUNTARY_LEAVE = "VOLUNTARY_LEAVE", "Voluntary leave"
        INACTIVITY_REMOVAL = "INACTIVITY_REMOVAL", "Inactivity removal"
        GOVERNANCE_REMOVAL = "GOVERNANCE_REMOVAL", "Governance removal"
        MODERATION_REMOVAL = "MODERATION_REMOVAL", "Moderation removal"
        CLAN_DISBAND_RELEASE = "CLAN_DISBAND_RELEASE", "Clan disband release"

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
    telegram_verified_at = models.DateTimeField(null=True, blank=True)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    last_clan_active_at = models.DateTimeField(null=True, blank=True, db_index=True)
    inactivity_warned_at = models.DateTimeField(null=True, blank=True)
    next_inactivity_check_at = models.DateTimeField(null=True, blank=True, db_index=True)

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


class ClanMembershipIncident(models.Model):
    membership = models.ForeignKey(
        ClanMembership, on_delete=models.PROTECT, related_name="incidents"
    )
    kind = models.CharField(max_length=32, default="UNEXPECTED_TELEGRAM_REMOVAL")
    telegram_update_id = models.BigIntegerField()
    telegram_actor_user_id = models.BigIntegerField(null=True, blank=True)
    observed_at = models.DateTimeField()
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "telegram_update_id"), name="clan_unique_membership_incident"
            )
        ]


class ClanExpectedRemoval(models.Model):
    membership = models.OneToOneField(
        ClanMembership, on_delete=models.PROTECT, related_name="expected_removal"
    )
    reason = models.CharField(max_length=32, choices=ClanMembership.EndReason.choices)
    source_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)


class ClanGovernanceCase(models.Model):
    class Type(models.TextChoices):
        SOLDIER_REMOVAL = "SOLDIER_REMOVAL", "Soldier removal"
        CAPTAIN_DISSOLUTION = "CAPTAIN_DISSOLUTION", "Captain dissolution"
        RECOVERY_DISSOLUTION = "RECOVERY_DISSOLUTION", "Recovery dissolution"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="governance_cases"
    )
    initiator = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="initiated_governance_cases"
    )
    target_membership = models.ForeignKey(
        ClanMembership, on_delete=models.PROTECT, null=True, blank=True
    )
    case_type = models.CharField(max_length=32, choices=Type.choices)
    reason = models.CharField(max_length=32)
    opened_at = models.DateTimeField()
    voting_deadline = models.DateTimeField(db_index=True)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    quorum_bps = models.PositiveSmallIntegerField()
    approval_bps = models.PositiveSmallIntegerField()
    minimum_eligible_voters = models.PositiveSmallIntegerField()
    eligible_count = models.PositiveIntegerField()
    yes_count = models.PositiveIntegerField(default=0)
    no_count = models.PositiveIntegerField(default=0)
    valid_cast_count = models.PositiveIntegerField(default=0)
    quorum_met = models.BooleanField(null=True)
    approval_met = models.BooleanField(null=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    finalized_at = models.DateTimeField(null=True, blank=True)
    execution_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "case_type"),
                condition=Q(status="OPEN"),
                name="clan_one_open_governance_type",
            ),
            models.CheckConstraint(condition=Q(quorum_bps__lte=10000), name="clan_case_quorum_bps"),
            models.CheckConstraint(
                condition=Q(approval_bps__lte=10000), name="clan_case_approval_bps"
            ),
        ]


class ClanGovernanceElector(models.Model):
    case = models.ForeignKey(ClanGovernanceCase, on_delete=models.PROTECT, related_name="electors")
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("case", "player"), name="clan_unique_case_elector")
        ]


class ClanGovernanceVote(models.Model):
    class Choice(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    case = models.ForeignKey(ClanGovernanceCase, on_delete=models.PROTECT, related_name="votes")
    voter = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    choice = models.CharField(max_length=3, choices=Choice.choices)
    cast_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("case", "voter"), name="clan_one_vote_per_voter")
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
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual review"

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
    next_create_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    unknown_credential_risk = models.BooleanField(default=False)
    next_revoke_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoke_claim_token = models.UUIDField(null=True, blank=True)
    revoke_claim_expires_at = models.DateTimeField(null=True, blank=True)
    recovery_cycle = models.PositiveSmallIntegerField(default=1)
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
