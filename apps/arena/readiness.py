"""Fail-closed integration boundary for a future approved qualification/source evaluator.

There is deliberately no Season-1 evaluator: a policy name/version is not its definition, and
an empty queue is not an ingress completion watermark. Only reviewed server code can install
an evaluator. Neither configuration, management commands nor receipt insertion enables one.
Evaluators must perform bounded database work only, join the Season barrier before domain locks,
and prove source closure (including delayed Telegram delivery), not merely return success.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone

from apps.arena.models import (
    EconomicEpoch,
    EpochReadinessEvaluation,
    PerformanceSourceFact,
    QualificationDecision,
)
from apps.game_config.services import resolve_game_config_version
from apps.seasons.barrier import acquire_close_barrier


@dataclass(frozen=True)
class EvaluationEvidence:
    # Scope must include every potentially eligible player, not only positive decisions.
    scope_count: int
    evaluated_count: int
    unresolved_count: int
    source_completion: dict[str, Any]
    provenance: dict[str, Any]
    blocking_reasons: tuple[str, ...] = ()
    scoped_pending_count: int = 0


@dataclass(frozen=True)
class ApprovedEvaluator:
    version: str
    inspect: Callable[[EconomicEpoch], EvaluationEvidence]


# Populating this requires an independently reviewed policy/source-completion implementation.
# Never add a settings switch, command flag, count-based fallback, or operator force-ready action.
APPROVED_EVALUATORS: dict[tuple[str, int], ApprovedEvaluator] = {}


def approved_evaluator(epoch: EconomicEpoch, key: str, version: int) -> ApprovedEvaluator | None:
    from apps.arena.recognition import bound_policy

    selected = bound_policy(epoch)
    if selected is not None:
        from apps.arena.economics import inspect_finalized_epoch

        binding, _ = selected
        return ApprovedEvaluator(
            f"S1_FINALIZED_V1:{binding.policy.checksum}", inspect_finalized_epoch
        )
    return APPROVED_EVALUATORS.get((key, version))


def policy_identity(epoch: EconomicEpoch) -> tuple[str, int]:
    config = resolve_game_config_version(epoch.config_version_id)
    if config.dlx_distribution is None:
        raise ValidationError("DLX_POLICY_UNAVAILABLE")
    predicate = config.dlx_distribution.qualified_player_predicate
    key, version = predicate.get("policy_key"), predicate.get("policy_version")
    if not isinstance(key, str) or not key or type(version) is not int or version < 1:
        raise ValidationError("QUALIFICATION_POLICY_INVALID")
    return key, version


def _inspect(epoch: EconomicEpoch, evaluator: ApprovedEvaluator) -> tuple[dict[str, Any], str]:
    result = evaluator.inspect(epoch)
    if (
        min(
            result.scope_count,
            result.evaluated_count,
            result.unresolved_count,
            result.scoped_pending_count,
        )
        < 0
        or result.evaluated_count > result.scope_count
        or result.unresolved_count > result.scope_count
        or result.scoped_pending_count > result.scope_count
    ):
        raise ValidationError("QUALIFICATION_COVERAGE_INVALID")
    reasons = list(result.blocking_reasons)
    if result.evaluated_count != result.scope_count or result.unresolved_count:
        reasons.append("QUALIFICATION_INCOMPLETE")
    if not result.source_completion or not result.provenance:
        reasons.append("SOURCE_COMPLETION_UNVERIFIED")
    evidence = {
        "scope_count": result.scope_count,
        "evaluated_count": result.evaluated_count,
        "unresolved_count": result.unresolved_count,
        "scoped_pending_count": result.scoped_pending_count,
        "source_completion": result.source_completion,
        "provenance": result.provenance,
        "blocking_reasons": sorted(set(reasons)),
    }
    digest = sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return evidence, digest


def evaluate_epoch_readiness(
    epoch: EconomicEpoch, *, evaluation_key: str
) -> EpochReadinessEvaluation:
    """Persist a recoverable attempt; never generate or backdate QualificationDecision rows.

    A crash leaves RUNNING (which cannot freeze). Retrying the same key resumes it. Terminal
    attempts are immutable/idempotent; changed inputs require a new evaluation identity.
    """
    if not evaluation_key.strip() or len(evaluation_key) > 200:
        raise ValidationError("EVALUATION_IDENTITY_REQUIRED")
    with transaction.atomic():
        acquire_close_barrier(epoch.season_id)
        row = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
        key, version = policy_identity(row)
        if row.state not in {"OPEN", "DRAINING"}:
            raise ValidationError("EPOCH_FINALIZED")
        run, _ = EpochReadinessEvaluation.objects.get_or_create(
            evaluation_key=evaluation_key,
            defaults={
                "epoch": row,
                "policy_key": key,
                "policy_version": version,
                "config_version_id": row.config_version_id,
                "cutoff_at": row.ends_at,
            },
        )
        if (
            run.epoch_id,
            run.policy_key,
            run.policy_version,
            run.config_version_id,
            run.cutoff_at,
        ) != (row.pk, key, version, row.config_version_id, row.ends_at):
            raise ValidationError("EVALUATION_IDEMPOTENCY_CONFLICT")
        if run.status != "RUNNING":
            return run
    try:
        with transaction.atomic():
            acquire_close_barrier(epoch.season_id)
            row = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
            run = EpochReadinessEvaluation.objects.select_for_update().get(pk=run.pk)
            if run.status != "RUNNING":
                return run
            reasons: list[str] = []
            if row.state not in {"OPEN", "DRAINING"}:
                reasons.append("EPOCH_FINALIZED")
            if timezone.now() < row.ends_at:
                reasons.append("EPOCH_ACTIVITY_WINDOW_OPEN")
            evaluator = approved_evaluator(row, key, version)
            if evaluator is None:
                reasons.extend(
                    ["QUALIFICATION_EVALUATOR_UNCONFIGURED", "SOURCE_COMPLETION_UNVERIFIED"]
                )
            if not reasons and evaluator is not None:
                from apps.arena.epochs import drain_blockers

                reasons.extend(f"DRAIN_{x.provider}" for x in drain_blockers(row))
                run.evaluator_version = evaluator.version
                run.evidence, run.input_digest = _inspect(row, evaluator)
                reasons.extend(run.evidence["blocking_reasons"])
            run.status = "BLOCKED" if reasons else "COMPLETE"
            run.blocking_reasons = sorted(set(reasons))
            run.completed_at = timezone.now()
            run.save()
            return run
    except Exception:
        # The failed work rolled back first. Keep safe diagnostics without arbitrary exception text.
        with transaction.atomic():
            acquire_close_barrier(epoch.season_id)
            run = EpochReadinessEvaluation.objects.select_for_update().get(pk=run.pk)
            if run.status == "RUNNING":
                run.status = "FAILED"
                run.blocking_reasons = ["EVALUATION_FAILED"]
                run.completed_at = timezone.now()
                run.save()
        raise


def readiness_blockers(epoch: EconomicEpoch) -> tuple[list[str], EpochReadinessEvaluation | None]:
    """Called under the exclusive barrier by Freeze; does not write or initiate evaluation."""
    try:
        key, version = policy_identity(epoch)
    except ValidationError:
        return ["QUALIFICATION_POLICY_INVALID"], None
    run = EpochReadinessEvaluation.objects.filter(epoch=epoch).order_by("-id").first()
    reasons: list[str] = []
    if run is None:
        reasons.append("QUALIFICATION_NOT_EVALUATED")
    elif run.status != "COMPLETE":
        reasons.append(f"QUALIFICATION_{run.status}")
        reasons.extend(run.blocking_reasons)
    evaluator = approved_evaluator(epoch, key, version)
    if evaluator is None:
        reasons.extend(["QUALIFICATION_EVALUATOR_UNCONFIGURED", "SOURCE_COMPLETION_UNVERIFIED"])
    if run is not None and run.status == "COMPLETE":
        if (run.policy_key, run.policy_version, run.config_version_id, run.cutoff_at) != (
            key,
            version,
            epoch.config_version_id,
            epoch.ends_at,
        ):
            reasons.append("QUALIFICATION_PROVENANCE_MISMATCH")
        elif evaluator is not None:
            evidence, digest = _inspect(epoch, evaluator)
            if run.evaluator_version != evaluator.version or run.input_digest != digest:
                reasons.append("QUALIFICATION_EVIDENCE_STALE")
            reasons.extend(evidence["blocking_reasons"])
    return sorted(set(reasons)), run


def preflight_epoch(epoch: EconomicEpoch) -> dict[str, Any]:
    """One consistent PostgreSQL read-only snapshot; no readiness locks or evaluator execution.

    READY is reserved for a future approved verifier. The operator cannot accidentally execute
    a plugin/evaluator or manufacture completeness by running this inspection.
    """
    from apps.arena.epochs import drain_blockers

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '10s'")
        row = EconomicEpoch.objects.select_related("season", "config_version").get(pk=epoch.pk)
        key, version = policy_identity(row)
        run = row.evaluations.order_by("-id").first()
        decisions = QualificationDecision.objects.filter(
            policy_key=key, policy_version=version, effective_at__lte=row.ends_at
        )
        latest = decisions.order_by("player_id", "-effective_at", "-id").distinct("player_id")
        counts = {state: 0 for state in QualificationDecision.Decision.values}
        # DISTINCT ON remains inside a subquery; counts never materialize player identifiers.
        for item in (
            QualificationDecision.objects.filter(pk__in=latest.values("pk"))
            .values("decision")
            .annotate(count=Count("pk"))
        ):
            counts[item["decision"]] = item["count"]
        from apps.moderation.models import ModerationAction
        from apps.players.models import Player

        actions = ModerationAction.objects.filter(target_id=OuterRef("pk"))
        candidates = Player.objects.filter(
            pk__in=QualificationDecision.objects.filter(
                pk__in=latest.values("pk"), decision="QUALIFIED"
            ).values("player_id")
        ).annotate(
            cutoff_state=Subquery(
                actions.filter(created_at__lte=row.ends_at)
                .order_by("-created_at", "-id")
                .values("resulting_state__moderation_state")[:1]
            ),
            has_audit=Exists(actions),
        )
        moderated = candidates.filter(
            Q(cutoff_state__in=["SUSPENDED", "BANNED"])
            | Q(
                cutoff_state__isnull=True,
                has_audit=False,
                moderation_state__in=["SUSPENDED", "BANNED"],
            )
        ).count()
        drains = {item.provider: item.count for item in drain_blockers(row)}
        reasons = [f"DRAIN_{name}" for name in drains]
        if timezone.now() < row.ends_at:
            reasons.append("EPOCH_ACTIVITY_WINDOW_OPEN")
        if run is None:
            reasons.append("QUALIFICATION_NOT_EVALUATED")
        elif run.status != "COMPLETE":
            reasons.extend([f"QUALIFICATION_{run.status}", *run.blocking_reasons])
        # A read-only preview never certifies a final transactional verification.
        reasons.append("FINAL_TRANSACTIONAL_VERIFICATION_REQUIRED")
        if approved_evaluator(row, key, version) is None:
            reasons.extend(["QUALIFICATION_EVALUATOR_UNCONFIGURED", "SOURCE_COMPLETION_UNVERIFIED"])
        facts = PerformanceSourceFact.objects.filter(
            season_id=row.season_id, source_time__gte=row.starts_at, source_time__lt=row.ends_at
        )
        from apps.rewards.models import RewardLedger

        rewards = RewardLedger.objects.filter(
            season_id=row.season_id, occurred_at__gte=row.starts_at, occurred_at__lt=row.ends_at
        )
        return {
            "season": row.season.code,
            "epoch_number": row.epoch_number,
            "epoch_state": row.state,
            "starts_at": row.starts_at.isoformat(),
            "ends_at": row.ends_at.isoformat(),
            "observed_at": timezone.now().isoformat(),
            "config": {
                "id": row.config_version_id,
                "version": row.config_version.version,
                "checksum": row.config_version.payload_checksum,
            },
            "qualification_policy": {"key": key, "version": version},
            "qualification_decisions_at_cutoff": decisions.count(),
            "latest_decisions": counts,
            "qualification_scope": "Global decisions at cutoff; coverage is separate",
            "qualified_before_moderation": counts["QUALIFIED"],
            "qualified_players": counts["QUALIFIED"] - moderated,
            "moderation_excluded_players": moderated,
            "excluded_or_disqualified_players": counts["DISQUALIFIED"] + moderated,
            "unresolved_eligibility": None if run is None else run.evidence.get("unresolved_count"),
            "economic_activity_players": rewards.values("player_id")
            .union(facts.values("player_id"))
            .count(),
            "source_facts_by_category": list(
                facts.values("category", "source_type")
                .annotate(count=Count("pk"))
                .order_by("category", "source_type")
            ),
            "reward_sources": list(
                rewards.values("source_type").annotate(count=Count("pk")).order_by("source_type")
            ),
            "pending_sources": drains,
            "evaluation": None
            if run is None
            else {
                "id": run.pk,
                "status": run.status,
                "evaluator_version": run.evaluator_version,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            },
            "readiness": "NOT_EVALUATED" if run is None else "BLOCKED",
            "blocking_reasons": sorted(set(reasons)),
            "required_action": "Approve qualification rules and source completion. "
            "preserve durable work. Do not force readiness or edit Epoch state.",
            "expected_freeze_result": "BLOCKED"
            if row.state in {"OPEN", "DRAINING"}
            else "ALREADY_FINALIZED_REQUIRES_HISTORICAL_REVIEW",
        }
