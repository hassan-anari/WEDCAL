"""Durable operator alerts and retryable email outbox; never changes economic authority."""

import json
import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.arena.epochs import reconcile_epoch
from apps.arena.models import (
    EconomicAlert,
    EconomicAlertDelivery,
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
        "ECONOMIC_ALERT_SEND_TIMEOUT_SECONDS",
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


def alert_routes() -> dict[str, Any]:
    routes = settings.ECONOMIC_ALERT_ROUTES
    if not isinstance(routes, dict):
        raise ValidationError("ECONOMIC_ALERT_ROUTES_INVALID")
    for key, route in routes.items():
        if (
            not isinstance(key, str)
            or not 0 < len(key) <= 64
            or not isinstance(route, dict)
            or set(route) != {"recipients", "from_email", "severities"}
            or not isinstance(route["recipients"], list)
            or not route["recipients"]
            or not isinstance(route["severities"], list)
            or not route["severities"]
            or any(s not in SEVERITIES for s in route["severities"])
        ):
            raise ValidationError("ECONOMIC_ALERT_ROUTE_INVALID")
        for address in [route["from_email"], *route["recipients"]]:
            validate_email(address)
    return routes


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


def _event(alert: EconomicAlert, outcome: str, routes: dict[str, Any], now: datetime) -> None:
    event = EconomicAlertEvent.objects.create(
        alert=alert, outcome=outcome, severity=alert.severity, evidence=alert.evidence
    )
    for key, route in routes.items():
        if event.severity in route["severities"]:
            EconomicAlertDelivery.objects.create(
                event=event, route_key=key, route_config=route, next_attempt_at=now
            )
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
    routes = alert_routes()  # Validate before any mutation or recovery decision.
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
            _event(row, outcome, routes, now)
    for row in EconomicAlert.objects.filter(status="OPEN").exclude(fingerprint__in=fingerprints):
        row.status = "RESOLVED"
        row.resolved_at = now
        row.last_seen_at = now
        row.evidence = {**row.evidence, "resolution": "CONDITION_NO_LONGER_DETECTED"}
        row.save()
        _event(row, "RESOLVED", routes, now)
    return len(findings)


@transaction.atomic
def deliver_alert(delivery_id: int, *, now: datetime | None = None) -> bool:
    if not settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        return False
    alert_rules()
    backend = settings.ECONOMIC_ALERT_EMAIL_BACKEND
    if backend not in {
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.smtp.EmailBackend",
    }:
        raise ValidationError("EXPLICIT_ECONOMIC_ALERT_EMAIL_BACKEND_REQUIRED")
    now = now or timezone.now()
    row = (
        EconomicAlertDelivery.objects.select_for_update(of=("self",), skip_locked=True)
        .select_related("event__alert")
        .filter(pk=delivery_id, status="PENDING", next_attempt_at__lte=now)
        .first()
    )
    if row is None:
        return False
    if EconomicAlertDelivery.objects.filter(
        event__alert_id=row.event.alert_id,
        route_key=row.route_key,
        event_id__lt=row.event_id,
        status="PENDING",
    ).exists():
        return False  # Recovery must not overtake an opening delivery still pending on this route.
    # Revalidate the pinned routing snapshot; never copy credentials into durable routing data.
    route = row.route_config
    for address in [route["from_email"], *route["recipients"]]:
        validate_email(address)
    row.attempts += 1
    try:
        alert = row.event.alert
        payload = {
            "alert_id": alert.pk,
            "event_id": row.event_id,
            "code": alert.code,
            "outcome": row.event.outcome,
            "severity": row.event.severity,
            "season_id": alert.season_id,
            "epoch_id": alert.epoch_id,
            "evidence": row.event.evidence,
        }
        message = EmailMessage(
            subject=f"[{row.event.severity}] {alert.code}: {row.event.outcome}",
            body=json.dumps(payload, sort_keys=True),
            from_email=route["from_email"],
            to=route["recipients"],
            connection=get_connection(
                backend=backend, timeout=settings.ECONOMIC_ALERT_SEND_TIMEOUT_SECONDS
            ),
            headers={
                "Message-ID": f"<economic-alert-{row.pk}@{route['from_email'].rsplit('@', 1)[1]}>"
            },
        )
        if message.send() != 1:
            raise ConnectionError("Transport did not acknowledge delivery")
    except Exception:
        row.last_error_code = "TRANSPORT_FAILURE"
        row.status = "DEAD" if row.attempts >= settings.ECONOMIC_ALERT_MAX_ATTEMPTS else "PENDING"
        row.next_attempt_at = now + timedelta(
            seconds=min(
                settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS,
                settings.ECONOMIC_ALERT_RETRY_BASE_SECONDS
                * 2
                ** min(row.attempts - 1, settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS.bit_length()),
            )
        )
    else:
        row.status = "DELIVERED"
        row.last_error_code = ""
        row.delivered_at = now
    row.save()
    return row.status == "DELIVERED"


@transaction.atomic
def retry_dead_delivery(
    delivery_id: int,
    *,
    actor: User,
    reference: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    if (
        not actor.is_active
        or not actor.is_staff
        or not actor.has_perm("arena.control_economic_automation")
    ):
        raise PermissionDenied("Economic operator authority required")
    if not reference.strip() or len(reference) > 255:
        raise ValidationError("ECONOMIC_ALERT_RETRY_REFERENCE_REQUIRED")
    row = (
        EconomicAlertDelivery.objects.select_for_update()
        .select_related("event")
        .get(pk=delivery_id)
    )
    if row.status != "DEAD":
        raise ValidationError("ONLY_DEAD_ALERT_DELIVERIES_REQUIRE_MANUAL_RETRY")
    if not dry_run:
        row.status = "PENDING"
        row.next_attempt_at = timezone.now()
        row.save(update_fields=("status", "next_attempt_at"))
        EconomicAlertEvent.objects.create(
            alert_id=row.event.alert_id,
            outcome="RETRY_REQUESTED",
            severity=row.event.severity,
            evidence={
                "delivery_id": row.pk,
                "actor_id": actor.pk,
                "reference_digest": digest(reference),
            },
        )
    return {"dry_run": dry_run, "delivery_id": row.pk, "attempts": row.attempts}
