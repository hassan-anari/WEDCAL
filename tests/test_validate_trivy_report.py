from scripts.validate_trivy_report import evaluate


def test_actionable_trivy_findings_fail_policy_and_retain_location() -> None:
    result = evaluate(
        {
            "Results": [
                {
                    "Target": "Python",
                    "Type": "python-pkg",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-FIXABLE",
                            "Severity": "HIGH",
                            "PkgName": "example",
                            "InstalledVersion": "1",
                            "FixedVersion": "2",
                        },
                        {
                            "VulnerabilityID": "CVE-NO-FIX",
                            "Severity": "CRITICAL",
                            "PkgName": "base",
                            "InstalledVersion": "1",
                        },
                    ],
                }
            ]
        }
    )
    assert not result["passed"]
    assert result["fixable"][0]["target"] == "Python"
    assert result["unfixed_requiring_review"][0]["id"] == "CVE-NO-FIX"


def test_only_unfixed_findings_pass_automated_policy_but_require_review() -> None:
    result = evaluate(
        {
            "Results": [
                {
                    "Target": "debian",
                    "Type": "debian",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-NO-FIX",
                            "Severity": "HIGH",
                            "PkgName": "base",
                            "InstalledVersion": "1",
                            "FixedVersion": "",
                        }
                    ],
                }
            ]
        }
    )
    assert result["passed"]
    assert result["fixable"] == []
    assert result["unique_ids"] == ["CVE-NO-FIX"]
