# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connections, transaction
from django.urls import reverse

from apps.arena.alerts import deliver_alert, detect_conditions, retry_dead_delivery, scan_alerts
from apps.arena.automation import run_weekly_lifecycle
from apps.arena.models import (
    EconomicAlert,
    EconomicAlertDelivery,
    EconomicAlertEvent,
    EconomicAutomationAttempt,
    EconomicPolicyApproval,
    EconomicPolicyProposal,
)
from apps.seasons.models import SeasonCloseRun

from .test_phase4_configurable import configurable_world, milestone  # noqa: F401


@pytest.fixture
def alerts(settings):
    settings.ECONOMIC_ALERTS_ENABLED = True
    settings.ECONOMIC_ALERT_DELIVERY_ENABLED = True
    settings.ECONOMIC_ALERT_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.ECONOMIC_ALERT_ROUTES = {
        "test-ops": {
            "recipients": ["operator@example.invalid"],
            "from_email": "alerts@example.invalid",
            "severities": ["WARNING", "ERROR", "CRITICAL"],
        }
    }
    settings.ECONOMIC_ALERT_RULES = copy.deepcopy(settings.ECONOMIC_ALERT_RULES)
    return settings


def overdue(w):
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at + timedelta(hours=2)


@pytest.mark.django_db
def test_disabled_monitor_and_delivery_cannot_send(configurable_world, settings):
    w = configurable_world()
    overdue(w)
    assert scan_alerts() == 0
    assert not EconomicAlert.objects.exists()
    assert not deliver_alert(99999)


@pytest.mark.django_db
def test_deadline_dedup_recovery_and_disposable_delivery(configurable_world, alerts):
    w = configurable_world()
    overdue(w)
    for _ in range(2):
        assert scan_alerts() == 1
    incident = EconomicAlert.objects.get()
    assert incident.code == "EPOCH_FREEZE_OVERDUE"
    assert EconomicAlertEvent.objects.count() == EconomicAlertDelivery.objects.count() == 1
    delivery = EconomicAlertDelivery.objects.get()
    assert deliver_alert(delivery.pk)
    assert not deliver_alert(delivery.pk)
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["operator@example.invalid"]
    assert json.loads(str(mail.outbox[0].body))["code"] == incident.code
    assert run_weekly_lifecycle(w.season) == 1
    assert scan_alerts() == 0
    incident.refresh_from_db()
    assert incident.status == "RESOLVED"
    recovery = EconomicAlertDelivery.objects.exclude(pk=delivery.pk).get()
    assert deliver_alert(recovery.pk)
    assert json.loads(str(mail.outbox[-1].body))["outcome"] == "RESOLVED"
    assert scan_alerts() == 0 and EconomicAlertEvent.objects.count() == 2
    directory = Path("work/phase4-alert-reports")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lifecycle.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "transport": alerts.ECONOMIC_ALERT_EMAIL_BACKEND,
                "captured_messages": [json.loads(str(message.body)) for message in mail.outbox],
                "events": list(
                    EconomicAlertEvent.objects.order_by("pk").values("outcome", "severity")
                ),
                "deliveries": list(
                    EconomicAlertDelivery.objects.order_by("pk").values("status", "attempts")
                ),
                "incident_status": incident.status,
            },
            indent=2,
        )
        + "\n"
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        EconomicAlertEvent.objects.filter(pk=recovery.event_id).update(outcome="rewritten")


@pytest.mark.django_db
def test_growth_repeated_failure_and_real_worker_recovery(configurable_world, alerts):
    w = configurable_world(milestones=[milestone("approved", 1, 101)])
    overdue(w)
    control = w.season.economicautomationcontrol_set.get()
    for _ in range(alerts.ECONOMIC_ALERT_FAILURE_COUNT):
        EconomicAutomationAttempt.objects.create(
            epoch=w.epochs[0], control=control, stage="GROWTH_FREEZE", status="FAILED"
        )
    scan_alerts()
    codes = set(EconomicAlert.objects.values_list("code", flat=True))
    assert {
        "GROWTH_FINALIZATION_FAILED",
        "DLX_LIFECYCLE_BLOCKED",
        "ECONOMIC_WORKER_REPEATED_FAILURE",
    } <= codes
    run_weekly_lifecycle(w.season)
    scan_alerts()
    assert not EconomicAlert.objects.filter(status="OPEN").exists()


@pytest.mark.django_db
def test_recovery_delivery_waits_for_pending_opening(configurable_world, alerts):
    w = configurable_world()
    overdue(w)
    scan_alerts()
    opening = EconomicAlertDelivery.objects.get()
    run_weekly_lifecycle(w.season)
    scan_alerts()
    recovery = EconomicAlertDelivery.objects.exclude(pk=opening.pk).get()
    assert not deliver_alert(recovery.pk)
    assert deliver_alert(opening.pk)
    assert deliver_alert(recovery.pk)
    assert [json.loads(str(m.body))["outcome"] for m in mail.outbox] == ["OPENED", "RESOLVED"]


