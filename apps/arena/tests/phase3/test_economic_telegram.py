# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import io
from datetime import timedelta
from http.client import HTTPMessage
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError

from apps.arena.alert_delivery import (
    authorized_operators,
    deliver_alert,
    format_message,
    materialize_event,
    prepare_test_delivery,
    recipient_preview,
    recover_expired_deliveries,
    review_delivery,
    send_test_delivery,
)
from apps.arena.alerts import scan_alerts
from apps.arena.models import DlxAllocation, EconomicAlertDelivery, EconomicAlertEvent
from apps.notifications.models import BotAdminOperator, BroadcastCampaign, Notification
from apps.players.models import Player
from apps.telegram import bot_api
from apps.telegram.budget import BudgetDenial

from .test_economic_alerts import alerts, overdue  # noqa: F401
from .test_phase4_configurable import configurable_world  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
ORIGINAL_SEND = bot_api.send_message


def opening(configurable_world):
    w = configurable_world()
    overdue(w)
    scan_alerts()
    return w, EconomicAlertDelivery.objects.get()


def permission(code):
    return Permission.objects.get(content_type__app_label="arena", codename=code)


def test_exact_database_permission_population_and_group_permission(configurable_world, alerts):
    w, row = opening(configurable_world)
    op = alerts.TEST_OPERATOR
    user = op.user
    user.is_superuser = False
    user.save()
    assert not authorized_operators()
    user.user_permissions.add(Permission.objects.get(codename="manage_broadcasts"))
    assert not authorized_operators()  # Broadcast authority is independent.
    group = Group.objects.create(name="Economic alert recipients")
    group.permissions.add(permission("receive_economic_alert"))
    user.groups.add(group)
    assert [x.pk for x in authorized_operators()] == [op.pk]
    user.user_permissions.add(permission("receive_economic_alert"))
    assert len(authorized_operators()) == 1  # Direct + group permission cannot duplicate.
    User.objects.create_superuser("unmapped-staff")
    assert len(authorized_operators()) == 1
    assert deliver_alert(row.pk)
    assert not BroadcastCampaign.objects.exists() and not Notification.objects.exists()
    assert not DlxAllocation.objects.exists()


@pytest.mark.parametrize(
    "revocation", ["disabled", "inactive", "nonstaff", "permission", "identity", "contact"]
)
def test_dispatch_revalidates_revocation_without_any_send(configurable_world, alerts, revocation):
    _, row = opening(configurable_world)
    op = alerts.TEST_OPERATOR
    if revocation == "disabled":
        BotAdminOperator.objects.filter(pk=op.pk).update(enabled=False)
    elif revocation == "inactive":
        User.objects.filter(pk=op.user_id).update(is_active=False)
    elif revocation == "nonstaff":
        User.objects.filter(pk=op.user_id).update(is_staff=False)
    elif revocation == "permission":
        User.objects.filter(pk=op.user_id).update(is_superuser=False)
    elif revocation == "identity":
        BotAdminOperator.objects.filter(pk=op.pk).update(telegram_user_id=771002)
    else:
        Player.objects.filter(telegram_user_id=op.telegram_user_id).update(
            first_private_contact_at=None
        )
    assert not deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.status == "DEAD" and row.last_error_code == "RECIPIENT_AUTHORITY_REVOKED"
    assert not alerts.CAPTURED_MESSAGES


