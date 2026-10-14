"""Contract gate must reject the integration gaps this compatibility patch fixes."""

import json
from typing import Any

import pytest

from scripts.validate_contract import validate_files, validate_tap_wire


def test_committed_wire_contract_passes_semantic_gate() -> None:
    contents, errors = validate_files()
    assert not errors
    assert not validate_tap_wire(contents)


@pytest.mark.parametrize("defect", ["cooldown", "receipt", "partial", "recovery", "daily"])
def test_wire_gate_rejects_broken_authoritative_evidence(defect: str) -> None:
    contents, _ = validate_files()
    fixture: Any = json.loads(contents["docs/fixtures/tap-v2.json"])
    openapi: Any = json.loads(contents["docs/fixtures/tap-v2-openapi.json"])
    if defect == "cooldown":
        del openapi["components"]["schemas"]["LeaseOutput"]["properties"]["cooldown_seconds"]
    elif defect == "receipt":
        openapi["components"]["schemas"]["TapReceiptOutput"]["required"].remove("idempotency_key")
    elif defect == "partial":
        fixture["partial_response"]["receipt"]["rejected_taps"] = 0
    elif defect == "recovery":
        fixture["partial_recovery_response"]["receipt"] = fixture["batch_response"]["receipt"]
    else:
        fixture["state_after_daily_reward"]["receipt_cursor"] = fixture["batch_response"][
            "snapshot"
        ]["receipt_cursor"]
    contents["docs/fixtures/tap-v2.json"] = json.dumps(fixture)
    contents["docs/fixtures/tap-v2-openapi.json"] = json.dumps(openapi)
    assert validate_tap_wire(contents)
