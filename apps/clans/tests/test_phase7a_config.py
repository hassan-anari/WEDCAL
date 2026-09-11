import json
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.game_config.services import parse_clan
from apps.telegram.services import membership_semantics

CANONICAL = Path(__file__).resolve().parents[3] / "config/examples/season1.game-config.v1.json"


def test_clan_lifecycle_policy_uses_canonical_config() -> None:
    policy = parse_clan(json.loads(CANONICAL.read_text()))
    assert (policy.enabled, policy.min_level, policy.base_capacity) == (True, 5, 20)
    assert (policy.leave_cooldown_hours, policy.max_voluntary_leaves_per_season) == (48, 3)
    assert policy.access_rejoin_grace_hours == 48


def test_clan_policy_fails_closed_on_malformed_values() -> None:
    payload = json.loads(CANONICAL.read_text())
    payload["clan"]["leave"]["cooldown_hours"] = 0
    with pytest.raises(ValidationError):
        parse_clan(payload)


@pytest.mark.parametrize(
    ("status", "is_member", "expected"),
    [
        ("creator", None, (True, True, True)),
        ("administrator", None, (True, True, False)),
        ("member", None, (True, False, False)),
        ("restricted", True, (True, False, False)),
        ("restricted", False, (False, False, False)),
        ("left", None, (False, False, False)),
    ],
)
def test_canonical_telegram_membership_semantics(status, is_member, expected) -> None:  # type: ignore[no-untyped-def]
    assert membership_semantics(status, is_member) == expected