def test_multiple_recipients_no_players_created_and_private_only(configurable_world, alerts):
    w = configurable_world()
    second = User.objects.create_superuser("second-private-auditor")
    unreachable = BotAdminOperator.objects.create(
        user=second, telegram_user_id=771003, enabled=True
    )
    negative = User.objects.create_superuser("invalid-group-operator")
    BotAdminOperator.objects.create(user=negative, telegram_user_id=-771004, enabled=True)
    before = Player.objects.count()
    overdue(w)
    scan_alerts()
    assert Player.objects.count() == before
    assert EconomicAlertDelivery.objects.count() == 2
    dead = EconomicAlertDelivery.objects.get(operator=unreachable)
    assert dead.status == "DEAD" and dead.last_error_code == "PRIVATE_CONTACT_REQUIRED"
    assert {item["reachable"] for item in recipient_preview()} == {True, False}
    good = EconomicAlertDelivery.objects.get(operator=alerts.TEST_OPERATOR)
    assert deliver_alert(good.pk)
    assert all(x["chat_id"] > 0 for x in alerts.CAPTURED_MESSAGES)


def test_no_authorized_recipients_durable_failure_and_reroute(configurable_world, alerts):
    alerts.TEST_OPERATOR.enabled = False
    alerts.TEST_OPERATOR.save()
    w = configurable_world()
    overdue(w)
    scan_alerts()
    assert not EconomicAlertDelivery.objects.exists()
    failed = EconomicAlertEvent.objects.get(outcome="ROUTING_FAILED")
    assert failed.evidence["reason"] == "NO_AUTHORIZED_OPERATOR"
    event = EconomicAlertEvent.objects.get(outcome="OPENED")
    alerts.TEST_OPERATOR.enabled = True
    alerts.TEST_OPERATOR.save()
    assert materialize_event(event) == materialize_event(event)
    assert EconomicAlertDelivery.objects.count() == 1
    assert not alerts.CAPTURED_MESSAGES


@pytest.mark.parametrize("category", ["rate_limited", "local_rate_limited", "budget_unavailable"])
def test_safe_admission_retries_respect_retry_after(
    configurable_world, alerts, monkeypatch, category
):
    w, row = opening(configurable_world)
    alerts.ECONOMIC_ALERT_RETRY_MAX_SECONDS = 60
    monkeypatch.setattr(
        bot_api,
        "send_message",
        lambda *_: bot_api.TelegramResult(False, category=category, retry_after=300),
    )
    assert not deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.status == "PENDING" and row.next_attempt_at == w.clock["at"] + timedelta(seconds=300)
    assert not deliver_alert(row.pk, now=w.clock["at"] + timedelta(seconds=299))
    monkeypatch.setattr(bot_api, "send_message", alerts.TEST_SEND)
    assert deliver_alert(row.pk, now=row.next_attempt_at)


@pytest.mark.parametrize("category", ["forbidden", "chat_not_found"])
def test_unreachable_private_chats_fail_without_fallback(
    configurable_world, alerts, monkeypatch, category
):
    _, row = opening(configurable_world)
    monkeypatch.setattr(
        bot_api, "send_message", lambda *_: bot_api.TelegramResult(False, category=category)
    )
    assert not deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.status == "DEAD" and row.last_error_code == category.upper()
    assert not deliver_alert(row.pk)
    assert not alerts.CAPTURED_MESSAGES


def test_timeout_uncertain_no_blind_retry_and_audited_reconciliation(
    configurable_world, alerts, monkeypatch
):
    w, row = opening(configurable_world)
    transport = Mock(return_value=bot_api.TelegramResult(False, category="timeout", ambiguous=True))
    monkeypatch.setattr(bot_api, "send_message", transport)
    assert not deliver_alert(row.pk)
    assert not deliver_alert(row.pk, now=w.clock["at"] + timedelta(days=1))
    assert transport.call_count == 1
    row.refresh_from_db()
    assert row.status == "UNCERTAIN"
    assert (
        EconomicAlertEvent.objects.get(outcome="DELIVERY_RESULT").evidence["transport_code"]
        == "TIMEOUT"
    )
    with pytest.raises(ValidationError):
        review_delivery(
            row.pk, actor=w.actor, reference="checked", action="RETRY_DEAD", dry_run=False
        )
    with pytest.raises(ValidationError):
        review_delivery(
            row.pk, actor=w.actor, reference="checked", action="CONFIRM_DELIVERED", dry_run=False
        )
    preview = review_delivery(
        row.pk,
        actor=w.actor,
        reference="checked private history",
        action="CONFIRM_DELIVERED",
        message_id=81,
    )
    assert preview["dry_run"]
    row.refresh_from_db()
    assert row.status == "UNCERTAIN"
    review_delivery(
        row.pk,
        actor=w.actor,
        reference="checked private history",
        action="CONFIRM_DELIVERED",
        message_id=81,
        dry_run=False,
    )
    row.refresh_from_db()
    assert row.status == "DELIVERED" and row.telegram_message_id == 81
    assert EconomicAlertEvent.objects.filter(outcome="UNCERTAIN_REVIEW").count() == 1
    assert not deliver_alert(row.pk)


