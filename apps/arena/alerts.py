"""Durable economic incident detection; private operator delivery is a separate outbox."""

import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.arena.alert_delivery import materialize_event
from apps.arena.epochs import reconcile_epoch
from apps.arena.models import (
    EconomicAlert,
    EconomicAlertEvent,
    EconomicAutomationAttempt,
    EconomicAutomationControl,
    EconomicEpoch,
)
from apps.arena.policy import CATEGORIES, digest
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import SeasonCloseRun

logger = structlog.get_logger(__name__)
SEVERITIES = {"WARNING", "ERROR", "CRITICAL"}


def alert_rules() -> dict[str, dict[str, Any]]:
    for name in (
        "ECONOMIC_ALERT_BATCH_SIZE",
        "ECONOMIC_ALERT_FAILURE_COUNT",
        "ECONOMIC_ALERT_FAILURE_WINDOW_SECONDS",
        "ECONOMIC_ALERT_MAX_ATTEMPTS",
        "ECONOMIC_ALERT_RETRY_BASE_SECONDS",
        "ECONOMIC_ALERT_RETRY_MAX_SECONDS",
        "ECONOMIC_ALERT_DELIVERY_LEASE_SECONDS",
    ):
        if type(getattr(settings, name)) is not int or getattr(settings, name) <= 0:
            raise ValidationError("ECONOMIC_ALERT_OPERATIONAL_BOUND_INVALID")
    rules = {code: dict(value) for code, value in settings.ECONOMIC_ALERT_RULES.items()}
    for code, rule in rules.items():
        if (
            type(rule["enabled"]) is not bool
            or rule["severity"] not in SEVERITIES
            or type(rule["grace_seconds"]) is not int
            or rule["grace_seconds"] < 0
        ):
            raise ValidationError("ECONOMIC_ALERT_RULE_INVALID")
        if (
            code not in {"EPOCH_FREEZE_OVERDUE", "DLX_SETTLEMENT_OVERDUE", "ECONOMIC_WORKER_STALE"}
            and rule["grace_seconds"] != 0
        ):
            raise ValidationError("ECONOMIC_ALERT_GRACE_ONLY_FOR_DEADLINE_RULES")
    return rules


