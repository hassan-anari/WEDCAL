"""Reproducible numeric inventory. Classification is a review aid, not a correctness proof."""

import argparse
import ast
import csv
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def classify(path: Path, line: str, value: Any) -> tuple[str, str, str]:
    text = str(path)
    if "migrations" in path.parts:
        return (
            "MIGRATION_HISTORY",
            "Applied schema/history must not be edited; see audit exceptions",
            "database schema",
        )
    if "settings" in path.parts or any(
        word in line
        for word in (
            "limit",
            "batch_size",
            "chunk_size",
            "retry",
            "timeout",
            "schedule",
            "max_retries",
        )
    ):
        return (
            "OPERATIONAL_BOUND",
            "Workload/timeout control, not an earning amount",
            "runtime/service argument",
        )
    if any(
        word in line
        for word in ("max_length", "min_length", "choices=", "TextChoices", "Field(", "Constraint")
    ):
        return "STRUCTURAL_BOUND", "Storage, protocol, shape or validation boundary", "code/schema"
    if value in {200, 204, 400, 401, 403, 404, 409, 422, 429, 500, 503} and any(
        word in text for word in ("api", "schema", "core")
    ):
        return "PROTOCOL", "HTTP status; no economic parameter", "protocol"
    if path.name == "services.py" and "game_config" in path.parts:
        return (
            "CONFIG_VALIDATION",
            "Validate domain input or preserve an immutable legacy parser",
            "refer to ownership matrix",
        )
    if value in {0, 1, 2, 100, 1000, 10000, 60, 24, 86400}:
        return (
            "ARITHMETIC_OR_BOUND",
            "Identity, index, denomination or unit conversion; inspect context",
            "code, unless listed in audit exceptions",
        )
    return (
        "REVIEW_CONTEXT",
        "Contextual non-reward literal; review with domain matrix",
        "code/runtime; see context",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "work/numeric-inventory.csv")
    args = parser.parse_args()
    rows = []
    for path in sorted([*ROOT.glob("apps/**/*.py"), *ROOT.glob("dexo_land/**/*.py")]):
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        content = path.read_text()
        lines = content.splitlines()
        for node in ast.walk(ast.parse(content)):
            if not isinstance(node, ast.Constant) or type(node.value) not in {int, float}:
                continue
            line = lines[node.lineno - 1].strip()
            classification, reason, owner = classify(relative, line, node.value)
            rows.append(
                [str(relative), node.lineno, str(node.value), classification, reason, owner, line]
            )
        if "migrations" in relative.parts:
            # Include SQL numeric tokens, which are strings rather than Python AST constants.
            for number, line in enumerate(lines, 1):
                if not any(
                    word in line.upper()
                    for word in (
                        "SELECT",
                        "LOCK",
                        "WHERE",
                        "IF ",
                        "CHECK",
                        "BETWEEN",
                        "VALUES",
                        "INTERVAL",
                    )
                ):
                    continue
                for token in re.findall(r"(?<![A-Za-z_])\d[\d_]*(?:\.\d+)?", line):
                    rows.append(
                        [
                            str(relative),
                            number,
                            token,
                            "SQL_SCHEMA_HISTORY",
                            "Database invariant or historical guard; review with migration matrix",
                            "database schema",
                            line.strip(),
                        ]
                    )
    for path in sorted(ROOT.glob("config/examples/*.json")):
        payload = json.loads(path.read_text())

        def walk(value: Any, field: str, profile_path: Path = path) -> None:
            if type(value) in {int, float}:
                rows.append(
                    [
                        str(profile_path.relative_to(ROOT)),
                        "",
                        str(value),
                        "EXAMPLE_CONFIG",
                        "Explicit example/default input, never hardcoded domain behavior",
                        field,
                        "",
                    ]
                )
            elif isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{field}.{key}" if field else key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{field}[{index}]")

        walk(payload, "")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            ["file", "line", "value", "classification", "reason", "authoritative_owner", "context"]
        )
        writer.writerows(rows)
    print(f"{len(rows)} numeric entries written to {args.output}")


if __name__ == "__main__":
    main()