@pytest.mark.parametrize("action", ["CONFIRM_NOT_SENT_AND_RETRY", "ABANDON"])
def test_explicit_uncertain_recovery_options(configurable_world, alerts, monkeypatch, action):
    w, row = opening(configurable_world)
    monkeypatch.setattr(
        bot_api,
        "send_message",
        lambda *_: bot_api.TelegramResult(False, category="connection_error", ambiguous=True),
    )
    assert not deliver_alert(row.pk)
    review_delivery(
        row.pk,
        actor=w.actor,
        reference="Independent transport investigation",
        action=action,
        dry_run=False,
    )
    row.refresh_from_db()
    assert row.status == ("PENDING" if action == "CONFIRM_NOT_SENT_AND_RETRY" else "DEAD")


def test_worker_crash_after_send_is_uncertain_and_repeated_dispatch_cannot_resend(
    configurable_world, alerts, monkeypatch
):
    w, row = opening(configurable_world)

    def crash(*args):
        alerts.TEST_SEND(*args)
        raise SystemExit("Simulated worker termination after external acknowledgement")

    monkeypatch.setattr(bot_api, "send_message", crash)
    with pytest.raises(SystemExit):
        deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.status == "SENDING"
    assert not deliver_alert(row.pk)
    assert recover_expired_deliveries(now=row.lease_expires_at + timedelta(seconds=1)) == 1
    assert not deliver_alert(row.pk, now=w.clock["at"] + timedelta(days=1))
    row.refresh_from_db()
    assert row.status == "UNCERTAIN" and row.last_error_code == "SEND_LEASE_EXPIRED"
    assert len(alerts.CAPTURED_MESSAGES) == 1


def test_private_message_format_is_english_redacted_and_durable(configurable_world, alerts):
    _, row = opening(configurable_world)
    text = format_message(row.event)
    assert "Dexo Land — Economic Alert" in text and " UTC" in text
    assert "Transition: OPENED" in text and "Status: OPEN" in text
    assert "Severity: ERROR" in text and "Epoch: 1" in text
    assert "Action: Review the economic incident in Django Admin." in text
    assert str(row.telegram_user_id) not in text and "evidence" not in text.lower()
    assert all(ord(c) < 128 or c == "—" for c in text)
    assert len(text) < 4096


def test_uses_shared_client_admission_without_http_on_budget_denial(
    configurable_world, alerts, monkeypatch
):
    _, row = opening(configurable_world)
    alerts.TELEGRAM_BOT_TOKEN = "synthetic-test-token"
    budget = Mock(return_value=BudgetDenial("budget_unavailable", 31))
    http = Mock(side_effect=AssertionError("No real Telegram request allowed"))
    monkeypatch.setattr(bot_api, "send_message", ORIGINAL_SEND)
    monkeypatch.setattr(bot_api, "reserve_bot_api_budget", budget)
    monkeypatch.setattr("urllib.request.urlopen", http)
    assert not deliver_alert(row.pk)
    budget.assert_called_once_with(
        "sendMessage", fail_closed=True, chat_id=row.telegram_user_id, message_count=1
    )
    http.assert_not_called()
    row.refresh_from_db()
    assert row.status == "PENDING" and row.last_error_code == "BUDGET_UNAVAILABLE"


