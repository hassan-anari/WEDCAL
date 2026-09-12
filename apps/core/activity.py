"""Small shared qualification primitives for Telegram activity contexts."""

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime


def fingerprints(text: str) -> tuple[str, str]:
    exact = unicodedata.normalize("NFC", text)
    normalized = " ".join(exact.casefold().split())
    return hashlib.sha256(exact.encode()).hexdigest(), hashlib.sha256(
        normalized.encode()
    ).hexdigest()


def window_start_for(at: datetime, seconds: int) -> datetime:
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=at.tzinfo)


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
