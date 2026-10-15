"""Real PostgreSQL paired-device ownership and receipt regressions."""

# mypy: disable-error-code="no-untyped-def"
from datetime import timedelta
from uuid import uuid4

import pytest

from apps.base_tap.tests.test_v2 import AT, batch, world  # noqa: F401, F811
from apps.base_tap.v2 import TapError, issue_lease, release_lease, state_snapshot, submit_batch

pytestmark = pytest.mark.django_db(transaction=True)


def test_device_switch_fences_old_and_requires_intent_even_after_expiry(world):  # noqa: F811
    player, phone, state, _, _ = world
    desktop = uuid4()
    with pytest.raises(TapError, match="ACTIVE_SESSION"):
        issue_lease(player, session=desktop, at=AT)
    assert state_snapshot(player, session=desktop, at=AT)["ownership"]["status"] == "ANOTHER_DEVICE"
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    assert switched["burst_remaining"] == 50
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        submit_batch(player, batch(state), at=AT)
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        issue_lease(player, session=phone, at=AT + timedelta(seconds=121))
    recovered = issue_lease(
        player,
        session=phone,
        at=AT + timedelta(seconds=121),
        takeover=True,
        expected_generation=switched["generation"],
    )
    assert recovered["ownership"]["status"] == "OWNED"


def test_stale_release_cannot_revoke_successor_and_receipt_survives(world):  # noqa: F811
    player, phone, state, _, _ = world
    receipt = submit_batch(player, batch(state, count=7), at=AT)
    desktop = uuid4()
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    old = state["lease"]
    release_lease(
        player, lease_id=old["lease_id"], session=phone, generation=old["generation"], at=AT
    )
    assert (
        state_snapshot(player, session=desktop, at=AT)["lease"]["lease_id"]
        == switched["lease"]["lease_id"]
    )
    replay = submit_batch(player, batch(state, count=7), at=AT)
    assert replay["duplicate"] and replay["receipt"] == receipt["receipt"]
    assert replay["snapshot"]["progression"]["lifetime_points"] == 7
    own = switched["lease"]
    release_lease(
        player, lease_id=own["lease_id"], session=desktop, generation=own["generation"], at=AT
    )
    regained = issue_lease(
        player, session=phone, at=AT, takeover=True, expected_generation=switched["generation"]
    )
    assert regained["burst_remaining"] == 43


def test_export_session_fixture(world):  # noqa: F811
    import json
    import os
    from pathlib import Path

    from apps.base_tap.schemas import TapStateOutput

    player, phone, state, _, _ = world
    desktop = uuid4()
    blocked = state_snapshot(player, session=desktop, at=AT)
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    moved = state_snapshot(player, session=phone, at=AT)
    fixture = {
        "provenance": "Synthetic PostgreSQL test output; not production balances",
        "initial": TapStateOutput.model_validate(state).model_dump(mode="json"),
        "another_device": TapStateOutput.model_validate(blocked).model_dump(mode="json"),
        "switch_request": {
            "session_id": str(desktop),
            "takeover": True,
            "expected_generation": state["generation"],
        },
        "switch_response": TapStateOutput.model_validate(switched).model_dump(mode="json"),
        "moved": TapStateOutput.model_validate(moved).model_dump(mode="json"),
    }
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/session-switch.json").write_text(
            json.dumps(fixture, indent=2) + "\n"
        )