def test_test_delivery_is_permissioned_held_and_explicit(configurable_world, alerts):
    w, row = opening(configurable_world)
    actor = alerts.TEST_OPERATOR.user
    before = EconomicAlertEvent.objects.count()
    preview = prepare_test_delivery(row.event_id, actor=actor, reference="Authorized local test")
    assert preview["dry_run"] and preview["test_operator_id"] == alerts.TEST_OPERATOR.pk
    assert "Transition: TEST_REQUESTED" in preview["message"]
    assert "Status: TEST" in preview["message"]
    assert EconomicAlertEvent.objects.count() == before and not alerts.CAPTURED_MESSAGES
    result = prepare_test_delivery(
        row.event_id, actor=actor, reference="Authorized local test", dry_run=False
    )
    same = prepare_test_delivery(
        row.event_id, actor=actor, reference="Authorized local test", dry_run=False
    )
    assert result["delivery_ids"] == same["delivery_ids"]
    test = EconomicAlertDelivery.objects.get(pk=result["delivery_ids"][0])
    assert test.status == "HELD" and not deliver_alert(test.pk)
    with pytest.raises(PermissionDenied):
        send_test_delivery(test.pk, actor=w.actor, reference="Wrong operator")
    # Clear the earlier opening so the test observes the same event ordering barrier as real work.
    assert deliver_alert(row.pk)
    out = io.StringIO()
    call_command(
        "monitor_economics",
        "--test-event",
        str(row.event_id),
        "--actor",
        actor.username,
        "--reference",
        "Authorized local test",
        "--apply",
        "--send-test",
        stdout=out,
    )
    assert '"delivered": [true]' in out.getvalue()
    assert "Transition: TEST_REQUESTED" in alerts.CAPTURED_MESSAGES[-1]["text"]
    assert len(alerts.CAPTURED_MESSAGES) == 2
    assert not send_test_delivery(test.pk, actor=actor, reference="Same test again")
    with pytest.raises(CommandError):
        call_command("monitor_economics", "--send-test", stdout=io.StringIO())


def test_nonprivileged_test_and_transactional_send_fail_closed(configurable_world, alerts):
    _, row = opening(configurable_world)
    actor = alerts.TEST_OPERATOR.user
    actor.is_superuser = False
    actor.save()
    actor.user_permissions.add(
        permission("receive_economic_alert"),
        Permission.objects.get(codename="control_economic_automation"),
    )
    with pytest.raises(PermissionDenied):
        prepare_test_delivery(
            row.event_id, actor=actor, reference="Missing test permission", dry_run=False
        )
    with transaction.atomic(), pytest.raises(RuntimeError, match="outside"):
        deliver_alert(row.pk)
    assert not alerts.CAPTURED_MESSAGES


def test_pinned_target_guards_history_protection_and_no_channel_dependencies(
    configurable_world, alerts
):
    _, row = opening(configurable_world)
    for update in ({"telegram_user_id": -7}, {"operator_id": None}, {"channel": "LEGACY"}):
        with pytest.raises(DatabaseError), transaction.atomic():
            EconomicAlertDelivery.objects.filter(pk=row.pk).update(**update)
    with pytest.raises(DatabaseError), transaction.atomic():
        EconomicAlertDelivery.objects.filter(pk=row.pk).delete()
    module = import_module("apps.arena.migrations.0031_private_telegram_economic_alerts")
    from django.apps import apps

    with pytest.raises(IrreversibleError, match="Telegram alert history"):
        module.protect_telegram_history(apps, type("Editor", (), {"connection": connection})())
    for file in ("apps/arena/alerts.py", "apps/arena/alert_delivery.py"):
        content = Path(file).read_text()
        assert "django.core.mail" not in content and "from_email" not in content
    from django.conf import settings

    assert not hasattr(settings, "ECONOMIC_ALERT_EMAIL_BACKEND")
    assert not hasattr(settings, "ECONOMIC_ALERT_ROUTES")


