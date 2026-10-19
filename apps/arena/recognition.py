"""Explicit source allowlists and append-only recognition; never convert RewardLedger XP."""

from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.arena.models import (
    CreatorContentClass,
    EconomicDeferral,
    EconomicDeferralEvidence,
    EconomicDeferralResolution,
    EconomicEpoch,
    EconomicRecognition,
    EpochPolicyBinding,
    PerformanceSourceFact,
)
from apps.arena.policy import SOURCE_COMPONENTS, EconomicPolicy, digest, parse_policy
from apps.game_config.services import resolve_game_config_version

INDEPENDENT_MISSIONS = frozenset(
    {"visit-dalexio", "view-x-update", "join-telegram", "read-season-brief"}
)
TAP_SOURCES = frozenset({"BASE_TAP_BATCH", "BASE_TAP_V2_BATCH"})
CORE_FACT_SOURCES = TAP_SOURCES | {"TYPING_CREDIT", "PERSONAL_MISSION_VERIFIED"}


def bound_policy(epoch: EconomicEpoch) -> tuple[EpochPolicyBinding, EconomicPolicy] | None:
    binding = EpochPolicyBinding.objects.select_related("policy").filter(epoch=epoch).first()
    if binding is None:
        return None
    if binding.policy.checksum != digest(binding.policy.payload):
        raise ValidationError("ECONOMIC_POLICY_CHECKSUM_MISMATCH")
    return binding, parse_policy(binding.policy.payload)


def season_has_policy(season_id: int) -> bool:
    return EpochPolicyBinding.objects.filter(epoch__season_id=season_id).exists()


@transaction.atomic
def defer_source(
    epoch: EconomicEpoch,
    provider: str,
    source_id: str,
    *,
    reason: str,
    player_id: int | None = None,
    category: str = "",
    evidence: dict[str, Any] | None = None,
) -> EconomicDeferral:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(epoch.season_id)
    row, _ = EconomicDeferral.objects.get_or_create(
        epoch=epoch,
        provider=provider,
        source_id=source_id,
        defaults={
            "player_id": player_id,
            "category": category,
            "reason": reason,
            "evidence": evidence or {},
        },
    )
    if evidence and "inputs" in evidence and "evidence_digest" in evidence:
        EconomicDeferralEvidence.objects.get_or_create(
            deferral=row,
            evidence_digest=evidence["evidence_digest"],
            defaults={"payload": evidence},
        )
    return row


def record_recognition(
    *,
    season_id: int,
    player_id: int,
    actor_id: int,
    source_type: str,
    source_id: str,
    source_time: datetime,
    finalized_at: datetime,
    config_version_id: int,
    inputs: dict[str, Any],
    delayed: bool = False,
) -> EconomicRecognition | None:
    if not connection.in_atomic_block:
        raise RuntimeError("Recognition must share the canonical source transaction")
    if source_type not in SOURCE_COMPONENTS:
        raise ValidationError("SOURCE_NOT_ALLOWLISTED")
    if not season_has_policy(season_id):
        return None
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(season_id)
    category, component = SOURCE_COMPONENTS[source_type]
    evidence_digest = digest(
        {
            "season": season_id,
            "player": player_id,
            "actor": actor_id,
            "source_time": source_time.isoformat(),
            "config": config_version_id,
            "inputs": inputs,
            "category": category,
        }
    )
    existing = EconomicRecognition.objects.filter(
        source_type=source_type, source_id=source_id
    ).first()
    if existing:
        if existing.evidence_digest != evidence_digest:
            raise ValidationError("ECONOMIC_SOURCE_IDEMPOTENCY_CONFLICT")
        return existing
    source_epoch = EconomicEpoch.objects.filter(
        season_id=season_id, starts_at__lte=source_time, ends_at__gt=source_time
    ).first()
    if source_epoch is None:
        raise ValidationError("SOURCE_OUTSIDE_SIX_EPOCHS")
    recognized_at = source_time
    target: EconomicEpoch | None = source_epoch
    if delayed and finalized_at >= source_epoch.ends_at:
        target = EconomicEpoch.objects.filter(
            season_id=season_id, starts_at__lte=finalized_at, ends_at__gt=finalized_at
        ).first()
        recognized_at = finalized_at
    if target is None or target.state not in {"OPEN", "DRAINING"}:
        target = target or source_epoch
        defer_source(
            target,
            source_type,
            source_id,
            reason="FUNDED_CORRECTION_REQUIRED",
            player_id=player_id,
            category=category,
            evidence={
                "source_time": source_time.isoformat(),
                "finalized_at": finalized_at.isoformat(),
                "config_version_id": config_version_id,
                "actor_id": actor_id,
                "inputs": inputs,
                "evidence_digest": evidence_digest,
            },
        )
        return None
    selected = bound_policy(target)
    if selected is None:
        raise ValidationError("RECOGNIZED_EPOCH_POLICY_UNBOUND")
    binding, _ = selected
    record = EconomicRecognition.objects.create(
        epoch=target,
        player_id=player_id,
        actor_id=actor_id,
        category=category,
        component=component,
        source_type=source_type,
        source_id=source_id,
        source_time=source_time,
        finalized_at=finalized_at,
        recognized_at=recognized_at,
        config_version_id=config_version_id,
        policy=binding.policy,
        inputs=inputs,
        evidence_digest=evidence_digest,
    )
    # Resolution says recognition occurred, never that every other producer task completed.
    for case in EconomicDeferral.objects.filter(
        epoch__season_id=season_id,
        provider=source_type,
        source_id=source_id,
        resolution__isnull=True,
    ):
        EconomicDeferralResolution.objects.create(
            deferral=case,
            outcome="RECOGNIZED",
            service_actor="S1_FINALIZED_V1",
            evidence_reference=evidence_digest,
            recognition=record,
        )
    return record


