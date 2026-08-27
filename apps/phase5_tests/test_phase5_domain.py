import json
from pathlib import Path
from typing import cast

import pytest
from django.core.exceptions import ValidationError

from apps.game_config.services import parse_phase5
from apps.network.services import fingerprints


@pytest.fixture
def phase5_payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(Path("config/examples/season1.game-config.v1.json").read_text()),
    )


def test_canonical_phase5_config_is_typed_without_floats(phase5_payload: dict[str, object]) -> None:
    referral, network, retention = parse_phase5(phase5_payload)
    assert referral.tiers[-1].share_bps == 700
    assert network.window_seconds == 900
    assert retention.message_content_days == 14


def test_referral_tier_thresholds_must_ascend(phase5_payload: dict[str, object]) -> None:
    phase5_payload["referral"]["tiers"][1]["min_verified_referrals"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        parse_phase5(phase5_payload)


def test_network_reject_threshold_must_exceed_suspicious(phase5_payload: dict[str, object]) -> None:
    phase5_payload["anti_fraud"]["network"]["same_fingerprint_reject_count"] = 2  # type: ignore[index]
    with pytest.raises(ValidationError):
        parse_phase5(phase5_payload)


def test_fingerprints_use_nfc_casefold_and_collapsed_whitespace() -> None:
    exact_one, normalized_one = fingerprints("  CAFÉ\tWorld ")
    exact_two, normalized_two = fingerprints("cafe\u0301 world")
    assert exact_one != exact_two
    assert normalized_one == normalized_two


def test_exact_fingerprint_uses_nfc() -> None:
    assert fingerprints("é")[0] == fingerprints("e\u0301")[0]
