"""Exercise the actual authenticated JSON transport, including lost response/restart recovery."""

import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.test import Client

from apps.base_tap.schemas import BatchOutput, TapStateOutput
from apps.base_tap.tests.test_v2 import AT, batch, world  # noqa: F401
from apps.base_tap.v2 import purge_expired_leases
from apps.rewards.models import RewardLedger


@pytest.mark.django_db
@pytest.mark.parametrize("retire", ["expired", "revoked", "purged"])
def test_wire_recovery_with_unrelated_daily_reward(
    world: Any,  # noqa: F811
    settings: Any,
    telegram_bot_token: str,
    init_data_factory: Any,
    monkeypatch: Any,
    retire: str,
) -> None:
    player, session, state, _, _ = world
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    now = [AT]
    monkeypatch.setattr("apps.base_tap.api.timezone.now", lambda: now[0])
    from apps.telegram.auth import verify_init_data

    monkeypatch.setattr(
        "apps.telegram.mini_app_auth.verify_init_data",
        lambda raw, **kwargs: verify_init_data(raw, now=now[0], **kwargs),
    )

    def client() -> Client:
        raw = init_data_factory(
            user={"id": player.telegram_user_id, "first_name": "Dexo"}, auth_date=now[0]
        )
        return Client(headers={"Authorization": f"tma {raw}"})

    def submit(payload: Any) -> Any:
        return client().post(
            "/api/v1/base/v2/tap-batches",
            data=payload.model_dump_json(),
            content_type="application/json",
        )

    def snapshot() -> Any:
        response = client().get("/api/v1/base/v2/tap-state", {"session_id": str(session)})
        assert response.status_code == 200
        TapStateOutput.model_validate(response.json())
        return response.json()

    def order(snapshot: Any) -> tuple[int, int]:
        return snapshot["receipt_cursor"], snapshot["revision"]

    # First batch is committed; its response can be delayed independently of its receipt.
    first_body = batch(state, count=30, key="persisted-first")
    first_response = submit(first_body)
    assert first_response.status_code == 200
    first = first_response.json()
    active = snapshot()
    daily = client().post("/api/v1/daily/claim")
    assert daily.status_code == 200
    after_daily = snapshot()
    assert after_daily["revision"] == active["revision"]
    assert order(after_daily) > order(active)
    assert after_daily["lease"] == active["lease"]
    assert after_daily["progression"]["lifetime_points"] == 30 + daily.json()["reward_amount"]
    # Cursor advanced for Daily, without acknowledging any new Tap intent.
    assert order(first["snapshot"]) < order(after_daily)

    now[0] += timedelta(milliseconds=100)
    partial_body = batch(state, count=25, start=31, key="persisted-partial")
    # Persist exact body/key before transmission; discard the successful response to model loss.
    persisted = partial_body.model_dump_json()
    lost = submit(partial_body)
    assert lost.status_code == 200
    partial = lost.json()
    assert partial["accepted_taps"] == 20 and partial["rejected_taps"] == 5
    assert partial["sequence_through"] == 50
    assert partial["receipt"]["requested_taps"] == 25
    assert partial["receipt"]["idempotency_key"] == "persisted-partial"
    assert partial["receipt"]["lease_id"] == str(first_body.lease_id)
    assert partial["receipt"]["sequence_start"] == 31
    before_count = RewardLedger.objects.count()
    now[0] = AT + timedelta(seconds=120 if retire == "expired" else 11)
    new_session = uuid4() if retire == "revoked" else session
    renewed = client().post(
        "/api/v1/base/v2/tap-leases",
        data=json.dumps({"session_id": str(new_session)}),
        content_type="application/json",
    )
    assert renewed.status_code == 200
    assert renewed.json()["generation"] > state["generation"]
    now[0] += timedelta(milliseconds=100)
    if retire == "purged":
        now[0] = AT + timedelta(days=8)
        purge_expired_leases(at=now[0])
    # A fresh process/client uses ONLY persisted original payload. Fresh lease state is irrelevant.
    recovered = client().post(
        "/api/v1/base/v2/tap-batches", data=persisted, content_type="application/json"
    )
    assert recovered.status_code == 200
    retry = recovered.json()
    BatchOutput.model_validate(retry)
    assert retry["duplicate"]
    assert retry["receipt"] == partial["receipt"]
    assert retry["accepted_taps"] == 20 and retry["rejected_taps"] == 5
    assert retry["snapshot"]["progression"]["lifetime_points"] == 50 + daily.json()["reward_amount"]
    assert order(retry["snapshot"]) >= order(partial["snapshot"])
    assert RewardLedger.objects.count() == before_count
    conflicting = partial_body.model_copy(update={"tap_count": 24})
    assert submit(conflicting).json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert RewardLedger.objects.count() == before_count


@pytest.mark.django_db(transaction=True)
def test_postgres_snapshot_waits_for_unrelated_daily_commit(
    world: Any,  # noqa: F811
    monkeypatch: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from django.db import connection, connections

    from apps.base_tap.v2 import state_snapshot, submit_batch
    from apps.daily import services as daily
    from apps.rewards.services import settle_player_reward

    player, session, state, _, _ = world
    first = submit_batch(player, batch(state), at=AT)["snapshot"]
    settled, release, reading = Event(), Event(), Event()
    reader_pid: list[int] = []

    def pause_before_commit(**kwargs: Any) -> Any:
        result = settle_player_reward(**kwargs)
        settled.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(daily, "settle_player_reward", pause_before_commit)

    def writer() -> Any:
        connections.close_all()
        try:
            return daily.claim_daily(player, AT)
        finally:
            connections.close_all()

    def reader() -> Any:
        connections.close_all()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                reader_pid.append(cursor.fetchone()[0])
            reading.set()
            return state_snapshot(player, at=AT, session=session)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert settled.wait(5)
        snapshot = pool.submit(reader)
        assert reading.wait(5)
        try:
            blocked = False
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [reader_pid[0]])
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                sleep(0.01)
            assert blocked, (
                "Snapshot must await Daily commit rather than mix cursor/projection states"
            )
        finally:
            release.set()
        claim, created = writing.result(timeout=10)
        after = snapshot.result(timeout=10)
    assert created
    assert after["receipt_cursor"] == claim.reward_ledger_id > first["receipt_cursor"]
    assert after["revision"] == first["revision"]
    assert after["lease"] == first["lease"]
    assert after["progression"]["lifetime_points"] == 1 + claim.reward_amount