def record_core_fact(fact: PerformanceSourceFact) -> EconomicRecognition | None:
    if fact.source_type not in CORE_FACT_SOURCES:
        return None  # In particular TYPING_REFERRAL_CREDIT is passive provenance, not acquisition.
    if not season_has_policy(fact.season_id):
        return None
    if fact.category != "CORE_GAMEPLAY":
        raise ValidationError("CORE_FACT_CATEGORY_MISMATCH")
    config = resolve_game_config_version(fact.config_version_id)
    inputs: dict[str, Any] = {"amount": fact.amount}
    if fact.source_type in TAP_SOURCES:
        accepted = fact.metadata.get("accepted_taps")
        if (
            type(accepted) is not int
            or accepted <= 0
            or accepted * config.base_tap.points_per_rewarded_tap != fact.amount
        ):
            raise ValidationError("TAP_ACCEPTED_COUNT_UNPROVEN")
        if fact.source_type == "BASE_TAP_V2_BATCH":
            from apps.rewards.models import RewardLedger

            reward = RewardLedger.objects.filter(
                pk=fact.source_id,
                player_id=fact.player_id,
                season_id=fact.season_id,
                config_version_id=fact.config_version_id,
                source_type="BASE_TAP",
                occurred_at=fact.source_time,
                player_amount=fact.amount,
            ).first()
            if reward is None:
                raise ValidationError("TAP_CANONICAL_LEDGER_MISSING")
        inputs["accepted_taps"] = accepted
    elif fact.source_type == "TYPING_CREDIT":
        from apps.typing_game.models import TypingWalletTransaction

        credit = (
            TypingWalletTransaction.objects.select_related("attempt")
            .filter(
                attempt__public_id=fact.source_id,
                wallet__player_id=fact.player_id,
                kind="CREDIT",
                amount=fact.amount,
                season_id=fact.season_id,
                config_version_id=fact.config_version_id,
            )
            .first()
        )
        if (
            credit is None
            or credit.attempt is None
            or credit.attempt.status != "VALID"
            or credit.attempt.submitted_at != fact.source_time
        ):
            raise ValidationError("TYPING_CREDIT_UNPROVEN")
        # New facts seal quality; historical facts require corroboration before a policy binding.
        wpm = fact.metadata.get("wpm", credit.attempt.wpm)
        accuracy = fact.metadata.get("accuracy_bps", credit.attempt.accuracy_bps)
        if type(wpm) is not int or type(accuracy) is not int or not 0 <= accuracy <= 10000:
            raise ValidationError("TYPING_QUALITY_UNPROVEN")
        target = EconomicEpoch.objects.get(
            season_id=fact.season_id, starts_at__lte=fact.source_time, ends_at__gt=fact.source_time
        )
        selected = bound_policy(target)
        if selected is None:
            raise ValidationError("RECOGNIZED_EPOCH_POLICY_UNBOUND")
        ceiling = selected[1].number("typing_wpm_ceiling")
        inputs["quality"] = min(wpm, ceiling) * accuracy // 10000
        inputs["credit_id"] = credit.pk
    else:
        key = fact.metadata.get("mission_key")
        if key not in INDEPENDENT_MISSIONS or not fact.metadata.get("compatible_at_verification"):
            return None
        target = EconomicEpoch.objects.get(
            season_id=fact.season_id, starts_at__lte=fact.source_time, ends_at__gt=fact.source_time
        )
        selected = bound_policy(target)
        if selected is None or key not in selected[1].values["independent_mission_keys"]:
            return None
        inputs["definition_identity"] = fact.metadata["definition_identity"]
        inputs["verification_family"] = fact.metadata["verification_family"]
    return record_recognition(
        season_id=fact.season_id,
        player_id=fact.player_id,
        actor_id=fact.player_id,
        source_type=fact.source_type,
        source_id=fact.source_id,
        source_time=fact.source_time,
        finalized_at=fact.source_time,
        config_version_id=fact.config_version_id,
        inputs=inputs,
    )


