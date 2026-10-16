"""Synthetic subprocess worker used only by B4 latency tests; never a deployment entrypoint."""

import json
import os
import sys
import urllib.request
from pathlib import Path
from time import perf_counter


def main() -> None:
    if os.environ.get("B4_TEST_TRANSPORT") != "isolated-synthetic-only" or not os.environ.get(
        "POSTGRES_DB", ""
    ).startswith("test_"):
        raise RuntimeError("Isolated test database and transport are required")
    queue, output = sys.argv[1:]
    if not queue.startswith("b4-interactive-test-"):
        raise RuntimeError("Dedicated test queue required")
    os.environ["DJANGO_SETTINGS_MODULE"] = "dexo_land.settings.test"
    import django

    django.setup()
    from celery.signals import worker_ready
    from django.conf import settings
    from django.core.cache import cache
    from django.db import connection

    from dexo_land.celery import app

    cache.clear()  # The worker requires a test database and isolated test infrastructure.
    settings.TELEGRAM_BOT_TOKEN = "1:isolated"  # Synthetic only; HTTP is intercepted below.
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True
    settings.B3_BOT_WELCOME_ENABLED = False
    settings.B4_PUBLIC_WELCOME_ENABLED = False
    settings.TELEGRAM_ADMIN_IDS = {550001}
    settings.NOTIFICATION_INTERACTIVE_QUEUE = queue
    app.conf.worker_enable_remote_control = False

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self, limit):  # type: ignore[no-untyped-def]
            return b'{"ok":true,"result":{"message_id":1}}'

    def transport(url, data=None, timeout=5, *, context=None):  # type: ignore[no-untyped-def]
        request = url
        prefix = "https://api.telegram.org/bot1:isolated/"
        if not request.full_url.startswith(prefix):
            raise AssertionError("Unexpected external URL")
        method = request.full_url.removeprefix(prefix)
        payload = json.loads(request.data)
        if connection.in_atomic_block or method not in {"sendMessage", "answerCallbackQuery"}:
            raise AssertionError("Unexpected transport operation")
        if method == "sendMessage" and payload.get("chat_id") != 550001:
            raise AssertionError("Only the synthetic test actor may receive a message")
        # Real request/budgets/retry logic runs; only HTTP response bytes are synthetic.
        data = (json.dumps({"method": method, "at": perf_counter()}) + "\n").encode()
        descriptor = os.open(output, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        return Response()

    urllib.request.urlopen = transport

    @worker_ready.connect(weak=False)
    def ready(**kwargs):  # type: ignore[no-untyped-def]
        Path(output + ".ready").touch()

    app.worker_main(
        [
            "worker",
            "--pool=prefork",
            "--concurrency=2",
            f"--queues={queue}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        ]
    )


if __name__ == "__main__":
    main()