@pytest.mark.django_db
def test_stale_worker_and_restart_recovery(configurable_world, alerts):
    w = configurable_world()
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    attempt = EconomicAutomationAttempt.objects.create(
        epoch=w.epochs[0],
        control=w.season.economicautomationcontrol_set.get(),
        stage="PREPARE_QUALIFICATION",
    )
    w.clock["at"] += timedelta(
        seconds=alerts.ECONOMIC_ALERT_RULES["ECONOMIC_WORKER_STALE"]["grace_seconds"] + 1
    )
    scan_alerts()
    assert EconomicAlert.objects.filter(code="ECONOMIC_WORKER_STALE", status="OPEN").exists()
    run_weekly_lifecycle(w.season)
    scan_alerts()
    attempt.refresh_from_db()
    assert attempt.status == "INTERRUPTED"
    assert not EconomicAlert.objects.filter(status="OPEN").exists()


@pytest.mark.django_db
def test_incompatible_worker_is_durably_visible_before_economic_work(configurable_world, alerts):
    w = configurable_world()
    w.enable()
    with pytest.raises(ValidationError, match="INCOMPATIBLE"):
        run_weekly_lifecycle(w.season, capability="INCOMPATIBLE_OLD_WORKER")
    assert scan_alerts() == 1
    incident = EconomicAlert.objects.get()
    assert incident.code == "DLX_LIFECYCLE_BLOCKED"
    assert incident.evidence["error_code"] == "AUTOMATION_WORKER_OR_POLICY_INCOMPATIBLE"
    assert all(e.state == "OPEN" for e in w.epochs)


@pytest.mark.django_db
def test_official_gate_and_pause_are_not_false_overdue_failures(configurable_world, alerts):
    from apps.arena.automation import control_automation

    w = configurable_world(count=6, closing=3, gate=3)
    overdue(w)
    run_weekly_lifecycle(w.season)
    assert not detect_conditions()
    w.clock["at"] = w.epochs[1].ends_at + timedelta(hours=2)
    scan_alerts()
    assert EconomicAlert.objects.filter(code="DLX_SETTLEMENT_OVERDUE").exists()
    control_automation(
        w.season, enabled=False, actor=w.actor, reason="planned pause", evidence_reference="test"
    )
    scan_alerts()
    assert not EconomicAlert.objects.filter(status="OPEN").exists()


@pytest.mark.django_db
def test_reconciliation_signal_and_failed_or_manual_close(configurable_world, alerts, monkeypatch):
    w = configurable_world()
    overdue(w)
    run_weekly_lifecycle(w.season)
    # Fault injection at the canonical reconciliation adapter; no immutable ledger is rewritten.
    monkeypatch.setattr("apps.arena.alerts.reconcile_epoch", lambda _: {"CORE_GAMEPLAY": False})
    scan_alerts()
    assert EconomicAlert.objects.filter(code="ACCOUNTING_RECONCILIATION_MISMATCH").exists()
    run = SeasonCloseRun.objects.create(
        season=w.season,
        idempotency_key="synthetic-alert-close",
        cutoff_at=w.season.ends_at,
        reason="isolated monitoring test",
        stage="FAILED",
        failure_category="ValidationError",
    )
    scan_alerts()
    assert EconomicAlert.objects.filter(code="SEASON_CLOSE_BLOCKED").exists()
    run.stage, run.blocker_kind = "DRAINING", "MANUAL"
    run.save()
    scan_alerts()
    assert EconomicAlert.objects.get(code="SEASON_CLOSE_BLOCKED").status == "OPEN"
    run.stage, run.blocker_kind = "COMPLETED", ""
    run.save()
    scan_alerts()
    assert EconomicAlert.objects.get(code="SEASON_CLOSE_BLOCKED").status == "RESOLVED"