def record_mission_verification(progress: Any) -> None:
    from apps.missions.services import _require_policy_binding, require_approved_destination

    definition = progress.definition
    if definition.key not in INDEPENDENT_MISSIONS:
        return
    config = resolve_game_config_version(progress.config_version_id)
    if not config.missions.enabled or not definition.enabled or not definition.activated_at:
        raise ValidationError("MISSION_NOT_ACTIVATED")
    _require_policy_binding(definition, config)
    require_approved_destination(definition)
    at = progress.verified_at
    if at is not None and definition.activated_at > at:
        return  # Preserve historical XP; do not invent a pre-activation performance attestation.
    if (
        at is None
        or (definition.available_from and at < definition.available_from)
        or (definition.available_until and at >= definition.available_until)
    ):
        raise ValidationError("MISSION_OUTSIDE_ACTIVE_INTERVAL")
    fact, _ = PerformanceSourceFact.objects.get_or_create(
        source_type="PERSONAL_MISSION_VERIFIED",
        source_id=str(progress.pk),
        defaults={
            "player_id": progress.player_id,
            "season_id": progress.season_id,
            "category": "CORE_GAMEPLAY",
            "source_time": at,
            "amount": progress.reward_amount,
            "config_version_id": progress.config_version_id,
            "metadata": {
                "mission_key": definition.key,
                "definition_identity": f"{definition.key}:v{definition.version}",
                "verification_family": definition.verification_family,
                "compatible_at_verification": True,
                "verification_scope": "SERVER_STRATEGY_NOT_READING_OR_HUMAN_IDENTITY",
                "definition_id": definition.pk,
            },
        },
    )
    record_core_fact(fact)


def record_daily_claim(claim: Any) -> None:
    ledger = claim.reward_ledger
    if ledger is None or (
        ledger.player_id,
        ledger.season_id,
        ledger.config_version_id,
        ledger.source_type,
        ledger.occurred_at,
        ledger.player_amount,
    ) != (
        claim.player_id,
        claim.season_id,
        claim.config_version_id,
        "DAILY",
        claim.claimed_at,
        claim.reward_amount,
    ):
        raise ValidationError("DAILY_CLAIM_LEDGER_MISMATCH")
    record_recognition(
        season_id=claim.season_id,
        player_id=claim.player_id,
        actor_id=claim.player_id,
        source_type="DAILY_CLAIM",
        source_id=str(claim.pk),
        source_time=claim.claimed_at,
        finalized_at=claim.claimed_at,
        config_version_id=claim.config_version_id,
        inputs={"claim_period": claim.claim_period, "claim_id": claim.pk},
    )


