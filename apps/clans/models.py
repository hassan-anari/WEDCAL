from django.db import models
from django.db.models import Q
from django.utils import timezone


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
    recovery_governance_opened_at = models.DateTimeField(null=True, blank=True)
    captain_authority_lost_at = models.DateTimeField(null=True, blank=True, db_index=True)
    captain_authority_observed_at = models.DateTimeField(null=True, blank=True)
    captain_authority_last_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    bot_admin_observed_at = models.DateTimeField(null=True, blank=True)
    captain_inactivity_recovery_at = models.DateTimeField(null=True, blank=True)
    bot_admin_recovery_at = models.DateTimeField(null=True, blank=True)
    hq_xp = models.PositiveBigIntegerField(default=0)
    hq_level = models.PositiveSmallIntegerField(default=1)
    capacity_bonus = models.PositiveSmallIntegerField(default=0)
    treasury_balance = models.PositiveBigIntegerField(default=0)
    treasury_earned_total = models.PositiveBigIntegerField(default=0)
    contribution_total = models.PositiveBigIntegerField(default=0)
    competitive_score = models.PositiveBigIntegerField(default=0)
    score_reached_at = models.DateTimeField(null=True, blank=True)
    ranking_generation = models.PositiveBigIntegerField(default=0)
    hq_conversion_remainder = models.PositiveBigIntegerField(default=0)
    score_conversion_remainder = models.PositiveBigIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=("season", "-competitive_score", "score_reached_at", "id"))]
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
    contribution_total = models.PositiveBigIntegerField(default=0)

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
    result_reason = models.CharField(max_length=40, blank=True)

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
    """Applicant-locked services enforce one live request globally, retaining legacy audit rows."""

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
        permissions = [("resolve_invite_risk", "Can resolve unsafe Clan invite credentials")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(purpose="JOIN", join_request__isnull=False, membership__isnull=True)
                    | Q(purpose="REJOIN", join_request__isnull=True, membership__isnull=False)
                ),
                name="clan_invite_exactly_one_target",
            )
        ]


class ClanEconomicSettlement(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="economic_settlements"
    )
    membership = models.ForeignKey(
        ClanMembership, on_delete=models.PROTECT, related_name="economic_settlements"
    )
    source_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=128)
    gross_amount = models.PositiveBigIntegerField()
    player_amount = models.PositiveBigIntegerField()
    treasury_amount = models.PositiveBigIntegerField()
    contribution_amount = models.PositiveBigIntegerField()
    hq_xp_amount = models.PositiveBigIntegerField(default=0)
    clan_score_amount = models.PositiveBigIntegerField(default=0)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    reward = models.OneToOneField("rewards.RewardLedger", on_delete=models.PROTECT, null=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    occurred_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ClanTreasuryEntry(models.Model):
    class Direction(models.TextChoices):
        CREDIT = "CREDIT", "Credit"
        DEBIT = "DEBIT", "Debit"

    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="treasury_entries"
    )
    direction = models.CharField(max_length=6, choices=Direction.choices)
    amount = models.PositiveBigIntegerField()
    source_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=128)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=255, unique=True)
    occurred_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="clan_treasury_positive")
        ]
        indexes = [
            models.Index(fields=("clan_season", "-occurred_at"), name="clan_treasury_history")
        ]


class ClanContributionEntry(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="contribution_entries"
    )
    membership = models.ForeignKey(
        ClanMembership, on_delete=models.PROTECT, related_name="contribution_entries"
    )
    settlement = models.OneToOneField(
        ClanEconomicSettlement, on_delete=models.PROTECT, related_name="contribution_entry"
    )
    amount = models.PositiveBigIntegerField()
    gross_basis = models.PositiveBigIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()


class ClanHQXPEntry(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="hq_xp_entries"
    )
    source_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=128)
    amount = models.PositiveBigIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=255, unique=True)
    occurred_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)


class ClanActivity(models.Model):
    class Classification(models.TextChoices):
        VALID = "VALID", "Valid"
        SUSPICIOUS = "SUSPICIOUS", "Suspicious"
        REJECTED = "REJECTED", "Rejected"
        CONTROL = "CONTROL", "Control"
        LATE = "LATE", "Late after finalization"

    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT, related_name="activity")
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT, null=True)
    sender = models.ForeignKey("players.Player", on_delete=models.PROTECT, null=True)
    group = models.ForeignKey("telegram.TelegramGroup", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    telegram_message_id = models.BigIntegerField()
    telegram_update_id = models.BigIntegerField()
    observed_at = models.DateTimeField(db_index=True)
    character_count = models.PositiveIntegerField(default=0)
    rewardable_chars = models.PositiveIntegerField(default=0)
    classification = models.CharField(max_length=16, choices=Classification.choices)
    exact_fingerprint = models.CharField(max_length=64, blank=True)
    normalized_fingerprint = models.CharField(max_length=64, blank=True)
    window_processed = models.BooleanField(default=False, db_index=True)
    membership_eligible_at_source = models.BooleanField(default=False)
    home_integrity_at_source = models.BooleanField(default=False)
    context_owned_at_source = models.BooleanField(default=False)
    captain_bonus_eligible_at_source = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("group", "telegram_message_id"), name="clan_unique_group_message"
            )
        ]
        indexes = [
            models.Index(fields=("membership", "observed_at"), name="clan_activity_member_time")
        ]


