from datetime import UTC, datetime

import pytest

from apps.core.periods import resolve_daily_period


def test_exact_reset_boundary() -> None:
    period = resolve_daily_period(datetime(2026, 1, 2, tzinfo=UTC), "UTC")
    assert period.start == datetime(2026, 1, 2, tzinfo=UTC)
    assert period.previous_start == datetime(2026, 1, 1, tzinfo=UTC)


def test_calendar_period_handles_dst_without_subtracting_24_hours() -> None:
    period = resolve_daily_period(datetime(2026, 3, 8, 6, 0, tzinfo=UTC), "America/New_York")
    assert (period.end - period.start).total_seconds() == 23 * 3600


def test_period_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        resolve_daily_period(datetime(2026, 1, 1), "UTC")