def detect_conditions(now: datetime | None = None) -> list[dict[str, Any]]:
    """Read canonical durable state. A failed scan raises rather than resolving existing alerts."""
    now = now or timezone.now()
    rules = alert_rules()
    findings: list[dict[str, Any]] = []

    def add(code: str, season_id: int, epoch_id: int | None, evidence: dict[str, Any]) -> None:
        rule = rules[code]
        if rule["enabled"]:
            findings.append(
                {
                    "fingerprint": digest([code, season_id, epoch_id]),
                    "code": code,
                    "season_id": season_id,
                    "epoch_id": epoch_id,
                    "severity": rule["severity"],
                    "evidence": evidence,
                }
            )

    latest = EconomicAutomationAttempt.objects.filter(epoch_id=OuterRef("pk")).order_by("-pk")
    controls = EconomicAutomationControl.objects.filter(season_id=OuterRef("season_id")).order_by(
        "-pk"
    )
    epochs = (
        EconomicEpoch.objects.filter(season__status="ACTIVE")
        .select_related("season__baseline_config")
        .annotate(
            latest_attempt_id=Subquery(latest.values("pk")[:1]),
            automation_enabled=Subquery(controls.values("enabled")[:1]),
        )
        .order_by("pk")
    )
    for epoch in epochs.iterator(chunk_size=settings.ECONOMIC_ALERT_BATCH_SIZE):
        setup = resolve_game_config_version(epoch.season.baseline_config_id).dlx_distribution
        if setup is None:
            continue
        gate_at = epoch.season.starts_at + timedelta(
            days=((epoch.season.settlement_start_epoch or 1) - 1) * setup.epoch_days
        )
        expected = max(epoch.ends_at, gate_at)
        evidence: dict[str, Any] = {
            "epoch_state": epoch.state,
            "cutoff": epoch.ends_at.isoformat(),
            "settlement_gate_at": gate_at.isoformat(),
        }
        if epoch.automation_enabled:
            if epoch.state in {"OPEN", "DRAINING"} and now >= expected + timedelta(
                seconds=rules["EPOCH_FREEZE_OVERDUE"]["grace_seconds"]
            ):
                add("EPOCH_FREEZE_OVERDUE", epoch.season_id, epoch.pk, evidence)
            if epoch.state in {"FROZEN", "SETTLING"} and now >= expected + timedelta(
                seconds=rules["DLX_SETTLEMENT_OVERDUE"]["grace_seconds"]
            ):
                add("DLX_SETTLEMENT_OVERDUE", epoch.season_id, epoch.pk, evidence)
        attempt = EconomicAutomationAttempt.objects.filter(pk=epoch.latest_attempt_id).first()
        if attempt:
            evidence = {
                **evidence,
                "attempt_id": attempt.pk,
                "stage": attempt.stage,
                "attempt_status": attempt.status,
                "error_code": attempt.error_code
                if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", attempt.error_code)
                else "REDACTED_SEE_ATTEMPT"
                if attempt.error_code
                else "",
            }
            if attempt.status == "RUNNING" and now >= attempt.started_at + timedelta(
                seconds=rules["ECONOMIC_WORKER_STALE"]["grace_seconds"]
            ):
                add("ECONOMIC_WORKER_STALE", epoch.season_id, epoch.pk, evidence)
            if epoch.automation_enabled and attempt.status in {"BLOCKED", "FAILED"}:
                if attempt.stage == "GROWTH_FREEZE" and setup.growth_milestones:
                    add("GROWTH_FINALIZATION_FAILED", epoch.season_id, epoch.pk, evidence)
                if attempt.stage == "RECONCILIATION":
                    add("ACCOUNTING_RECONCILIATION_MISMATCH", epoch.season_id, epoch.pk, evidence)
                if attempt.stage != "SETTLEMENT" or now >= gate_at:
                    add("DLX_LIFECYCLE_BLOCKED", epoch.season_id, epoch.pk, evidence)
            recent = EconomicAutomationAttempt.objects.filter(
                epoch=epoch,
                status__in=["FAILED", "INTERRUPTED"],
                started_at__gte=now
                - timedelta(seconds=settings.ECONOMIC_ALERT_FAILURE_WINDOW_SECONDS),
            ).count()
            if recent >= settings.ECONOMIC_ALERT_FAILURE_COUNT and attempt.status != "COMPLETE":
                add(
                    "ECONOMIC_WORKER_REPEATED_FAILURE",
                    epoch.season_id,
                    epoch.pk,
                    {**evidence, "failures_in_window": recent},
                )
        if epoch.state == "SETTLED":
            reconciled = reconcile_epoch(epoch)
            if set(reconciled) != set(CATEGORIES) or not all(reconciled.values()):
                add(
                    "ACCOUNTING_RECONCILIATION_MISMATCH",
                    epoch.season_id,
                    epoch.pk,
                    {**evidence, "categories": reconciled},
                )
    for run in SeasonCloseRun.objects.exclude(stage="COMPLETED").iterator(
        chunk_size=settings.ECONOMIC_ALERT_BATCH_SIZE
    ):
        if run.stage == "FAILED" or run.blocker_kind == "MANUAL":
            add(
                "SEASON_CLOSE_BLOCKED",
                run.season_id,
                None,
                {
                    "close_run_id": run.pk,
                    "stage": run.stage,
                    "blocker_kind": run.blocker_kind,
                    "failure_category": run.failure_category,
                },
            )
    # An attempt and actual reconciliation can identify the same incident in one scan.
    return list({row["fingerprint"]: row for row in findings}.values())


def _event(alert: EconomicAlert, outcome: str, now: datetime) -> None:
    event = EconomicAlertEvent.objects.create(
        alert=alert, outcome=outcome, severity=alert.severity, evidence=alert.evidence
    )
    materialize_event(event, now=now)
    logger.warning(
        "economic_alert_transition",
        alert_id=alert.pk,
        code=alert.code,
        severity=alert.severity,
        outcome=outcome,
    )


@transaction.atomic
def scan_alerts(*, now: datetime | None = None) -> int:
    if not settings.ECONOMIC_ALERTS_ENABLED:
        return 0
    now = now or timezone.now()
    alert_rules()  # Validate before any mutation or recovery decision.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(11045, 0)")
        if not cursor.fetchone()[0]:
            return 0
    findings = detect_conditions(now)
    fingerprints = []
    for finding in findings:
        fingerprints.append(finding["fingerprint"])
        row, created = EconomicAlert.objects.get_or_create(
            fingerprint=finding["fingerprint"],
            defaults={**finding, "opened_at": now, "last_seen_at": now},
        )
        outcome = (
            "OPENED"
            if created
            else "REOPENED"
            if row.status == "RESOLVED"
            else "SEVERITY_CHANGED"
            if row.severity != finding["severity"]
            else ""
        )
        row.status = "OPEN"
        row.severity = finding["severity"]
        row.evidence = finding["evidence"]
        row.last_seen_at = now
        row.resolved_at = None
        row.save()
        if outcome:
            _event(row, outcome, now)
    for row in EconomicAlert.objects.filter(status="OPEN").exclude(fingerprint__in=fingerprints):
        row.status = "RESOLVED"
        row.resolved_at = now
        row.last_seen_at = now
        row.evidence = {**row.evidence, "resolution": "CONDITION_NO_LONGER_DETECTED"}
        row.save()
        _event(row, "RESOLVED", now)
    return len(findings)