def test_two_reachable_admins_receive_once_each(configurable_world, alerts, player_factory):
    w = configurable_world()
    second = User.objects.create_superuser("second-reachable-auditor")
    player = player_factory(771005)
    player.first_private_contact_at = player.first_seen_at
    player.save(update_fields=("first_private_contact_at",))
    BotAdminOperator.objects.create(
        user=second, telegram_user_id=player.telegram_user_id, enabled=True
    )
    overdue(w)
    scan_alerts()
    event = EconomicAlertEvent.objects.get(outcome="OPENED")
    ids = materialize_event(event)
    assert len(ids) == 2 and ids == materialize_event(event)
    for pk in ids:
        assert deliver_alert(pk)
        assert not deliver_alert(pk)
    assert len(alerts.CAPTURED_MESSAGES) == 2
    assert {x["chat_id"] for x in alerts.CAPTURED_MESSAGES} == {
        player.telegram_user_id,
        alerts.TEST_OPERATOR.telegram_user_id,
    }


@pytest.mark.parametrize("outcome", ["OPENED", "REOPENED", "SEVERITY_CHANGED", "RESOLVED"])
def test_all_incident_transitions_have_durable_english_messages(
    configurable_world, alerts, outcome
):
    _, row = opening(configurable_world)
    assert deliver_alert(row.pk)
    event = EconomicAlertEvent.objects.create(
        alert_id=row.event.alert_id, outcome=outcome, severity="CRITICAL", evidence={}
    )
    pk = materialize_event(event)[0]
    assert deliver_alert(pk)
    text = alerts.CAPTURED_MESSAGES[-1]["text"]
    assert f"Transition: {outcome}" in text and "Severity: CRITICAL" in text
    assert f"Event ID: {event.pk}" in text
    assert "Status: RESOLVED" in text if outcome == "RESOLVED" else "Status: OPEN" in text


def test_actual_shared_client_429_is_persisted_without_network(
    configurable_world, alerts, monkeypatch
):
    import json
    import urllib.error

    _, row = opening(configurable_world)
    alerts.TELEGRAM_BOT_TOKEN = "synthetic-test-token"
    denial = urllib.error.HTTPError(
        "https://synthetic.invalid/",
        429,
        "Rate limited",
        HTTPMessage(),
        io.BytesIO(json.dumps({"parameters": {"retry_after": 120}}).encode()),
    )
    monkeypatch.setattr(bot_api, "send_message", ORIGINAL_SEND)
    monkeypatch.setattr(bot_api, "reserve_bot_api_budget", lambda *_, **__: None)
    pause = Mock()
    monkeypatch.setattr(bot_api, "defer_bot_api", pause)
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=denial))
    assert not deliver_alert(row.pk)
    row.refresh_from_db()
    assert row.status == "PENDING" and row.last_error_code == "RATE_LIMITED"
    pause.assert_called_once_with(120)


def test_recovery_waits_for_uncertain_opening(configurable_world, alerts, monkeypatch):
    from apps.arena.automation import run_weekly_lifecycle

    w, row = opening(configurable_world)
    monkeypatch.setattr(
        bot_api,
        "send_message",
        lambda *_: bot_api.TelegramResult(False, category="timeout", ambiguous=True),
    )
    assert not deliver_alert(row.pk)
    run_weekly_lifecycle(w.season)
    scan_alerts()
    recovery = EconomicAlertDelivery.objects.get(event__outcome="RESOLVED")
    assert not deliver_alert(recovery.pk)
    review_delivery(
        row.pk,
        actor=w.actor,
        reference="Opening ambiguity reviewed and abandoned",
        action="ABANDON",
        dry_run=False,
    )
    monkeypatch.setattr(bot_api, "send_message", alerts.TEST_SEND)
    assert deliver_alert(recovery.pk)


