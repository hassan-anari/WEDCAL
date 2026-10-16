"""Evidence for presentation only; all amounts come from committed canonical ledger rows."""

from typing import Any

from ninja import Schema

from apps.rewards.models import RewardLedger
from apps.rewards.schemas import BootstrapProgression


class ConfirmedReward(Schema):
    event_id: str
    amount: int
    ledger_ids: list[int]
    source: str
    created: bool
    progression_before: BootstrapProgression
    progression_after: BootstrapProgression


def confirmed_reward(
    *,
    rows: list[RewardLedger],
    event_id: str,
    source: str,
    created: bool,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any] | None:
    if not rows:
        return None
    return {
        "event_id": event_id,
        "amount": sum(row.player_amount for row in rows),
        "ledger_ids": [row.pk for row in rows],
        "source": source,
        "created": created,
        "progression_before": before["progression"],
        "progression_after": after["progression"],
    }