def record_clan_contribution(settlement: Any) -> None:
    from apps.clans.models import ClanContributionEntry

    if settlement.source_type not in {"CLAN_ACTIVITY", "CLAN_MISSION"}:
        raise ValidationError("CLAN_SOURCE_NOT_ALLOWLISTED")
    entry = ClanContributionEntry.objects.filter(settlement=settlement).first()
    ledger = settlement.reward
    if (
        entry is None
        or (
            entry.membership_id,
            entry.clan_season_id,
            entry.amount,
            entry.gross_basis,
            entry.config_version_id,
            entry.occurred_at,
        )
        != (
            settlement.membership_id,
            settlement.clan_season_id,
            settlement.contribution_amount,
            settlement.gross_amount,
            settlement.config_version_id,
            settlement.occurred_at,
        )
        or ledger is None
        or (
            ledger.player_id,
            ledger.season_id,
            ledger.config_version_id,
            ledger.source_type,
            ledger.source_id,
            ledger.occurred_at,
            ledger.gross_amount,
            ledger.player_amount,
            ledger.clan_treasury_amount,
        )
        != (
            settlement.membership.player_id,
            settlement.clan_season.season_id,
            settlement.config_version_id,
            settlement.source_type,
            settlement.source_id,
            settlement.occurred_at,
            settlement.gross_amount,
            settlement.player_amount,
            settlement.treasury_amount,
        )
    ):
        raise ValidationError("CLAN_CONTRIBUTION_NOT_RECONCILED")
    if settlement.source_type == "CLAN_MISSION" and season_has_policy(
        settlement.clan_season.season_id
    ):
        from apps.clans.models import ClanMissionParticipant

        participant = ClanMissionParticipant.objects.filter(
            reward_settlement=settlement,
            reward_eligible=True,
            eligibility_evaluated_at=settlement.occurred_at,
            progress__completed_at=settlement.occurred_at,
            progress__definition_id=settlement.metadata.get("definition_id"),
        ).first()
        if participant is None:
            epoch = EconomicEpoch.objects.get(
                season_id=settlement.clan_season.season_id,
                starts_at__lte=settlement.occurred_at,
                ends_at__gt=settlement.occurred_at,
            )
            defer_source(
                epoch,
                "CLAN_CONTRIBUTION",
                str(entry.pk),
                reason="MISSION_CONTRIBUTION_PROVENANCE_UNRESOLVED",
                player_id=settlement.membership.player_id,
                category="CLAN_PERFORMANCE",
            )
            return
    record_recognition(
        season_id=settlement.clan_season.season_id,
        player_id=settlement.membership.player_id,
        actor_id=settlement.membership.player_id,
        source_type="CLAN_CONTRIBUTION",
        source_id=str(entry.pk),
        source_time=entry.occurred_at,
        finalized_at=settlement.created_at,
        config_version_id=entry.config_version_id,
        inputs={
            "contribution": entry.amount,
            "settlement_id": settlement.pk,
            "canonical_source_type": settlement.source_type,
            "contribution_effect_ids": settlement.metadata.get("contribution_effect_ids", []),
        },
        delayed=True,
    )


def record_referral_activation(relation: Any, activation_evidence: dict[str, Any]) -> None:
    if relation.status != "VERIFIED" or not relation.verified_at or not relation.verified_config_id:
        raise ValidationError("REFERRAL_NOT_VERIFIED")
    fact, _ = PerformanceSourceFact.objects.get_or_create(
        source_type="REFERRAL_ACTIVATION",
        source_id=str(relation.pk),
        defaults={
            "season_id": activation_evidence["season_id"],
            "player_id": relation.inviter_id,
            "category": "VERIFIED_REFERRAL",
            "source_time": relation.verified_at,
            "amount": 1,
            "config_version_id": relation.verified_config_id,
            "metadata": {
                **activation_evidence,
                "invitee_id": relation.invitee_id,
                "attributed_at": relation.attributed_at.isoformat(),
                "verified_at": relation.verified_at.isoformat(),
            },
        },
    )
    record_recognition(
        season_id=fact.season_id,
        player_id=relation.inviter_id,
        actor_id=relation.invitee_id,
        source_type="REFERRAL_ACTIVATION",
        source_id=str(relation.pk),
        source_time=relation.verified_at,
        finalized_at=relation.verified_at,
        config_version_id=relation.verified_config_id,
        inputs={"invitee_id": relation.invitee_id, "activation_fact_id": fact.pk},
    )