def test_old_delivery_history_survives_real_forward_migration(configurable_world):
    from django.db.migrations.executor import MigrationExecutor

    w = configurable_world()
    before = DlxAllocation.objects.count()
    executor = MigrationExecutor(connection)
    old = [("arena", "0030_protect_feature_history_on_reverse")]
    executor.migrate(old)
    historical = executor.loader.project_state(old).apps
    try:
        incident = historical.get_model("arena", "EconomicAlert").objects.create(
            fingerprint="a" * 64,
            season_id=w.season.pk,
            epoch_id=w.epochs[0].pk,
            code="EPOCH_FREEZE_OVERDUE",
            severity="ERROR",
            status="OPEN",
            evidence={},
            opened_at=w.clock["at"],
            last_seen_at=w.clock["at"],
        )
        event = historical.get_model("arena", "EconomicAlertEvent").objects.create(
            alert_id=incident.pk, outcome="OPENED", severity="ERROR", evidence={}
        )
        legacy = historical.get_model("arena", "EconomicAlertDelivery")
        snapshot = {
            "historical_transport": "retired",
            "opaque_destination": "historical-protected-value",
        }
        pending = legacy.objects.create(
            event_id=event.pk,
            route_key="historical-pending",
            route_config=snapshot,
            next_attempt_at=w.clock["at"],
        )
        sent = legacy.objects.create(
            event_id=event.pk,
            route_key="historical-delivered",
            route_config=snapshot,
            next_attempt_at=w.clock["at"],
            status="DELIVERED",
            delivered_at=w.clock["at"],
            attempts=1,
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    pending = EconomicAlertDelivery.objects.get(pk=pending.pk)
    sent = EconomicAlertDelivery.objects.get(pk=sent.pk)
    assert pending.channel == sent.channel == "LEGACY"
    assert pending.route_config == sent.route_config == snapshot
    assert pending.status == "DEAD" and pending.last_error_code == "LEGACY_TRANSPORT_DISABLED"
    assert sent.status == "DELIVERED" and sent.attempts == 1 and sent.delivered_at == w.clock["at"]
    assert (
        EconomicAlertEvent.objects.filter(outcome="CHANNEL_RETIRED").get().evidence["delivery_id"]
        == pending.pk
    )
    assert DlxAllocation.objects.count() == before
    with pytest.raises(DatabaseError), transaction.atomic():
        EconomicAlertDelivery.objects.filter(pk=pending.pk).update(status="PENDING")


def test_real_downgrade_refuses_private_history_before_any_ddl(configurable_world, alerts):
    from django.db.migrations.executor import MigrationExecutor

    _, row = opening(configurable_world)
    executor = MigrationExecutor(connection)
    with pytest.raises(IrreversibleError, match="Private Telegram alert history"):
        executor.migrate([("arena", "0030_protect_feature_history_on_reverse")])
    row.refresh_from_db()
    assert row.channel == "TELEGRAM_PRIVATE"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations WHERE app='arena' "
            "AND name='0031_private_telegram_economic_alerts'"
        )
        assert cursor.fetchone()[0] == 1


def test_recipient_identity_correction_cannot_duplicate_same_private_event(
    configurable_world, alerts
):
    _, row = opening(configurable_world)
    op = alerts.TEST_OPERATOR
    BotAdminOperator.objects.filter(pk=op.pk).update(telegram_user_id=771006)
    replacement = User.objects.create_superuser("replacement-mapped-operator")
    BotAdminOperator.objects.create(
        user=replacement, telegram_user_id=row.telegram_user_id, enabled=True
    )
    ids = materialize_event(row.event)
    assert set(ids) == {row.pk}
    assert EconomicAlertDelivery.objects.count() == 1
    assert not deliver_alert(row.pk)  # Original pinned authority no longer matches.
    assert not alerts.CAPTURED_MESSAGES
