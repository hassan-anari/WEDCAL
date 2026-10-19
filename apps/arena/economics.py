"""Explicit Gate B preparation. No task, migration, setting or policy document activates it."""

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Subquery, Sum
from django.utils import timezone

from apps.arena.eligibility import activity_route, trusted_observations
from apps.arena.models import (
    EconomicDeferral,
    EconomicDeferralResolution,
    EconomicEpoch,
    EconomicPolicyVersion,
    EconomicRecognition,
    EconomicReviewDecision,
    EpochPolicyBinding,
    EpochSourceManifest,
    PerformanceSourceFact,
    QualificationDecision,
)
from apps.arena.policy import CATEGORIES, EconomicPolicy, digest, parse_policy
from apps.arena.qualification import record_prepared_decisions
from apps.arena.recognition import (
    CORE_FACT_SOURCES,
    bound_policy,
    defer_source,
    record_clan_contribution,
    record_core_fact,
    record_creator_window,
    record_daily_claim,
    record_recognition,
)
from apps.arena.scoring import aggregate_recognitions, creator_scores, empty_inputs, player_scores
from apps.players.models import Player
from apps.seasons.barrier import acquire_close_barrier
from apps.seasons.models import Season


def require_permission(actor: User, permission: str) -> None:
    if not actor.is_active or not actor.has_perm(permission):
        raise PermissionDenied("Economic operation permission required")


@transaction.atomic
def bind_season_policy(
    season: Season, policy: EconomicPolicyVersion, *, actor: User, evidence_reference: str
) -> list[EpochPolicyBinding]:
    """Explicit future operator action; no API or scheduled task exposes this operation."""
    require_permission(actor, "arena.bind_economic_policy")
    if not evidence_reference.strip() or not policy.approval_reference.strip():
        raise ValidationError("Independent approval and historical evidence references required")
    parsed = parse_policy(policy.payload)
    from apps.arena.configuration import validate_policy_compatibility

    validate_policy_compatibility(season.baseline_config, policy, parsed)
    if policy.checksum != digest(policy.payload):
        raise ValidationError("ECONOMIC_POLICY_CHECKSUM_MISMATCH")
    acquire_close_barrier(season.pk)
    from apps.arena.epochs import initialize_epochs

    rows = initialize_epochs(season)
    if any(row.state not in {"OPEN", "DRAINING"} for row in rows):
        raise ValidationError("Cannot bind a new policy to finalized history")
    existing = list(EpochPolicyBinding.objects.filter(epoch__season=season))
    if existing:
        if len(existing) != len(rows) or any(
            row.policy_id != policy.pk or row.evidence_reference != evidence_reference
            for row in existing
        ):
            raise ValidationError("Economic policy binding conflict")
        return existing
    return [
        EpochPolicyBinding.objects.create(
            epoch=row, policy=policy, approved_by=actor, evidence_reference=evidence_reference
        )
        for row in rows
    ]


def _recognized(epoch: EconomicEpoch) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in (
            EconomicRecognition.objects.filter(epoch=epoch)
            .order_by("pk")
            .values(
                "id",
                "player_id",
                "actor_id",
                "source_type",
                "source_id",
                "source_time",
                "finalized_at",
                "recognized_at",
                "component",
                "category",
                "inputs",
                "evidence_digest",
                "policy_id",
            )
        )
    ]