@pytest.mark.django_db
def test_transport_retry_exhaustion_permissioned_recovery_and_pinned_routing(
    configurable_world, alerts, monkeypatch
):
    w = configurable_world()
    overdue(w)
    scan_alerts()
    row = EconomicAlertDelivery.objects.get()
    original = __import__("apps.arena.alerts", fromlist=["get_connection"]).get_connection
    monkeypatch.setattr(
        "apps.arena.alerts.get_connection",
        lambda **_: (_ for _ in ()).throw(ConnectionError("test only")),
    )
    alerts.ECONOMIC_ALERT_MAX_ATTEMPTS = 2
    for now in (w.clock["at"], w.clock["at"] + timedelta(hours=1)):
        assert not deliver_alert(row.pk, now=now)
    row.refresh_from_db()
    assert row.status == "DEAD" and row.attempts == 2
    assert row.last_error_code == "TRANSPORT_FAILURE"
    assert retry_dead_delivery(row.pk, actor=w.actor, reference="fixed-test-route")["dry_run"]
    row.refresh_from_db()
    assert row.status == "DEAD"
    with pytest.raises(PermissionDenied):
        retry_dead_delivery(
            row.pk,
            actor=User.objects.create_user("auditor-only", is_staff=True),
            reference="x",
            dry_run=False,
        )
    retry_dead_delivery(row.pk, actor=w.actor, reference="fixed-test-route", dry_run=False)
    monkeypatch.setattr("apps.arena.alerts.get_connection", original)
    alerts.ECONOMIC_ALERT_ROUTES["test-ops"]["recipients"] = ["changed@example.invalid"]
    assert deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.attempts == 3
    assert mail.outbox[-1].to == ["operator@example.invalid"]
    assert EconomicAlertEvent.objects.filter(outcome="RETRY_REQUESTED").count() == 1
    with pytest.raises(DatabaseError), transaction.atomic():
        EconomicAlertDelivery.objects.filter(pk=row.pk).update(route_config={})


@pytest.mark.django_db
def test_failed_scan_cannot_resolve_alerts_or_deliver_from_preview(
    configurable_world, alerts, monkeypatch
):
    w = configurable_world()
    overdue(w)
    call_command("monitor_economics", stdout=io.StringIO())
    assert not EconomicAlert.objects.exists()
    scan_alerts()
    original = __import__("apps.arena.alerts", fromlist=["detect_conditions"]).detect_conditions
    monkeypatch.setattr(
        "apps.arena.alerts.detect_conditions",
        lambda _: (_ for _ in ()).throw(ConnectionError("test")),
    )
    with pytest.raises(ConnectionError):
        scan_alerts()
    assert EconomicAlert.objects.get().status == "OPEN"
    monkeypatch.setattr("apps.arena.alerts.detect_conditions", original)
    alerts.ECONOMIC_ALERT_ROUTES = {"bad-route": {}}
    with pytest.raises(ValidationError):
        scan_alerts()
    assert EconomicAlert.objects.get().status == "OPEN"


@pytest.mark.django_db
def test_configured_deadline_severity_and_dedup(configurable_world, alerts):
    w = configurable_world()
    overdue(w)
    rule = alerts.ECONOMIC_ALERT_RULES["EPOCH_FREEZE_OVERDUE"]
    rule["grace_seconds"] = 3 * 60 * 60
    assert scan_alerts() == 0
    w.clock["at"] += timedelta(hours=2)
    assert scan_alerts() == 1
    rule["severity"] = "CRITICAL"
    assert scan_alerts() == 1
    assert EconomicAlert.objects.get().severity == "CRITICAL"
    assert list(EconomicAlertEvent.objects.order_by("pk").values_list("outcome", flat=True)) == [
        "OPENED",
        "SEVERITY_CHANGED",
    ]
    assert scan_alerts() == 1 and EconomicAlertEvent.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_scans_and_deliveries_do_not_duplicate_outbox(configurable_world, alerts):
    w = configurable_world()
    overdue(w)
    barrier = Barrier(2)

    def worker(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return operation()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, [scan_alerts, scan_alerts]))
    assert EconomicAlertEvent.objects.count() == EconomicAlertDelivery.objects.count() == 1
    row = EconomicAlertDelivery.objects.get()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(worker, [lambda: deliver_alert(row.pk), lambda: deliver_alert(row.pk)])
        )
    assert outcomes.count(True) == 1 and len(mail.outbox) == 1


@pytest.mark.django_db
def test_new_audit_admins_are_read_only_and_route_details_redacted(
    configurable_world, alerts, client, rf
):
    w = configurable_world()
    overdue(w)
    scan_alerts()
    client.force_login(w.actor)
    request = rf.get("/")
    request.user = w.actor
    for model in (
        EconomicAlert,
        EconomicAlertEvent,
        EconomicAlertDelivery,
        EconomicPolicyProposal,
        EconomicPolicyApproval,
    ):
        inspector = admin.site._registry[model]
        assert not inspector.has_add_permission(request)
        assert not inspector.has_change_permission(request)
        assert not inspector.has_delete_permission(request)
        assert not inspector.get_actions(request)
    row = EconomicAlertDelivery.objects.get()
    page = client.get(reverse("admin:arena_economicalertdelivery_change", args=(row.pk,)))
    assert page.status_code == 200 and b"operator@example.invalid" not in page.content
    assert (
        client.post(
            reverse("admin:arena_economicalertdelivery_change", args=(row.pk,)), {}
        ).status_code
        == 403
    )