class ClanActivityWindow(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT)
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField(null=True)
    eligible_chars = models.PositiveBigIntegerField(default=0)
    gross_amount = models.PositiveBigIntegerField(default=0)
    settlement = models.OneToOneField(ClanEconomicSettlement, on_delete=models.PROTECT, null=True)
    finalized_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "season", "config_version", "window_start"),
                name="clan_unique_activity_window",
            )
        ]


class ClanInventory(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT, related_name="inventory")
    item_id = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "item_id"), name="clan_unique_inventory_item"
            )
        ]


class ClanItemPurchase(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT)
    purchaser = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    item_id = models.CharField(max_length=64)
    price = models.PositiveBigIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    treasury_entry = models.OneToOneField(ClanTreasuryEntry, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ClanItemUse(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT, related_name="item_uses")
    actor = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    item_id = models.CharField(max_length=64)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=255, unique=True)
    result = models.JSONField(default=dict)
    used_at = models.DateTimeField()


class ClanActiveEffect(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="active_effects"
    )
    item_use = models.ForeignKey(ClanItemUse, on_delete=models.PROTECT, related_name="effects")
    category = models.CharField(max_length=64)
    value = models.PositiveIntegerField()
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)


class ClanAchievementUnlock(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="achievement_unlocks"
    )
    key = models.CharField(max_length=64)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    unlocked_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("clan_season", "key"), name="clan_unique_achievement")
        ]


class ClanCaptainBonusDaily(models.Model):
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT)
    period_key = models.CharField(max_length=64)
    eligible_gross = models.PositiveBigIntegerField(default=0)
    awarded_amount = models.PositiveBigIntegerField(default=0)
    processed_sources = models.JSONField(default=list)
    config_version = models.ForeignKey(
        "game_config.GameConfigVersion", on_delete=models.PROTECT, null=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "period_key"), name="clan_captain_bonus_day"
            )
        ]


class ClanCaptainBonusSource(models.Model):
    daily = models.ForeignKey(
        ClanCaptainBonusDaily, on_delete=models.PROTECT, related_name="sources"
    )
    source_key = models.CharField(max_length=160)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    eligible_gross = models.PositiveBigIntegerField()
    awarded_amount = models.PositiveBigIntegerField(default=0)
    occurred_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("daily", "source_key"), name="clan_unique_captain_bonus_source"
            )
        ]


class ClanCaptainBonusConfigAccumulator(models.Model):
    daily = models.ForeignKey(
        ClanCaptainBonusDaily, on_delete=models.PROTECT, related_name="config_accumulators"
    )
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    eligible_gross = models.PositiveBigIntegerField(default=0)
    calculation_remainder = models.PositiveIntegerField(default=0)
    calculated_amount = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("daily", "config_version"), name="clan_unique_captain_bonus_config"
            )
        ]


class ClanMissionDefinition(models.Model):
    class Kind(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Individual"
        GROUP = "GROUP", "Group"

    key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices)
    requirement = models.CharField(max_length=64)
    target_units = models.PositiveBigIntegerField()
    reward_band = models.CharField(max_length=16)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    enabled = models.BooleanField(default=False)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("key", "version"), name="clan_unique_mission_version")
        ]


class ClanMissionProgress(models.Model):
    definition = models.ForeignKey(ClanMissionDefinition, on_delete=models.PROTECT)
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT)
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT, null=True)
    units = models.PositiveBigIntegerField(default=0)
    completed_at = models.DateTimeField(null=True)
    reward_settlement = models.OneToOneField(
        ClanEconomicSettlement, on_delete=models.PROTECT, null=True
    )
    progress_remainder = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("definition", "clan_season", "membership"),
                name="clan_unique_mission_progress",
                nulls_distinct=False,
            )
        ]


class ClanEconomicAccumulator(models.Model):
    clan_season = models.ForeignKey(
        ClanSeason, on_delete=models.PROTECT, related_name="economic_accumulators"
    )
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    contribution_remainder = models.PositiveBigIntegerField(default=0)
    hq_remainder = models.PositiveBigIntegerField(default=0)
    score_remainder = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("clan_season", "config_version"),
                name="clan_unique_economy_accumulator",
            )
        ]


class ClanMissionSourceEvent(models.Model):
    definition = models.ForeignKey(ClanMissionDefinition, on_delete=models.PROTECT)
    clan_season = models.ForeignKey(ClanSeason, on_delete=models.PROTECT)
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT)
    source_id = models.CharField(max_length=128)
    units = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("definition", "source_id"), name="clan_unique_mission_source"
            )
        ]


class ClanMissionParticipant(models.Model):
    progress = models.ForeignKey(
        ClanMissionProgress, on_delete=models.PROTECT, related_name="participants"
    )
    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT)
    reward_settlement = models.OneToOneField(
        ClanEconomicSettlement, on_delete=models.PROTECT, null=True
    )
    eligibility_evaluated_at = models.DateTimeField(null=True)
    reward_eligible = models.BooleanField(null=True)
    ineligibility_reason = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("progress", "membership"), name="clan_unique_mission_participant"
            )
        ]


class ClanIdempotencyKey(models.Model):
    key = models.CharField(max_length=200, unique=True)
    action = models.CharField(max_length=32)
    semantic_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class ClanMissionSignal(models.Model):
    """Durable source adapter for the existing mission settlement engine."""

    membership = models.ForeignKey(ClanMembership, on_delete=models.PROTECT)
    source_key = models.CharField(max_length=128, unique=True)
    requirement = models.CharField(max_length=64)
    units = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True, db_index=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_category = models.CharField(max_length=64, blank=True)
