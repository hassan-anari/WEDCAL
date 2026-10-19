"""Explicit activation, pause, crash recovery and orchestration of existing economic services."""

from collections.abc import Callable
from functools import partial
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.arena.economics import prepare_epoch, require_permission
from apps.arena.epochs import (
    EpochBlocked,
    SettlementOrderBlocked,
    freeze_epoch,
    reconcile_epoch,
    settle_epoch,
)
from apps.arena.models import EconomicAutomationAttempt, EconomicAutomationControl
from apps.arena.readiness import evaluate_epoch_readiness
from apps.arena.recognition import bound_policy
from apps.seasons.models import Season

CAPABILITY = "CONFIGURABLE_WEEKLY_V1"
CONTROL_LOCK = 11044  # Dedicated structural lock namespace, independent of Season write barriers.
WORKER_LOCK = 11043
logger = structlog.get_logger(__name__)


def _control(season_id: int) -> EconomicAutomationControl | None:
    return EconomicAutomationControl.objects.filter(season_id=season_id).order_by("-pk").first()


def _compatible(season: Season) -> Any:
    rows = list(season.economic_epochs.order_by("epoch_number"))
    from apps.game_config.services import resolve_game_config_version

    setup = resolve_game_config_version(season.baseline_config_id).dlx_distribution
    if setup is None or len(rows) != setup.epoch_count:
        raise ValidationError("AUTOMATION_EPOCH_TOPOLOGY_INCOMPLETE")
    selected = [bound_policy(row) for row in rows]
    if any(value is None for value in selected):
        raise ValidationError("AUTOMATION_POLICY_UNBOUND")
    documents = {value[0].policy_id for value in selected if value is not None}
    if len(documents) != 1:
        raise ValidationError("AUTOMATION_POLICY_MIXED")
    assert selected[0] is not None
    binding, policy = selected[0]
    from apps.arena.configuration import validate_policy_compatibility

    validate_policy_compatibility(season.baseline_config, binding.policy, policy)
    return binding.policy


@transaction.atomic
def control_automation(
    season: Season, *, enabled: bool, actor: User, reason: str, evidence_reference: str
) -> EconomicAutomationControl:
    require_permission(actor, "arena.control_economic_automation")
    if type(enabled) is not bool or not reason.strip() or not evidence_reference.strip():
        raise ValidationError("AUTOMATION_EXPLICIT_AUTHORITY_REQUIRED")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [CONTROL_LOCK, season.pk])
    season = Season.objects.select_related("baseline_config").get(pk=season.pk)
    previous = _control(season.pk)
    if enabled:
        if season.status != "ACTIVE":
            raise ValidationError("AUTOMATION_REQUIRES_ACTIVE_SEASON")
        document = _compatible(season)
    elif previous is not None:
        document = previous.policy
    else:
        raise ValidationError("AUTOMATION_NOT_PREVIOUSLY_AUTHORIZED")
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('dexo.phase4_control', %s, true)", [CAPABILITY])
    return EconomicAutomationControl.objects.create(
        season=season,
        enabled=enabled,
        actor=actor,
        policy=document,
        config_checksum=season.baseline_config.payload_checksum,
        capability=CAPABILITY,
        reason=reason,
        evidence_reference=evidence_reference,
    )


def _stage(
    season: Season,
    control: EconomicAutomationControl,
    attempt: EconomicAutomationAttempt,
    name: str,
    operation: Callable[[], Any],
) -> Any:
    # Pause is serialized against each stage, so no new economic stage starts after pause returns.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", [CONTROL_LOCK, season.pk])
        current = _control(season.pk)
        if current is None or current.pk != control.pk or not current.enabled:
            raise EpochBlocked("AUTOMATION_PAUSED_OR_AUTHORITY_CHANGED")
        attempt.stage = name
        attempt.save(update_fields=("stage",))
        return operation()


def _finalize_sources(season: Season) -> dict[str, int]:
    from apps.clans.missions import process_mission_signals
    from apps.clans.windowing import finalize_due_windows
    from apps.network.services import settle_completed_windows

    limit = settings.DLX_AUTOMATION_SOURCE_BATCH_SIZE
    return {
        "creator_windows": settle_completed_windows(limit=limit, season_id=season.pk),
        "clan_windows": finalize_due_windows(limit=limit, season_id=season.pk),
        "mission_signals": process_mission_signals(limit=limit, season_id=season.pk),
    }


