#!/usr/bin/env python3
"""Validate durable, semantic markers in the repository contract files."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = (
    "AGENTS.md",
    "DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md",
    "README.md",
    "DEXO_LAND_SEASON1_ECONOMY_CONFIG_CONTRACT.md",
    "config/examples/season1.game-config.v1.json",
    "config/examples/season1.game-config.v2.json",
)


def normalized(text: str) -> str:
    """Make prose checks insensitive to case, punctuation, and whitespace."""
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def has_all(text: str, *markers: str) -> bool:
    normalized_text = normalized(text)
    return all(normalized(marker) in normalized_text for marker in markers)


def validate_files() -> tuple[dict[str, str], list[str]]:
    contents: dict[str, str] = {}
    errors: list[str] = []

    for relative_path in REQUIRED_FILES:
        path = REPOSITORY_ROOT / relative_path
        if not path.is_file():
            errors.append(f"Missing required contract file: {relative_path}")
            continue

        content = path.read_text(encoding="utf-8")
        if not content.strip():
            errors.append(f"Required contract file is empty: {relative_path}")
            continue

        contents[relative_path] = content

    return contents, errors


def validate_invariants(contents: dict[str, str]) -> list[str]:
    errors: list[str] = []
    agents = contents.get("AGENTS.md", "")
    source = contents.get("DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md", "")
    economy = contents.get("DEXO_LAND_SEASON1_ECONOMY_CONFIG_CONTRACT.md", "")
    example = contents.get("config/examples/season1.game-config.v1.json", "")

    checks = (
        (
            agents,
            ("every development phase", "hard gated delivery unit"),
            "AGENTS.md must retain the mandatory phase delivery gate.",
        ),
        (
            agents,
            ("every phase", "GitHub Actions coverage"),
            "AGENTS.md must require GitHub Actions coverage for every phase.",
        ),
        (
            agents,
            ("required checks", "green", "coding agent", "stop"),
            "AGENTS.md must require coding agents to stop after a green PR.",
        ),
        (
            agents,
            ("next phase", "repository owner", "explicitly authorizes"),
            "AGENTS.md must require explicit owner authorization before the next phase.",
        ),
        (
            agents,
            ("canonical product contract", "DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md"),
            "AGENTS.md must reference the canonical Source of Truth.",
        ),
        (
            source,
            (
                "confirmed",
                "off-chain",
                "DLX allocations",
                "not claimable",
                "transferable",
                "tradable",
            ),
            "Source of Truth must distinguish confirmed off-chain allocation from liquid DLX.",
        ),
        (
            source,
            ("45 days", "six economic epochs", "three-day", "no seventh"),
            "Source of Truth must retain the 45-day, six-epoch timing.",
        ),
        (
            source,
            ("No War/Defence backend development in Season 1",),
            "Source of Truth must keep War/Defence outside Season 1 backend scope.",
        ),
        (
            source,
            ("GitHub Actions", "PR quality gate"),
            "Source of Truth must retain the GitHub Actions / PR quality gate.",
        ),
        (
            source,
            ("60,000,000", "100,000,000", "settlement_start_epoch", "chronologically"),
            "Source of Truth must retain pool growth and delayed-settlement semantics.",
        ),
        (
            economy,
            ("3500 bps", "Core Gameplay 2000 bps", "1500 bps", "1000 bps", "10,000 bps"),
            "Economy contract must retain canonical category weights.",
        ),
        (
            source,
            ("BASE_TAP", "ineligible", "passive referral share", "batched"),
            "Source of Truth must retain Base Tap batching and referral ineligibility.",
        ),
        (
            source,
            (
                "Phase 9",
                "after Clan economy inputs exist",
                "off-chain DLX allocation ledger",
                "growth-unlock",
                "carry-forward",
            ),
            "Source of Truth must assign weekly DLX settlement ownership after Clan economy.",
        ),
        (
            example,
            (
                '"schema_version": 2',
                '"epoch_count": 6',
                '"price_status": "UNAVAILABLE"',
                '"accounting_unit": "OFF_CHAIN_DLX_ALLOCATION_AMOUNT"',
                '"base_dlx_amount": 60000000',
            ),
            "Example config must use economy schema v2 and Season 1 DLX semantics.",
        ),
    )

    for text, markers, message in checks:
        if not has_all(text, *markers):
            errors.append(message)

    if has_all(source, "DLX distribution during each season"):
        errors.append("Source of Truth contains stale per-season DLX distribution wording.")
    if "atomic_units" in example:
        errors.append("Example config must not imply finalized native-chain atomic units.")

    try:
        payload = json.loads(example)
        distribution = payload["dlx_distribution"]
        timing = distribution["timing"]
        dlx = payload["dlx"]
        typing = payload["typing"]
        base_tap = payload["base_tap"]
        weights = distribution["category_weights_bps"]
        if (
            timing["product_period_days"] != 45
            or timing["epoch_count"] != 6
            or timing["epoch_days"] != 7
            or timing["closing_days"] != 3
        ):
            errors.append(
                "Structured config must encode exactly 45 days, six 7-day epochs "
                "and 3 closing days."
            )
        if sum(weights.values()) != 10_000:
            errors.append("Structured DLX category weights must total exactly 10000 bps.")
        forbidden = (
            dlx["confirmed_allocation_claimable_transferable_or_tradable"],
            dlx["wallet_withdrawal_or_on_chain_claim_enabled"],
        )
        if (
            any(value is not False for value in forbidden)
            or distribution["accounting_unit"] != "OFF_CHAIN_DLX_ALLOCATION_AMOUNT"
        ):
            errors.append("Structured config must keep Season-1 DLX off-chain and non-liquid.")
        if "dlx" in base_tap:
            errors.append("Structured config must prohibit direct Base Tap DLX.")
        canonical_typing = {
            "completed_invalid_attempt_consumes_quota": True,
            "technical_failure_consumes_quota": False,
            "claimable_wallet_enabled": True,
            "claim_anytime": True,
            "auto_settle_at_season_close": True,
            "claim_preserves_source_season": True,
        }
        if any(typing.get(key) is not value for key, value in canonical_typing.items()):
            errors.append(
                "Structured config must retain canonical Season-1 Typing lifecycle flags."
            )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"Example config structural validation failed: {exc}")

    try:
        legacy = json.loads(example)
        current = json.loads(contents["config/examples/season1.game-config.v2.json"])
        tap = current["base_tap"]
        if current["meta"]["schema_version"] != 3 or current["meta"]["economy_contract_version"] != "3.0.0":
            errors.append("B2 config must use schema 3 / economy 3.0.0")
        if tap.get("protocol_version") != 2 or tap.get("rewarded_taps_per_burst") != 50 or tap.get("cooldown_seconds") != 10:
            errors.append("B2 initial burst policy must match owner 50/10 approval")
        if any(key in tap for key in ("rewarded_taps_per_utc_day", "reset_timezone")):
            errors.append("B2 cannot carry daily Tap earning/reset fields")
        if tap.get("grants_direct_dlx") is not False or tap.get("passive_referral_share_eligible") is not False:
            errors.append("B2 must retain direct DLX/referral exclusion")
        for key in legacy.keys() | current.keys():
            if key not in {"meta", "base_tap"} and legacy.get(key) != current.get(key):
                errors.append(f"B2 changed unrelated economy section: {key}")
        if not has_all(source, "no UTC daily Tap cap", "immediately", "Historical Tap protocol 1"):
            errors.append("B2 canonical Source of Truth must distinguish immediate uncapped Tap from history")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"B2 configuration structural validation failed: {exc}")
    return errors


def main() -> int:
    contents, errors = validate_files()
    errors.extend(validate_invariants(contents))

    if errors:
        print("Repository contract validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Repository contract validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
