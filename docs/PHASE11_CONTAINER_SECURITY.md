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
application virtual environment. Filesystem and raw-Trivy reproduction identified their exact source
as `/usr/local/lib/python3.13/site-packages/pip/_vendor/bom.cdx.json` in the official Python image.
That third-party CycloneDX inventory describes pip's genuinely present vendored installer code:
`pip/_vendor/msgpack` and `pip/_vendor/pkg_resources` (setuptools provenance); they are not standalone
installed distributions and do not come from `/app/.venv`. The Trivy records consequently had target
`Python`, class `lang-pkgs`, type `python-pkg`, PURLs `pkg:pypi/msgpack@1.1.2` and
`pkg:pypi/setuptools@70.3.0`, and no `PkgPath` or layer field because the CycloneDX components omit
those fields. This is not stale OCI-attestation metadata: it is an SBOM file in the final filesystem.

The application never installs dependencies at runtime, so pip and its vendored package-management
code are unnecessary after the builder creates `/app/.venv`. The runtime stage removes pip itself,
which removes both the vendored implementations and their CycloneDX inventory, rather than adding
artificial application dependencies. A local Trivy rootfs control scan detected both findings before
removal and neither afterward; adding the synchronized application virtualenv back still produced no
Python HIGH/CRITICAL finding. Hosted CI additionally asserts that neither interpreter exposes pip,
msgpack, or setuptools and that no matching metadata remains. Debian updates and the permanent
unprivileged `app` user are preserved.

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

## Current no-fix review candidates

These eight IDs came from the current Trivy database during the filesystem control scan. The final
hosted JSON must reconfirm them before the dispositions become acceptance evidence.

| ID | Affected installed package(s) | Runtime analysis | Provisional classification |
|---|---|---|---|
| CVE-2025-69720 | `libncursesw6`, `libtinfo6`, `ncurses-base`, `ncurses-bin` | Vulnerable code is the `infocmp` CLI. It is present but neither the Django/Celery runtime nor externally influenced input invokes it. Exploitation requires invoking it on attacker-crafted terminfo. | `NOT_REACHABLE_IN_RUNTIME` |
| CVE-2026-16742 | `libsystemd0`, `libudev1` | The affected `systemd-homed` service/executable is absent; the container has no init system, homed user records, or local login facility. | `NOT_APPLICABLE_TO_CONTAINER_THREAT_MODEL` |
| CVE-2026-54369 | `libacl1` | The library is present, but `setfacl` is absent and the application performs no privileged ACL pathname operations. Runtime is non-root. | `NOT_REACHABLE_IN_RUNTIME` |
| CVE-2026-76642 | util-linux binary/library package family | Requires a privileged/SUID mount helper plus attacker-controlled mount hooks/filesystems. The app user has no mount capability and the application never invokes `mount`. | `NOT_APPLICABLE_TO_CONTAINER_THREAT_MODEL` |
| CVE-2026-78408 | util-linux binary/library package family | Requires a privileged operator to run `nsenter --join-cgroup` against an attacker-controlled target. The application never invokes `nsenter` and runs without host cgroup authority. | `NOT_APPLICABLE_TO_CONTAINER_THREAT_MODEL` |
| CVE-2026-78409 | util-linux binary/library package family | Requires a local unprivileged user with an fstab-authorized `X-mount.subdir` entry and privileged mount execution. The image has no such application path or writable host fstab contract. | `NOT_APPLICABLE_TO_CONTAINER_THREAT_MODEL` |
| CVE-2026-78410 | util-linux binary/library package family | Requires a SUID restricted bind mount, attacker-replaceable authorized source, and X-mount ownership/mode hooks. The non-root application does not mount filesystems. | `NOT_APPLICABLE_TO_CONTAINER_THREAT_MODEL` |
| CVE-2026-9538 | `perl-base` | The vulnerable `Archive::Tar` module is absent from the filesystem; neither application interpreter nor task path invokes Perl archive handling. | `NOT_REACHABLE_IN_RUNTIME` |