def run_weekly_lifecycle(season: Season, *, capability: str = CAPABILITY) -> int:
    """One connection-owned worker lock, released on process death. Stages commit independently.

    Use direct PostgreSQL/session pooling; transaction-mode PgBouncer is incompatible with
    this session lock. No lock survives a lost worker connection, and every stage is replayable.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [WORKER_LOCK, season.pk])
        if not cursor.fetchone()[0]:
            return 0
    try:
        control = _control(season.pk)
        if control is None or not control.enabled:
            return 0
        season = Season.objects.select_related("baseline_config").get(pk=season.pk)
        try:
            if (
                capability != CAPABILITY
                or control.capability != capability
                or control.config_checksum != season.baseline_config.payload_checksum
                or season.status != "ACTIVE"
                or _compatible(season).pk != control.policy_id
            ):
                raise ValidationError("AUTOMATION_WORKER_OR_POLICY_INCOMPATIBLE")
        except ValidationError:
            target = (
                season.economic_epochs.exclude(state="SETTLED").order_by("epoch_number").first()
            )
            if target is not None:
                EconomicAutomationAttempt.objects.create(
                    epoch=target,
                    control=control,
                    stage="COMPATIBILITY",
                    status="BLOCKED",
                    error_code="AUTOMATION_WORKER_OR_POLICY_INCOMPATIBLE",
                    completed_at=timezone.now(),
                )
            raise
        # RUNNING rows can only belong to a lost prior worker, since we own its session lock.
        EconomicAutomationAttempt.objects.filter(epoch__season=season, status="RUNNING").update(
            status="INTERRUPTED", error_code="WORKER_CONNECTION_LOST", completed_at=timezone.now()
        )
        count = 0
        completed = EconomicAutomationAttempt.objects.filter(
            epoch__season=season, status="COMPLETE"
        ).values("epoch_id")
        for epoch in (
            season.economic_epochs.filter(ends_at__lte=timezone.now())
            .exclude(state="SETTLED", pk__in=completed)
            .order_by("epoch_number")
        ):
            attempt = EconomicAutomationAttempt.objects.create(
                epoch=epoch, control=control, stage="START"
            )
            try:
                stage = partial(_stage, season, control, attempt)

                if epoch.state in {"OPEN", "DRAINING"}:
                    stage("SOURCE_FINALIZATION", lambda: _finalize_sources(season))
                    stage("PREPARE_QUALIFICATION", partial(prepare_epoch, epoch))
                    evaluation = stage(
                        "READINESS",
                        partial(
                            evaluate_epoch_readiness,
                            epoch,
                            evaluation_key=f"automation:{attempt.pk}",
                        ),
                    )
                    if evaluation.status != "COMPLETE":
                        raise EpochBlocked(",".join(evaluation.blocking_reasons))
                    stage("GROWTH_FREEZE", partial(freeze_epoch, epoch))
                if epoch.state != "SETTLED":
                    stage("SETTLEMENT", partial(settle_epoch, epoch))
                    count += 1
                reconciled = stage("RECONCILIATION", partial(reconcile_epoch, epoch))
                if not reconciled or not all(reconciled.values()):
                    raise EpochBlocked("CATEGORY_RECONCILIATION_FAILED")
                attempt.status = "COMPLETE"
                attempt.evidence = {"categories": reconciled}
            except (EpochBlocked, SettlementOrderBlocked, ValidationError) as exc:
                attempt.status = "BLOCKED"
                attempt.error_code = str(exc)[:128]
            except Exception:
                attempt.status = "FAILED"
                attempt.error_code = "TRANSIENT_OR_UNEXPECTED_FAILURE"
                raise
            finally:
                attempt.completed_at = timezone.now()
                attempt.save(
                    update_fields=("stage", "status", "error_code", "completed_at", "evidence")
                )
                logger.info(
                    "dlx_automation_stage_result",
                    season_id=season.pk,
                    epoch_id=epoch.pk,
                    stage=attempt.stage,
                    status=attempt.status,
                    error_code=attempt.error_code,
                )
            if attempt.status != "COMPLETE":
                break  # No later Epoch may overtake a blocked earlier Epoch.
        return count
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [WORKER_LOCK, season.pk])
