from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ResetPeriod:
    start: datetime
    end: datetime
    key: str
    previous_start: datetime


def resolve_daily_period(
    at: datetime, timezone_name: str, reset_hour: int = 0, reset_minute: int = 0
) -> ResetPeriod:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Reset periods require an aware timestamp")
    zone = ZoneInfo(timezone_name)
    local = at.astimezone(zone)
    boundary = datetime.combine(local.date(), time(reset_hour, reset_minute), zone)
    if local < boundary:
        boundary = datetime.combine(
            local.date() - timedelta(days=1), time(reset_hour, reset_minute), zone
        )
    next_boundary = datetime.combine(
        boundary.date() + timedelta(days=1), time(reset_hour, reset_minute), zone
    )
    previous = datetime.combine(
        boundary.date() - timedelta(days=1), time(reset_hour, reset_minute), zone
    )
    start = boundary.astimezone(UTC)
    return ResetPeriod(
        start, next_boundary.astimezone(UTC), start.isoformat(), previous.astimezone(UTC)
    )
