#!/usr/bin/env python3
"""Validate durable, semantic markers in the repository contract files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = (
    "AGENTS.md",
    "DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md",
    "README.md",
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
            ("DLX allocation does not occur after each season",),
            "Source of Truth must prohibit per-season final DLX allocation.",
        ),
        (
            source,
            ("Final DLX allocation happens only after the overall season campaign",),
            "Source of Truth must retain multi-season final DLX allocation timing.",
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
    )

    for text, markers, message in checks:
        if not has_all(text, *markers):
            errors.append(message)

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