def record_creator_window(window: Any) -> None:
    if not season_has_policy(window.season_id):
        return
    from apps.core.activity import window_start_for
    from apps.network.models import NetworkActivity
    from apps.network.services import registration_reward_eligible_at
    from apps.telegram.models import TelegramInboundUpdate

    config = resolve_game_config_version(window.config_version_id)
    if not window.finalized_at or window.state not in {"REWARDED", "ZERO"}:
        return
    events = NetworkActivity.objects.filter(
        registration_id=window.registration_id,
        season_id=window.season_id,
        config_version_id=window.config_version_id,
        observed_at__gte=window.window_start,
        observed_at__lt=window.window_start + timedelta(seconds=config.network.window_seconds),
        window_processed=True,
        classification="VALID",
        program_active=True,
        sender_id__isnull=False,
        rewardable_chars__gt=0,
    ).select_related("registration")
    for event in events:
        if EconomicRecognition.objects.filter(
            source_type="CREATOR_ACTIVITY", source_id=str(event.pk)
        ).exists():
            continue  # Permanent receipt survives normal content/fingerprint expiry.
        epoch = EconomicEpoch.objects.get(
            season_id=window.season_id,
            starts_at__lte=event.observed_at,
            ends_at__gt=event.observed_at,
        )
        ownership = (
            event.registration.state_events.filter(occurred_at__lte=event.observed_at)
            .order_by("-occurred_at", "-pk")
            .first()
        )
        if (
            not window.owner_id
            or ownership is None
            or ownership.owner_id != window.owner_id
            or not registration_reward_eligible_at(event.registration, event.observed_at)
            or not event.normalized_fingerprint
        ):
            defer_source(
                epoch,
                "CREATOR_ACTIVITY",
                str(event.pk),
                reason="CREATOR_HISTORICAL_EVIDENCE_MISSING",
                player_id=window.owner_id or event.registration.creator_id,
                category="CREATOR_COMMUNITY_GROWTH",
            )
            continue
        if event.sender_id == window.owner_id:
            continue
        if not TelegramInboundUpdate.objects.filter(
            update_id=event.telegram_update_id, webhook_authenticated_at__isnull=False
        ).exists():
            defer_source(
                epoch,
                "CREATOR_ACTIVITY",
                str(event.pk),
                reason="SOURCE_AUTHENTICATION_UNPROVEN",
                player_id=window.owner_id,
                category="CREATOR_COMMUNITY_GROWTH",
            )
            continue
        target = (
            EconomicEpoch.objects.filter(
                season_id=window.season_id,
                starts_at__lte=window.finalized_at,
                ends_at__gt=window.finalized_at,
            ).first()
            if window.finalized_at >= epoch.ends_at
            else epoch
        )
        # Even after Freeze retain sealed source proof for a funded correction.
        # This never inserts a normal recognition into a frozen/nonexistent Epoch.
        target = target or epoch
        selected = bound_policy(target)
        if selected is None:
            raise ValidationError("RECOGNIZED_EPOCH_POLICY_UNBOUND")
        _, policy = selected
        recognized_at = window.finalized_at if target.pk != epoch.pk else event.observed_at
        day = (recognized_at - target.starts_at).days
        if not 0 <= day < policy.number("days"):
            day = (event.observed_at - epoch.starts_at).days  # Final-week correction proof only.
        content_class, _ = CreatorContentClass.objects.get_or_create(
            epoch=target,
            owner_id=window.owner_id,
            day=day,
            fingerprint=event.normalized_fingerprint,
            defaults={"expires_at": event.fingerprint_expires_at},
        )
        assert event.sender_id is not None
        record_recognition(
            season_id=window.season_id,
            player_id=window.owner_id,
            actor_id=event.sender_id,
            source_type="CREATOR_ACTIVITY",
            source_id=str(event.pk),
            source_time=event.observed_at,
            finalized_at=window.finalized_at,
            config_version_id=event.config_version_id,
            inputs={
                "fingerprint": str(content_class.public_id),
                "characters": event.rewardable_chars,
                "source_seconds": int(event.observed_at.timestamp()),
                "window": int(
                    window_start_for(
                        event.observed_at, policy.number("creator_window_seconds")
                    ).timestamp()
                ),
                "window_id": window.pk,
                "registration_id": event.registration_id,
                "owner_id": window.owner_id,
            },
            delayed=True,
        )


def purge_creator_content_classes(at: datetime | None = None) -> int:
    return CreatorContentClass.objects.filter(
        expires_at__lte=at or timezone.now(), fingerprint__isnull=False
    ).update(fingerprint=None)


def record_clan_window_presence(window: Any) -> None:
    if not season_has_policy(window.season_id) or not window.finalized_at:
        return
    from apps.clans.models import ClanActivity
    from apps.telegram.models import TelegramInboundUpdate

    rows = ClanActivity.objects.filter(
        membership_id=window.membership_id,
        season_id=window.season_id,
        config_version_id=window.config_version_id,
        observed_at__gte=window.window_start,
        observed_at__lt=window.window_end,
        window_processed=True,
        classification="VALID",
        sender_id__isnull=False,
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
    )
    for activity in rows:
        if not TelegramInboundUpdate.objects.filter(
            update_id=activity.telegram_update_id, webhook_authenticated_at__isnull=False
        ).exists():
            epoch = EconomicEpoch.objects.get(
                season_id=window.season_id,
                starts_at__lte=activity.observed_at,
                ends_at__gt=activity.observed_at,
            )
            defer_source(
                epoch,
                "COMMUNITY_ACTIVITY",
                f"clan:{activity.pk}",
                reason="SOURCE_AUTHENTICATION_UNPROVEN",
                player_id=activity.sender_id,
                category="LOYALTY_CONSISTENCY",
            )
            continue
        assert activity.sender_id is not None
        record_recognition(
            season_id=window.season_id,
            player_id=activity.sender_id,
            actor_id=activity.sender_id,
            source_type="COMMUNITY_ACTIVITY",
            source_id=f"clan:{activity.pk}",
            source_time=activity.observed_at,
            finalized_at=window.finalized_at,
            config_version_id=activity.config_version_id,
            inputs={"activity_id": activity.pk, "window_id": window.pk},
            delayed=True,
        )