def qualification_outcomes(
    epoch: EconomicEpoch,
    policy: EconomicPolicy,
    *,
    extra_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[int, tuple[str, str]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    records = _recognized(epoch) + (extra_records or [])
    people, creators = aggregate_recognitions(records, epoch.starts_at, policy)
    referenced = set(people) | set(
        EconomicDeferral.objects.filter(epoch=epoch, player_id__isnull=False).values_list(
            "player_id", flat=True
        )
    )
    # Include accounts materialized after cutoff if authoritative source identities refer to them.
    from apps.moderation.models import ModerationAction

    actions = ModerationAction.objects.filter(target_id=OuterRef("pk"))
    players = list(
        Player.objects.filter(Q(created_at__lt=epoch.ends_at) | Q(pk__in=referenced))
        .annotate(
            cutoff_moderation=Subquery(
                actions.filter(created_at__lte=epoch.ends_at)
                .order_by("-created_at", "-pk")
                .values("resulting_state")[:1]
            ),
            has_moderation_audit=Exists(actions),
        )
        .only("id", "first_authenticated_at", "first_mini_app_at", "moderation_state")
        .order_by("pk")
    )
    observed = trusted_observations(players)
    reviews = {r.player_id: r.outcome for r in EconomicReviewDecision.objects.filter(epoch=epoch)}
    from apps.arena.readiness import policy_identity

    key, version = policy_identity(epoch)
    prior = {
        r.player_id: r
        for r in QualificationDecision.objects.filter(
            policy_key=key, policy_version=version, effective_at__lte=epoch.ends_at
        )
        .exclude(source="S1_FINALIZED_V1")
        .order_by("player_id", "-effective_at", "-pk")
        .distinct("player_id")
    }
    outcomes = {}
    inactive_inputs = empty_inputs(policy)
    for player in players:
        raw = people.get(player.pk, inactive_inputs)
        action = player.cutoff_moderation
        state = (
            action.get("moderation_state", "ACTIVE")
            if action is not None
            else "ACTIVE"
            if player.has_moderation_audit
            else player.moderation_state
        )
        external = prior.get(player.pk)
        if (
            state in {"BANNED", "SUSPENDED"}
            or reviews.get(player.pk) == "DISQUALIFIED"
            or (external and external.decision == "DISQUALIFIED")
        ):
            outcome = ("DISQUALIFIED", "MODERATION_OR_REVIEW_DISQUALIFIED")
        elif (state == "FLAGGED" or (external and external.decision == "PENDING")) and reviews.get(
            player.pk
        ) != "CLEARED":
            outcome = ("PENDING", "INDIVIDUAL_REVIEW")
        elif player.pk not in observed or observed[player.pk] > epoch.ends_at - timedelta(
            hours=policy.number("min_age_hours")
        ):
            outcome = ("NOT_YET_QUALIFIED", "TRUSTED_AGE_NOT_ESTABLISHED")
        elif not activity_route(
            raw,
            meaningful_days=policy.number("meaningful_days"),
            daily_periods=policy.number("daily_only_periods"),
        ):
            outcome = ("NOT_YET_QUALIFIED", "MEANINGFUL_ACTIVITY_NOT_ESTABLISHED")
        else:
            outcome = ("QUALIFIED", "FINALIZED_PARTICIPATION")
        outcomes[player.pk] = outcome
    return outcomes, people, creators


def _relevant_ingress(epoch: EconomicEpoch) -> list[Any]:
    from apps.telegram.models import TelegramInboundUpdate

    rows = []
    for inbound in TelegramInboundUpdate.objects.exclude(state__in=["PROCESSED", "IGNORED"]):
        message = inbound.payload.get("message") if isinstance(inbound.payload, dict) else None
        date = message.get("date") if isinstance(message, dict) else None
        if (
            inbound.webhook_authenticated_at is not None
            and type(date) is int
            and datetime.fromtimestamp(date, UTC) >= epoch.ends_at
        ):
            continue
        rows.append(inbound)
    return rows


def _inventory(epoch: EconomicEpoch, *, compact: bool = False) -> dict[str, Any]:
    """A content-free source census. Claim and purge fields cannot change earned performance."""
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.clans.models import ClanActivity, ClanContributionEntry, ClanMissionSignal
    from apps.daily.models import DailyClaim
    from apps.missions.models import PlayerMission
    from apps.network.models import CreatorWindowSettlement, NetworkActivity
    from apps.referrals.models import ReferralRelation

    def inventory(rows: Iterable[Any]) -> Any:
        return _seal_rows(rows) if compact else list(rows)

    return {
        "legacy_tap_pending": inventory(
            BaseTapDailyAccumulator.objects.filter(season_id=epoch.season_id, pending_points__gt=0)
            .order_by("pk")
            .values_list("pk", "player_id", "first_accepted_at", "pending_points")
        ),
        "facts": inventory(
            PerformanceSourceFact.objects.filter(
                season_id=epoch.season_id,
                source_time__lt=epoch.ends_at,
                source_type__in=CORE_FACT_SOURCES | {"REFERRAL_ACTIVATION"},
            )
            .order_by("pk")
            .values_list("pk", "source_type", "source_id", "amount", "config_version_id")
        ),
        "daily": inventory(
            DailyClaim.objects.filter(season_id=epoch.season_id, claimed_at__lt=epoch.ends_at)
            .order_by("pk")
            .values_list("pk", "claim_period", "reward_ledger_id")
        ),
        "missions": inventory(
            PlayerMission.objects.filter(season_id=epoch.season_id, verified_at__lt=epoch.ends_at)
            .order_by("pk")
            .values_list("pk", "definition_id", "config_version_id", "reward_amount")
        ),
        "clan": inventory(
            ClanContributionEntry.objects.filter(
                membership__season_id=epoch.season_id, occurred_at__lt=epoch.ends_at
            )
            .order_by("pk")
            .values_list("pk", "settlement_id", "amount", "gross_basis")
        ),
        "clan_activity": inventory(
            ClanActivity.objects.filter(season_id=epoch.season_id, observed_at__lt=epoch.ends_at)
            .order_by("pk")
            .values_list(
                "pk", "sender_id", "window_processed", "classification", "rewardable_chars"
            )
        ),
        "creator": inventory(
            CreatorWindowSettlement.objects.filter(
                season_id=epoch.season_id, window_start__lt=epoch.ends_at
            )
            .order_by("pk")
            .values_list("pk", "owner_id", "state", "finalized_at")
        ),
        "network_activity": inventory(
            NetworkActivity.objects.filter(season_id=epoch.season_id, observed_at__lt=epoch.ends_at)
            .order_by("pk")
            .values_list(
                "pk",
                "sender_id",
                "window_processed",
                "classification",
                "rewardable_chars",
                "program_active",
            )
        ),
        "referrals": inventory(
            ReferralRelation.objects.filter(
                verified_at__gte=epoch.starts_at, verified_at__lt=epoch.ends_at, status="VERIFIED"
            )
            .order_by("pk")
            .values_list("pk", "inviter_id", "invitee_id", "verified_config_id")
        ),
        "pending_ingress": [
            (row.pk, row.update_id, row.state, row.webhook_authenticated_at)
            for row in _relevant_ingress(epoch)
        ],
        "pending_clan_signals": inventory(
            ClanMissionSignal.objects.filter(
                membership__season_id=epoch.season_id,
                occurred_at__lt=epoch.ends_at,
                processed_at__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", "membership_id", "requires_review")
        ),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def scoped_work(epoch: EconomicEpoch, *, persist: bool = False) -> list[str]:
    """Only unscopable impact is global. Deferred rows are never declared processed."""
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.clans.models import ClanActivity, ClanActivityWindow, ClanMissionSignal
    from apps.network.models import CreatorWindowSettlement, NetworkActivity

    global_reasons = []
    for accumulator in BaseTapDailyAccumulator.objects.filter(
        season_id=epoch.season_id, pending_points__gt=0
    ):
        earned = (
            PerformanceSourceFact.objects.filter(
                season_id=epoch.season_id,
                player_id=accumulator.player_id,
                source_type="BASE_TAP_BATCH",
                metadata__utc_day=str(accumulator.utc_day),
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        if earned == accumulator.pending_points + accumulator.settled_points:
            continue  # Accepted facts already finalized performance; XP drain is independent.
        if accumulator.first_accepted_at is None:
            global_reasons.append("LEGACY_TAP_SOURCE_TIME_UNKNOWN")
        elif accumulator.first_accepted_at < epoch.ends_at:
            if persist:
                defer_source(
                    epoch,
                    "BASE_TAP_ACCUMULATOR",
                    str(accumulator.pk),
                    reason="LEGACY_TAP_NOT_SETTLED",
                    player_id=accumulator.player_id,
                    category=CATEGORIES[1],
                    evidence={"pending_points": accumulator.pending_points},
                )
            elif not EconomicDeferral.objects.filter(
                epoch=epoch, provider="BASE_TAP_ACCUMULATOR", source_id=str(accumulator.pk)
            ).exists():
                global_reasons.append("SCOPED_SOURCE_DISPOSITION_MISSING")
    for inbound in _relevant_ingress(epoch):
        payload = inbound.payload
        message = payload.get("message") if isinstance(payload, dict) else None
        sender = message.get("from") if isinstance(message, dict) else None
        player_id = (
            Player.objects.filter(telegram_user_id=cast(int, sender.get("id")))
            .values_list("pk", flat=True)
            .first()
            if isinstance(sender, dict) and isinstance(sender.get("id"), int)
            else None
        )
        if inbound.webhook_authenticated_at is None or player_id is None:
            global_reasons.append("UNSCOPED_TELEGRAM_INGRESS")
        elif (
            not persist
            and not EconomicDeferral.objects.filter(
                epoch=epoch, provider="TELEGRAM_INBOUND", source_id=str(inbound.pk)
            ).exists()
        ):
            global_reasons.append("SCOPED_INGRESS_DISPOSITION_MISSING")
        elif persist:
            defer_source(
                epoch,
                "TELEGRAM_INBOUND",
                str(inbound.pk),
                reason="UNFINISHED_AUTHENTICATED_USER_WORK",
                player_id=player_id,
                evidence={"update_id": inbound.update_id, "state": inbound.state},
            )
    checks = (
        (
            "NETWORK_ACTIVITY",
            NetworkActivity.objects.filter(
                season_id=epoch.season_id, observed_at__lt=epoch.ends_at, window_processed=False
            ),
            "sender_id",
        ),
        (
            "CLAN_ACTIVITY",
            ClanActivity.objects.filter(
                season_id=epoch.season_id, observed_at__lt=epoch.ends_at, window_processed=False
            ),
            "sender_id",
        ),
        (
            "CLAN_MISSION_SIGNAL",
            ClanMissionSignal.objects.filter(
                membership__season_id=epoch.season_id,
                occurred_at__lt=epoch.ends_at,
                processed_at__isnull=True,
            ),
            "membership__player_id",
        ),
        (
            "CREATOR_WINDOW",
            CreatorWindowSettlement.objects.filter(
                season_id=epoch.season_id, window_start__lt=epoch.ends_at, finalized_at__isnull=True
            ),
            "registration__creator_id",
        ),
        (
            "CLAN_WINDOW",
            ClanActivityWindow.objects.filter(
                season_id=epoch.season_id, window_start__lt=epoch.ends_at, finalized_at__isnull=True
            ),
            "membership__player_id",
        ),
    )
    for provider, rows, field in checks:
        for source_id, player_id in rows.values_list("pk", field).iterator(chunk_size=1000):
            if player_id is None:
                global_reasons.append(f"UNSCOPED_{provider}")
            elif (
                not persist
                and not EconomicDeferral.objects.filter(
                    epoch=epoch, provider=provider, source_id=str(source_id)
                ).exists()
            ):
                global_reasons.append("SCOPED_SOURCE_DISPOSITION_MISSING")
            elif persist:
                defer_source(
                    epoch, provider, str(source_id), reason="NOT_FINALIZED", player_id=player_id
                )
    return sorted(set(global_reasons))


def _resolve_finished_work(epoch: EconomicEpoch) -> None:
    """Resolve a raw work case only after an explicit economic handoff or rejection."""
    from apps.clans.models import ClanActivity
    from apps.network.models import NetworkActivity
    from apps.telegram.models import TelegramInboundUpdate

    for case in EconomicDeferral.objects.filter(
        epoch__season_id=epoch.season_id, resolution__isnull=True
    ):
        source: Any = None
        if case.provider == "NETWORK_ACTIVITY":
            source = NetworkActivity.objects.filter(pk=case.source_id).first()
            target_type, target_id = "CREATOR_ACTIVITY", case.source_id
        elif case.provider == "CLAN_ACTIVITY":
            source = ClanActivity.objects.filter(pk=case.source_id).first()
            target_type, target_id = "COMMUNITY_ACTIVITY", f"clan:{case.source_id}"
        elif case.provider == "TELEGRAM_INBOUND":
            inbound = TelegramInboundUpdate.objects.filter(pk=case.source_id).first()
            if inbound and inbound.state == "IGNORED":
                EconomicDeferralResolution.objects.create(
                    deferral=case,
                    outcome="NON_ECONOMIC_INGRESS",
                    service_actor="S1_FINALIZED_V1",
                    evidence_reference=f"telegram:{inbound.pk}:IGNORED",
                )
            # PROCESSED alone does not prove normalization or economic disposition.
            continue
        else:
            continue  # Window/signal cases need an explicit reviewed disposition.
        if source is None or not source.window_processed:
            continue
        recognition = EconomicRecognition.objects.filter(
            source_type=target_type, source_id=target_id
        ).first()
        child = EconomicDeferral.objects.filter(
            epoch__season_id=epoch.season_id, provider=target_type, source_id=target_id
        ).first()
        if recognition or child or source.classification == "REJECTED":
            EconomicDeferralResolution.objects.create(
                deferral=case,
                outcome="ECONOMIC_HANDOFF" if recognition or child else "SOURCE_REJECTED",
                service_actor="S1_FINALIZED_V1",
                recognition=recognition,
                evidence_reference=(
                    f"recognition:{recognition.pk}"
                    if recognition
                    else f"deferral:{child.pk}"
                    if child
                    else f"{case.provider}:{case.source_id}:REJECTED"
                ),
            )


@transaction.atomic
def resolve_nonperformance_case(
    case: EconomicDeferral, *, actor: User, reason: str, evidence_reference: str
) -> EconomicDeferralResolution:
    """Audited rejection/nonperformance only; this cannot award or extinguish vested DLX."""
    require_permission(actor, "arena.resolve_economic_deferral")
    acquire_close_barrier(case.epoch.season_id)
    case = EconomicDeferral.objects.select_for_update().get(pk=case.pk)
    if not reason.strip() or not evidence_reference.strip():
        raise ValidationError("Reviewed nonperformance evidence required")
    if case.category or hasattr(case, "assessment") or hasattr(case, "correction_entitlement"):
        raise ValidationError("Economic beneficiary cases require the funded correction path")
    row, created = EconomicDeferralResolution.objects.get_or_create(
        deferral=case,
        defaults={
            "outcome": "REVIEWED_NONPERFORMANCE",
            "actor": actor,
            "evidence_reference": f"{reason}: {evidence_reference}",
        },
    )
    if not created and (
        row.actor_id != actor.pk or row.evidence_reference != f"{reason}: {evidence_reference}"
    ):
        raise ValidationError("DEFERRAL_RESOLUTION_CONFLICT")
    return row


@transaction.atomic
def refresh_deferred_sources(epoch: EconomicEpoch, *, actor: User) -> None:
    """Finalize known source evidence after Freeze, including the final Epoch closing period."""
    require_permission(actor, "arena.resolve_economic_deferral")
    acquire_close_barrier(epoch.season_id)
    synchronize_finalized_sources(epoch)
    scoped_work(epoch, persist=True)


def synchronize_finalized_sources(epoch: EconomicEpoch) -> None:
    """Historical replay is explicit and idempotent; absent proof is a scoped case."""
    from apps.clans.models import ClanActivityWindow, ClanEconomicSettlement
    from apps.daily.models import DailyClaim
    from apps.missions.models import PlayerMission
    from apps.network.models import CreatorWindowSettlement
    from apps.referrals.models import ReferralRelation
    from apps.rewards.models import RewardLedger
    from apps.typing_game.models import TypingAttempt

    known_typing = set(
        PerformanceSourceFact.objects.filter(
            source_type="TYPING_CREDIT", season_id=epoch.season_id
        ).values_list("source_id", flat=True)
    )
    for attempt in TypingAttempt.objects.filter(
        season_id=epoch.season_id, status="VALID", submitted_at__lt=epoch.ends_at
    ):
        if str(attempt.public_id) not in known_typing:
            defer_source(
                epoch,
                "TYPING_CREDIT",
                str(attempt.public_id),
                reason="HISTORICAL_CREDIT_FACT_MISSING",
                player_id=attempt.player_id,
                category=CATEGORIES[1],
                evidence={"attempt_id": attempt.pk},
            )
    known_tap = set(
        PerformanceSourceFact.objects.filter(
            source_type="BASE_TAP_V2_BATCH", season_id=epoch.season_id
        ).values_list("source_id", flat=True)
    )
    for reward in RewardLedger.objects.filter(
        season_id=epoch.season_id, source_type="BASE_TAP", occurred_at__lt=epoch.ends_at
    ):
        if reward.source_id.startswith("v2:") and str(reward.pk) not in known_tap:
            defer_source(
                epoch,
                "BASE_TAP_V2_BATCH",
                str(reward.pk),
                reason="HISTORICAL_BATCH_FACT_MISSING",
                player_id=reward.player_id,
                category=CATEGORIES[1],
                evidence={"reward_id": reward.pk},
            )
    recognized = EconomicRecognition.objects.filter(
        source_type=OuterRef("source_type"), source_id=OuterRef("source_id")
    )
    for fact in (
        PerformanceSourceFact.objects.filter(
            season_id=epoch.season_id,
            source_time__lt=epoch.ends_at,
            source_type__in=CORE_FACT_SOURCES,
        )
        .filter(~Exists(recognized))
        .iterator(chunk_size=1000)
    ):
        if (
            fact.source_type in {"BASE_TAP_BATCH", "BASE_TAP_V2_BATCH"}
            and type(fact.metadata.get("accepted_taps")) is not int
        ):
            defer_source(
                epoch,
                fact.source_type,
                fact.source_id,
                reason="HISTORICAL_TAP_COUNT_UNPROVEN",
                player_id=fact.player_id,
                category=CATEGORIES[1],
                evidence={"fact_id": fact.pk},
            )
        else:
            record_core_fact(fact)
    for claim in DailyClaim.objects.filter(
        season_id=epoch.season_id, claimed_at__lt=epoch.ends_at
    ).select_related("reward_ledger"):
        if claim.reward_ledger_id:
            record_daily_claim(claim)
        else:
            defer_source(
                epoch,
                "DAILY_CLAIM",
                str(claim.pk),
                reason="CLAIM_NOT_FINALIZED",
                player_id=claim.player_id,
                category=CATEGORIES[1],
            )
    selected = bound_policy(epoch)
    assert selected is not None
    verified = PlayerMission.objects.filter(
        season_id=epoch.season_id,
        verified_at__lt=epoch.ends_at,
        definition__key__in=selected[1].values["independent_mission_keys"],
    )
    known_missions = [
        int(value)
        for value in PerformanceSourceFact.objects.filter(
            source_type="PERSONAL_MISSION_VERIFIED"
        ).values_list("source_id", flat=True)
        if value.isdecimal()
    ]
    for mission in verified.exclude(pk__in=known_missions):
        defer_source(
            epoch,
            "PERSONAL_MISSION_VERIFIED",
            str(mission.pk),
            reason="HISTORICAL_VERIFICATION_ATTESTATION_MISSING",
            player_id=mission.player_id,
            category=CATEGORIES[1],
        )
    for settlement in ClanEconomicSettlement.objects.filter(
        clan_season__season_id=epoch.season_id, occurred_at__lt=epoch.ends_at
    ).select_related("clan_season", "membership"):
        record_clan_contribution(settlement)
    from apps.arena.recognition import record_clan_window_presence

    for window in ClanActivityWindow.objects.filter(
        season_id=epoch.season_id, window_start__lt=epoch.ends_at, finalized_at__isnull=False
    ):
        record_clan_window_presence(window)
    for creator_window in CreatorWindowSettlement.objects.filter(
        season_id=epoch.season_id, window_start__lt=epoch.ends_at, finalized_at__isnull=False
    ).select_related("registration"):
        record_creator_window(creator_window)
    for fact in PerformanceSourceFact.objects.filter(
        season_id=epoch.season_id, source_type="REFERRAL_ACTIVATION", source_time__lt=epoch.ends_at
    ):
        relation = ReferralRelation.objects.filter(
            pk=fact.source_id,
            status="VERIFIED",
            verified_at=fact.source_time,
            verified_config_id=fact.config_version_id,
        ).first()
        if relation is None:
            raise ValidationError("REFERRAL_ACTIVATION_PROVENANCE_MISMATCH")
        assert relation.verified_at is not None and relation.verified_config_id is not None
        record_recognition(
            season_id=epoch.season_id,
            player_id=relation.inviter_id,
            actor_id=relation.invitee_id,
            source_type="REFERRAL_ACTIVATION",
            source_id=str(relation.pk),
            source_time=relation.verified_at,
            finalized_at=relation.verified_at,
            config_version_id=relation.verified_config_id,
            inputs={"invitee_id": relation.invitee_id, "activation_fact_id": fact.pk},
        )
    for relation in ReferralRelation.objects.filter(
        verified_at__gte=epoch.starts_at, verified_at__lt=epoch.ends_at, status="VERIFIED"
    ).exclude(
        pk__in=[
            int(v)
            for v in PerformanceSourceFact.objects.filter(
                season_id=epoch.season_id, source_type="REFERRAL_ACTIVATION"
            ).values_list("source_id", flat=True)
            if v.isdecimal()
        ]
    ):
        defer_source(
            epoch,
            "REFERRAL_ACTIVATION",
            str(relation.pk),
            reason="HISTORICAL_ACTIVATION_ATTESTATION_MISSING",
            player_id=relation.inviter_id,
            category=CATEGORIES[3],
            evidence={"relation_id": relation.pk},
        )
    _resolve_finished_work(epoch)


def _seal_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """Ordered, length-framed digest avoids materializing a complete source inventory."""
    checksum = sha256()
    count = 0
    iterator = rows.iterator(chunk_size=1000) if hasattr(rows, "iterator") else iter(rows)
    for row in iterator:
        encoded = json.dumps(_json_safe(row), sort_keys=True, separators=(",", ":")).encode()
        checksum.update(len(encoded).to_bytes(8, "big"))
        checksum.update(encoded)
        count += 1
    return {"rows": count, "digest": checksum.hexdigest(), "encoding": "LENGTH_FRAMED_JSON_V1"}


def manifest_evidence(epoch: EconomicEpoch, policy: EconomicPolicy) -> dict[str, Any]:
    outcomes, _, _ = qualification_outcomes(epoch, policy)
    compact = policy.values["implementation"] == "CONFIGURABLE_V1"
    recognitions = (
        EconomicRecognition.objects.filter(epoch=epoch)
        .order_by("pk")
        .values_list("pk", "evidence_digest")
    )
    deferrals = (
        EconomicDeferral.objects.filter(epoch=epoch)
        .order_by("pk")
        .values_list("pk", "provider", "source_id", "reason")
    )
    evidence = {
        "implementation": policy.values["implementation"],
        "source_universe": "FINALIZED_RECOGNITIONS_PLUS_EXPLICIT_DEFERRALS",
        "inventory": _inventory(epoch, compact=compact),
        "recognitions": _seal_rows(recognitions) if compact else list(recognitions),
        "outcomes": _seal_rows(sorted(outcomes.items())) if compact else outcomes,
        "deferrals": _seal_rows(deferrals) if compact else list(deferrals),
        "global_reasons": scoped_work(epoch),
    }
    if compact:
        evidence["outcome_counts"] = {
            state: sum(value[0] == state for value in outcomes.values())
            for state in QualificationDecision.Decision.values
        }
        evidence["scope_count"] = len(outcomes)
    return cast(dict[str, Any], _json_safe(evidence))


@transaction.atomic
def prepare_epoch(epoch: EconomicEpoch) -> EpochSourceManifest:
    acquire_close_barrier(epoch.season_id)
    epoch = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
    selected = bound_policy(epoch)
    if selected is None:
        raise ValidationError("ECONOMIC_POLICY_UNBOUND")
    binding, policy = selected
    if epoch.state not in {"OPEN", "DRAINING"} or timezone.now() < epoch.ends_at:
        raise ValidationError("EPOCH_NOT_PREPARABLE")
    synchronize_finalized_sources(epoch)
    scoped_work(epoch, persist=True)
    outcomes, _, _ = qualification_outcomes(epoch, policy)
    from apps.arena.readiness import policy_identity

    key, version = policy_identity(epoch)
    prepared = []
    for player_id, (decision, reason) in outcomes.items():
        evidence = {
            "epoch_id": epoch.pk,
            "policy_id": binding.policy_id,
            "policy_checksum": binding.policy.checksum,
            "outcome": decision,
            "reason": reason,
        }
        prepared.append(
            QualificationDecision(
                player_id=player_id,
                policy_key=key,
                policy_version=version,
                decision=decision,
                effective_at=epoch.ends_at,
                reason_code=reason,
                source="S1_FINALIZED_V1",
                source_id=f"{epoch.pk}:{player_id}:{digest(evidence)}",
                evidence=evidence,
            )
        )
        if len(prepared) >= 1000:
            record_prepared_decisions(epoch, policy_key=key, policy_version=version, rows=prepared)
            prepared.clear()
        if decision == "PENDING":
            defer_source(epoch, "PLAYER_REVIEW", str(player_id), reason=reason, player_id=player_id)
    record_prepared_decisions(epoch, policy_key=key, policy_version=version, rows=prepared)
    evidence = manifest_evidence(epoch, policy)
    manifest, _ = EpochSourceManifest.objects.get_or_create(
        epoch=epoch,
        input_digest=digest(evidence),
        defaults={"policy": binding.policy, "evidence": evidence},
    )
    return manifest


def inspect_finalized_epoch(epoch: EconomicEpoch) -> Any:
    from apps.arena.readiness import EvaluationEvidence

    selected = bound_policy(epoch)
    if selected is None:
        raise ValidationError("ECONOMIC_POLICY_UNBOUND")
    binding, policy = selected
    evidence = manifest_evidence(epoch, policy)
    manifest = EpochSourceManifest.objects.filter(
        epoch=epoch, input_digest=digest(evidence), policy=binding.policy
    ).first()
    outcomes = evidence["outcomes"]
    reasons = list(evidence["global_reasons"])
    if manifest is None:
        reasons.append("FINALIZED_SOURCE_MANIFEST_MISSING_OR_STALE")
    counts = evidence.get("outcome_counts") or {
        state: sum(value[0] == state for value in outcomes.values())
        for state in QualificationDecision.Decision.values
    }
    scope_count = int(evidence.get("scope_count", len(outcomes)))
    return EvaluationEvidence(
        scope_count=scope_count,
        evaluated_count=scope_count,
        unresolved_count=0,
        scoped_pending_count=counts["PENDING"],
        source_completion={
            "manifest_id": manifest.pk if manifest else None,
            "manifest_digest": digest(evidence),
            "coverage": "FINALIZED_PLUS_DEFERRED",
            "unprocessed_sources_claimed_complete": False,
        },
        provenance={
            "policy_id": binding.policy_id,
            "checksum": binding.policy.checksum,
            "dispositions": counts,
            "binding_evidence": binding.evidence_reference,
        },
        blocking_reasons=tuple(reasons),
    )


def build_finalized_snapshots(epoch: EconomicEpoch) -> None:
    from apps.arena.models import EpochPerformanceSnapshot
    from apps.arena.readiness import policy_identity

    selected = bound_policy(epoch)
    assert selected is not None
    _, policy = selected
    outcomes, people, events = qualification_outcomes(epoch, policy)
    qualified = {pid for pid, value in outcomes.items() if value[0] == "QUALIFIED"}
    anchored = {pid for pid, raw in people.items() if raw["anchor_days"]}
    creators = creator_scores(events, qualified, anchored, policy)
    key, version = policy_identity(epoch)
    decisions = {
        row.player_id: row
        for row in QualificationDecision.objects.filter(
            player_id__in=qualified,
            policy_key=key,
            policy_version=version,
            effective_at__lte=epoch.ends_at,
        )
        .order_by("player_id", "-effective_at", "-pk")
        .distinct("player_id")
    }
    pending = []
    for pid in sorted(qualified):
        decision = decisions.get(pid)
        if decision is None or decision.decision != "QUALIFIED":
            raise ValidationError("QUALIFICATION_PREPARATION_STALE")
        for category, (units, raw) in player_scores(
            people[pid], creators.get(pid, {}), policy
        ).items():
            pending.append(
                EpochPerformanceSnapshot(
                    epoch=epoch,
                    category=category,
                    player_id=pid,
                    raw_inputs=raw,
                    performance_units=units,
                    qualification_decision=decision,
                    config_version_id=epoch.config_version_id,
                )
            )
            if len(pending) >= 1000:
                EpochPerformanceSnapshot.objects.bulk_create(pending, batch_size=1000)
                pending.clear()
    EpochPerformanceSnapshot.objects.bulk_create(pending, batch_size=1000)


@transaction.atomic
def review_player(
    epoch: EconomicEpoch,
    player: Player,
    *,
    outcome: str,
    actor: User,
    reason: str,
    evidence_reference: str,
) -> EconomicReviewDecision:
    require_permission(actor, "arena.review_economic_eligibility")
    if (
        outcome not in {"CLEARED", "DISQUALIFIED"}
        or not reason.strip()
        or not evidence_reference.strip()
    ):
        raise ValidationError("Reviewed disposition and evidence required")
    acquire_close_barrier(epoch.season_id)
    epoch = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
    decision, created = EconomicReviewDecision.objects.get_or_create(
        epoch=epoch,
        player=player,
        defaults={
            "outcome": outcome,
            "actor": actor,
            "reason": reason,
            "evidence_reference": evidence_reference,
        },
    )
    if not created and (
        decision.outcome,
        decision.actor_id,
        decision.reason,
        decision.evidence_reference,
    ) != (outcome, actor.pk, reason, evidence_reference):
        raise ValidationError("ECONOMIC_REVIEW_CONFLICT")
    for case in EconomicDeferral.objects.filter(
        epoch=epoch, provider="PLAYER_REVIEW", player=player, resolution__isnull=True
    ):
        if epoch.state in {"OPEN", "DRAINING"} or outcome == "DISQUALIFIED":
            EconomicDeferralResolution.objects.create(
                deferral=case, outcome=outcome, actor=actor, evidence_reference=evidence_reference
            )
        else:
            selected = bound_policy(epoch)
            assert selected is not None
            _, policy = selected
            outcomes, people, events = qualification_outcomes(epoch, policy)
            qualified = {pid for pid, value in outcomes.items() if value[0] == "QUALIFIED"}
            anchored = {pid for pid, raw in people.items() if raw["anchor_days"]}
            creators = creator_scores(events, qualified, anchored, policy)
            for category, (units, raw) in player_scores(
                people[player.pk], creators.get(player.pk, {}), policy
            ).items():
                if units >= policy.number("minimum_category_score"):
                    defer_source(
                        epoch,
                        "PLAYER_REVIEW",
                        f"{player.pk}:{category}",
                        reason="REVIEW_CLEARED_CORRECTION_REQUIRED",
                        player_id=player.pk,
                        category=category,
                        evidence={
                            "review_id": decision.pk,
                            "reviewed_performance_units": units,
                            "creator_h_units": raw.get("h_units", 0),
                        },
                    )
            EconomicDeferralResolution.objects.create(
                deferral=case,
                outcome="REVIEW_CLEARED_REFERRED",
                actor=actor,
                evidence_reference=evidence_reference,
            )
    return decision
