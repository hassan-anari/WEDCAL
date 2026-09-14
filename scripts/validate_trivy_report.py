#!/usr/bin/env python3
"""Reject fixable HIGH/CRITICAL vulnerabilities in a Trivy JSON image report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def evaluate(report: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for result in report.get("Results") or []:
        target = str(result.get("Target") or "")
        ecosystem = str(result.get("Type") or result.get("Class") or "unknown")
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity") or "UNKNOWN").upper()
            if severity not in {"HIGH", "CRITICAL"}:
                continue
            findings.append(
                {
                    "id": str(vulnerability.get("VulnerabilityID") or "UNKNOWN"),
                    "severity": severity,
                    "package": str(vulnerability.get("PkgName") or "UNKNOWN"),
                    "installed_version": str(vulnerability.get("InstalledVersion") or ""),
                    "fixed_version": str(vulnerability.get("FixedVersion") or ""),
                    "ecosystem": ecosystem,
                    "target": target,
                }
            )
    fixable = [finding for finding in findings if finding["fixed_version"]]
    unfixable = [finding for finding in findings if not finding["fixed_version"]]
    return {
        "policy": "HIGH/CRITICAL findings with an upstream fixed version are release-blocking",
        "passed": not fixable,
        "finding_instances": len(findings),
        "unique_ids": sorted({finding["id"] for finding in findings}),
        "fixable": fixable,
        "unfixed_requiring_review": unfixable,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.report.read_text()))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if result["fixable"]:
        print(json.dumps(result["fixable"], indent=2, sort_keys=True))
        return 1
    print(
        f"Trivy policy passed; {len(result['unfixed_requiring_review'])} "
        "unfixed HIGH/CRITICAL finding instances require documented review"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
