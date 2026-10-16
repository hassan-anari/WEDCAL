# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Real local RabbitMQ/Celery/PostgreSQL; transport is synthetic and never contacts Telegram."""

import json
import math
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter, sleep
from uuid import uuid4

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.db import connection

from apps.notifications.models import BotAdminInteraction, BotAdminOperator, TelegramDelivery
from apps.notifications.tests.test_b3_bot_admin import callback, message
from apps.telegram.tasks import process_telegram_update

pytestmark = pytest.mark.django_db(transaction=True)


def test_webhook_to_celery_reply_and_callback_p95_under_five_seconds(
    client, settings, monkeypatch, tmp_path
):
    cache.clear()  # The Redis database is isolated synthetic test infrastructure.
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True
    settings.B3_BOT_WELCOME_ENABLED = False
    settings.TELEGRAM_WEBHOOK_SECRET = "isolated-b4-webhook"
    settings.TELEGRAM_ADMIN_IDS = {550001}
    operator = User.objects.create_user("latency-operator")
    operator.user_permissions.add(Permission.objects.get(codename="manage_broadcasts"))
    BotAdminOperator.objects.create(user=operator, telegram_user_id=550001, enabled=True)

    def forbidden(*args):
        raise AssertionError("Webhook ingestion must not send Telegram requests")

    monkeypatch.setattr("apps.telegram.bot_api.request", forbidden)
    monkeypatch.setattr("apps.telegram.bot_api.send_message", forbidden)
    queue = "b4-interactive-test-" + uuid4().hex
    # Route both real tasks to a unique test queue; never consume unrelated queues.
    settings.NOTIFICATION_INTERACTIVE_QUEUE = queue
    for task in (process_telegram_update,):
        original = task.apply_async

        def route(*args, _original=original, **kwargs):
            kwargs["queue"] = queue
            return _original(*args, **kwargs)

        monkeypatch.setattr(task, "apply_async", route)
    ack_ms, reply_ms = [], []

    def post(payload):
        started = perf_counter()
        response = client.post(
            "/api/v1/telegram/webhook",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=settings.TELEGRAM_WEBHOOK_SECRET,
        )
        ack_ms.append((perf_counter() - started) * 1000)
        assert response.status_code == 200, response.content
        return started

    evidence_file = tmp_path / "transport.jsonl"
    cursor = 0

    def receive():
        nonlocal cursor
        deadline = perf_counter() + 5
        while perf_counter() < deadline:
            rows = evidence_file.read_text().splitlines() if evidence_file.exists() else []
            if len(rows) > cursor:
                value = json.loads(rows[cursor])
                cursor += 1
                return value["method"], value["at"]
            sleep(0.02)
        raise AssertionError(list(TelegramDelivery.objects.values("id", "state", "error_category")))

    @contextmanager
    def worker():
        environment = dict(os.environ)
        environment.update(
            {
                "POSTGRES_DB": connection.settings_dict["NAME"],
                "B4_TEST_TRANSPORT": "isolated-synthetic-only",
            }
        )
        with (tmp_path / "worker.log").open("w") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "apps.notifications.tests.b4_worker",
                    queue,
                    str(evidence_file),
                ],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = perf_counter() + 20
                while not Path(str(evidence_file) + ".ready").exists():
                    assert process.poll() is None, (tmp_path / "worker.log").read_text()
                    assert perf_counter() < deadline, "Isolated worker startup exceeded 20 seconds"
                    sleep(0.05)
                yield
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise AssertionError("Isolated worker failed warm shutdown") from None

    with worker():
        for index in range(12):
            started = post(message(800000 + index, user_id=550001))
            method, delivered = receive()
            assert method == "sendMessage"
            reply_ms.append((delivered - started) * 1000)
        count = TelegramDelivery.objects.count()
        post(message(800011, user_id=550001))  # Identical webhook replay creates no reply.
        assert TelegramDelivery.objects.count() == count
        token = (
            BotAdminInteraction.objects.filter(action="compose", consumed_at__isnull=True)
            .order_by("-id")
            .first()
        )
        assert token
        started = post(callback(800012, token.token, user_id=550001))
        results = [receive(), receive()]
        callback_ms = next(
            (at - started) * 1000 for method, at in results if method == "answerCallbackQuery"
        )
        assert {method for method, _ in results} == {"sendMessage", "answerCallbackQuery"}
        before = TelegramDelivery.objects.count()
        post(message(800013, user_id=550002))  # A non-operator cannot get admin messages.
        assert TelegramDelivery.objects.count() == before
        from apps.telegram.models import TelegramInboundUpdate

        deadline = perf_counter() + 5
        while not TelegramInboundUpdate.objects.filter(
            update_id=800013, state="PROCESSED"
        ).exists():
            assert perf_counter() < deadline
            sleep(0.02)
        assert len(evidence_file.read_text().splitlines()) == cursor

    def p95(values):
        return sorted(values)[math.ceil(0.95 * len(values)) - 1]

    evidence = {
        "environment": "isolated PostgreSQL 17 / Redis budgets / RabbitMQ / Celery; synthetic HTTP",
        "samples": len(reply_ms),
        "reply_ms": reply_ms,
        "reply_p95_ms": p95(reply_ms),
        "webhook_ack_p95_ms": p95(ack_ms),
        "callback_ack_ms": callback_ms,
        "duplicate_or_unauthorized_sends": 0,
        "production_or_real_user_contacts": 0,
        "limits": "Not a production, WAN, Telegram API or physical-device latency certification.",
    }
    Path("work").mkdir(exist_ok=True)
    Path("work/b4-interactive-latency.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert evidence["reply_p95_ms"] < 5000
    assert callback_ms < 5000 and evidence["webhook_ack_p95_ms"] < 1000
