from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.utils import timezone

from apps.seasons.close import _telegram
from apps.telegram.models import TelegramDeadLetterDisposition, TelegramInboundUpdate
from apps.telegram.services import resolve_dead_letter


def dead_letter(update_id: int = 99001) -> TelegramInboundUpdate:
    return TelegramInboundUpdate.objects.create(
        update_id=update_id,
        payload={"update_id": update_id},
        state=TelegramInboundUpdate.State.DEAD_LETTER,
        processing_attempts=9,
        last_error_category="poison",
        payload_expires_at=timezone.now() + timedelta(days=1),
    )


@pytest.mark.django_db
def test_pre_cutoff_dead_letter_is_manual_not_transient(
    phase4_config_factory: Any, season_factory: Any
) -> None:
    row = dead_letter()
    evidence = _telegram(
        season_factory(phase4_config_factory()), row.received_at + timedelta(seconds=1)
    )
    assert evidence.blocking
    assert evidence.unresolved_review_count == 1
    assert evidence.safe_terminal_state == "MANUAL_REVIEW"
    assert evidence.watermark["transient_count"] == 0


@pytest.mark.django_db
def test_authorized_requeue_is_audited_idempotent_and_dispatches_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    row = dead_letter()
    actor = User.objects.create_user("operator")
    actor.user_permissions.add(Permission.objects.get(codename="resolve_dead_letter"))
    dispatched: list[int] = []
    monkeypatch.setattr(
        "apps.telegram.services.dispatch_inbound_update",
        lambda inbound: dispatched.append(inbound.pk),
    )

    def resolve() -> TelegramDeadLetterDisposition:
        return resolve_dead_letter(
            row.pk,
            outcome=TelegramDeadLetterDisposition.Outcome.REQUEUE,
            actor=actor,
            reason="fixed parser",
            evidence_reference="OPS-11",
            idempotency_key="dead-letter:99001:requeue:1",
        )

    first = resolve()
    second = resolve()
    row.refresh_from_db()
    assert first.pk == second.pk
    assert TelegramDeadLetterDisposition.objects.count() == 1
    assert row.state == TelegramInboundUpdate.State.PENDING
    assert row.processing_attempts == 0
    assert first.processing_attempts == 9 and first.failure_category == "poison"
    assert dispatched == [row.pk]


@pytest.mark.django_db
def test_unauthorized_dead_letter_disposition_fails_closed() -> None:
    row = dead_letter()
    with pytest.raises(PermissionDenied):
        resolve_dead_letter(
            row.pk,
            outcome=TelegramDeadLetterDisposition.Outcome.DISPOSE,
            actor=User.objects.create_user("unprivileged"),
            reason="reviewed",
            evidence_reference="OPS-12",
            idempotency_key="dead-letter:99001:dispose:1",
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_requeue_identity_dispatches_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    row = dead_letter(99002)
    actor = User.objects.create_user("concurrent-operator")
    actor.user_permissions.add(Permission.objects.get(codename="resolve_dead_letter"))
    dispatched: list[int] = []
    monkeypatch.setattr(
        "apps.telegram.services.dispatch_inbound_update",
        lambda inbound: dispatched.append(inbound.pk),
    )

    def worker() -> int:
        close_old_connections()
        try:
            return resolve_dead_letter(
                row.pk,
                outcome=TelegramDeadLetterDisposition.Outcome.REQUEUE,
                actor=User.objects.get(pk=actor.pk),
                reason="fixed parser",
                evidence_reference="OPS-13",
                idempotency_key="dead-letter:99002:requeue:1",
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: worker(), range(2)))
    assert ids[0] == ids[1]
    assert TelegramDeadLetterDisposition.objects.filter(inbound_update=row).count() == 1
    assert dispatched == [row.pk]
