"""Small shared qualification primitives for Telegram activity contexts."""

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from django.db import connection


def fingerprints(text: str) -> tuple[str, str]:
    exact = unicodedata.normalize("NFC", text)
    normalized = " ".join(exact.casefold().split())
    return hashlib.sha256(exact.encode()).hexdigest(), hashlib.sha256(
        normalized.encode()
    ).hexdigest()


def window_start_for(at: datetime, seconds: int) -> datetime:
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=at.tzinfo)


def lock_activity_window(membership_id: int, config_id: int, start: datetime) -> None:
    """Serialize ingestion/finalization for one durable economic window."""
    digest = hashlib.sha256(
        f"clan:{membership_id}:{config_id}:{start.isoformat()}".encode()
    ).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def lock_activity_qualification(clan_season_id: int, sender_id: int) -> None:
    """Serialize every horizon-dependent qualification for one Clan sender."""
    digest = hashlib.sha256(f"clan-qualification:{clan_season_id}:{sender_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


@dataclass(frozen=True, slots=True)
class Qualification:
    accepted: bool
    suspicious: bool
    rewardable_chars: int


def qualify_text(
    *,
    text: str,
    eligible: bool,
    min_chars: int,
    max_chars: int,
    rate_count: int,
    rate_limit: int,
    duplicate_count: int,
    suspicious_count: int,
    reject_count: int,
) -> Qualification:
    accepted = bool(
        eligible
        and len(text) >= min_chars
        and rate_count < rate_limit
        and duplicate_count < reject_count
    )
    return Qualification(
        accepted,
        accepted and duplicate_count >= suspicious_count,
        min(len(text), max_chars) if accepted else 0,
    )
