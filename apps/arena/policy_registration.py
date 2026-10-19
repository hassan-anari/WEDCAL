"""Explicit two-person policy registration; validation never approves or activates a Season."""

from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.arena.models import EconomicPolicyApproval, EconomicPolicyProposal, EconomicPolicyVersion
from apps.arena.policy import CATEGORIES, digest, parse_policy

POLICY_NUMBERS = frozenset(
    (
        "scale",
        "days",
        "min_age_hours",
        "meaningful_days",
        "daily_only_periods",
        "community_messages_per_day",
        "tap_weight",
        "tap_saturation",
        "typing_weight",
        "typing_runs_per_day",
        "typing_wpm_ceiling",
        "daily_weight",
        "mission_weight",
        "mission_limit",
        "clan_work_weight",
        "clan_consistency_weight",
        "clan_saturation",
        "clan_meaningful_contribution",
        "creator_h_weight",
        "creator_w_weight",
        "creator_h_saturation",
        "creator_window_speakers",
        "creator_window_characters",
        "creator_windows_per_day",
        "creator_speaker_span_seconds",
        "creator_window_seconds",
        "creator_distinct_messages",
        "referral_saturation",
        "minimum_category_score",
        "player_cap_bps",
        "carry_release_bps",
        "correction_reserve_bps",
    )
)


def validate_document(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValidationError("POLICY_OBJECT_REQUIRED")
    legacy = payload.get("implementation") == "S1_FINALIZED_V1"
    required = POLICY_NUMBERS | {
        "implementation",
        "recognition",
        "participation_targets",
        "independent_mission_keys",
        "growth_milestones" if legacy else "growth",
    }
    if set(payload) != required:
        raise ValidationError("POLICY_SCHEMA_FIELDS_INVALID")
    for key in POLICY_NUMBERS:
        if type(payload[key]) is not int or not 0 < payload[key] <= 2**63 - 1:
            raise ValidationError(f"POLICY_INTEGER_INVALID:{key}")
    targets = payload["participation_targets"]
    if (
        not isinstance(targets, dict)
        or set(targets) != set(CATEGORIES)
        or any(type(v) is not int or not 0 < v <= 2**63 - 1 for v in targets.values())
    ):
        raise ValidationError("POLICY_PARTICIPATION_SCHEMA_INVALID")
    missions = payload["independent_mission_keys"]
    if not isinstance(missions, list) or any(not isinstance(v, str) for v in missions):
        raise ValidationError("POLICY_MISSION_SCHEMA_INVALID")
    if not legacy:
        growth = payload["growth"]
        if not isinstance(growth, dict) or set(growth) - {"rule", "approval_reference"}:
            raise ValidationError("POLICY_GROWTH_SCHEMA_INVALID")
        if "approval_reference" in growth:
            _reference(growth["approval_reference"])
    parse_policy(payload)  # Preserve all approved formula, scoring, recognition and safety checks.
    return digest(payload)


def _reference(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValidationError("POLICY_AUDIT_REFERENCE_REQUIRED")


def _actor(actor: User, permission: str) -> None:
    if not actor.is_active or not actor.is_staff or not actor.has_perm(permission):
        raise PermissionDenied(permission)


@transaction.atomic
def propose_policy(
    *,
    key: str,
    version: int,
    payload: Any,
    actor: User,
    submission_reference: str,
    expected_checksum: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    _actor(actor, "arena.propose_economic_policy")
    _reference(submission_reference)
    if not isinstance(key, str) or not key.strip() or len(key) > 64 or key != key.strip():
        raise ValidationError("POLICY_KEY_INVALID")
    if type(version) is not int or not 0 < version <= 2**31 - 1:
        raise ValidationError("POLICY_VERSION_INVALID")
    checksum = validate_document(payload)
    if expected_checksum is not None and expected_checksum != checksum:
        raise ValidationError("POLICY_CHECKSUM_MISMATCH")
    if not dry_run and expected_checksum is None:
        raise ValidationError("POLICY_CONFIRMED_CHECKSUM_REQUIRED")
    if EconomicPolicyVersion.objects.filter(key=key, version=version).exists():
        raise ValidationError("POLICY_ALREADY_REGISTERED")
    existing = EconomicPolicyProposal.objects.filter(key=key, version=version).first()
    if existing and (
        existing.checksum != checksum
        or existing.submitted_by_id != actor.pk
        or existing.submission_reference != submission_reference
    ):
        raise ValidationError("POLICY_PROPOSAL_CONFLICT")
    if not dry_run:
        existing, _ = EconomicPolicyProposal.objects.get_or_create(
            key=key,
            version=version,
            defaults={
                "payload": payload,
                "checksum": checksum,
                "submitted_by": actor,
                "submission_reference": submission_reference,
            },
        )
        if (
            existing.checksum != checksum
            or existing.submitted_by_id != actor.pk
            or existing.submission_reference != submission_reference
        ):
            raise ValidationError("POLICY_PROPOSAL_CONFLICT")
    return {
        "dry_run": dry_run,
        "checksum": checksum,
        "proposal_id": existing.pk if existing else None,
    }


@transaction.atomic
def approve_policy(
    proposal_id: int,
    *,
    actor: User,
    approval_reference: str,
    expected_checksum: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    _actor(actor, "arena.approve_economic_policy")
    _reference(approval_reference)
    proposal = EconomicPolicyProposal.objects.select_for_update().get(pk=proposal_id)
    if proposal.submitted_by_id == actor.pk:
        raise PermissionDenied("POLICY_INDEPENDENT_APPROVER_REQUIRED")
    checksum = validate_document(proposal.payload)
    if checksum != proposal.checksum or checksum != expected_checksum:
        raise ValidationError("POLICY_CHECKSUM_MISMATCH")
    existing = EconomicPolicyApproval.objects.filter(proposal=proposal).first()
    if existing:
        if existing.approved_by_id != actor.pk or existing.approval_reference != approval_reference:
            raise ValidationError("POLICY_APPROVAL_CONFLICT")
        return {"dry_run": dry_run, "checksum": checksum, "policy_id": existing.policy_id}
    if EconomicPolicyVersion.objects.filter(key=proposal.key, version=proposal.version).exists():
        raise ValidationError("POLICY_ALREADY_REGISTERED")
    document = None
    if not dry_run:
        document = EconomicPolicyVersion.objects.create(
            key=proposal.key,
            version=proposal.version,
            payload=proposal.payload,
            checksum=checksum,
            approval_reference=approval_reference,
        )
        EconomicPolicyApproval.objects.create(
            proposal=proposal,
            policy=document,
            approved_by=actor,
            approval_reference=approval_reference,
        )
    return {
        "dry_run": dry_run,
        "checksum": checksum,
        "policy_id": document.pk if document else None,
        **(
            {"validated_payload": proposal.payload, "submitted_by_id": proposal.submitted_by_id}
            if dry_run
            else {}
        ),
    }
