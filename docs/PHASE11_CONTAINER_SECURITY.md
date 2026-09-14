# Phase 11 container security review

Review date: 2026-09-14 UTC. Review owner: repository owner / Phase-11 release reviewer.

## Policy

- A HIGH or CRITICAL image finding with a non-empty Trivy `FixedVersion` is release-blocking.
- A HIGH or CRITICAL finding classified `BLOCKING` by review is release-blocking.
- Every no-fix HIGH/CRITICAL requires an individual applicability and temporary-risk disposition.
- New HIGH/CRITICAL IDs require review; scanner success by itself is not acceptance.
- No vulnerability is suppressed by `.trivyignore` in this pass.

The release workflow retains SARIF, raw JSON, and a policy result. The policy validator fails when a
fixable HIGH/CRITICAL remains and preserves package, version, ecosystem, and image target in its
machine-readable output.

## Accepted hosted baseline

The artifact inspected by the independent reviewer at hosted HEAD
`e087e09ded0f30efb09360174111b93a603ff710` contained 181 SARIF results: three CRITICAL and 55 HIGH
instances, representing 22 unique HIGH/CRITICAL IDs. Fourteen unique IDs had a fix and eight did not.

The actionable findings reported by that artifact were:

| Package | Finding IDs | Fixed version |
|---|---|---|
| `perl-base` | CVE-2026-13221, CVE-2026-42496, CVE-2026-8376, CVE-2026-42497, CVE-2026-48962, CVE-2026-57432, CVE-2026-57433 | `5.40.1-6+deb13u1` |
| `libsqlite3-0` | CVE-2026-11822, CVE-2026-11824 | `3.46.1-7+deb13u2` |
| `libpcre2-8-0` | CVE-2026-86145, CVE-2026-89161 | `10.46-1~deb13u2` |
| `gzip` | CVE-2026-41992 | `1.13-1+deb13u1` |
| `setuptools` | CVE-2025-47273 | `78.1.1` |
| `msgpack` | GHSA-6v7p-g79w-8964 | `1.2.1` |

`setuptools` and `msgpack` are absent from the application dependency graph and synchronized
application virtual environment. They are bootstrap/base-image packages, not runtime requirements;
the final stage removes them rather than adding artificial direct project dependencies. The runtime
stage upgrades packages from the configured Debian repository, clears apt/pip caches, adds no build
toolchain, and continues permanently as the unprivileged `app` user.

## Final-artifact review gate

The next hosted `container-scan` artifact must be used to populate this table. Do not infer a final
disposition from the pre-remediation artifact because scanner metadata and installed packages may
change during rebuild.

| ID | Package/path | Final status | Runtime prerequisite and disposition |
|---|---|---|---|
| _from final `trivy-results.json`_ | _from artifact_ | `BLOCKING` until reviewed | Record exact executable/library path, exploit prerequisites, application reachability, upstream status, owner, and next review condition. |

PH-039 remains `VERIFYING` until the hosted policy passes, the final raw JSON/SARIF artifacts are
retained, and every row in `unfixed_requiring_review` receives a concrete disposition. A no-fix
finding is not implicitly accepted because the process is containerized or non-root.
