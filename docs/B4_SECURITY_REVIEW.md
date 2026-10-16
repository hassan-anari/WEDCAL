# B4 security and economic review

Review scope: backend Draft #29, P-08/P-09 and approved B3 corrections. This is an implementation
review and isolated test evidence, not independent approval or production certification.

## Trust and economic boundaries

Signed Telegram initData remains the sole Mini App identity. Profile/customization, notifications,
Typing history, invitees and owned groups are actor-scoped. Unauthorized entitlement changes return
an existing typed error; no demo fallback grants items. Ranking score/rank comes from versioned
PostgreSQL projections and atomically read Redis projections; known pending/drifting data returns
503 rather than mixing generations. CLOSED/ARCHIVED rank/score uses immutable final standings.
Identity/profile decoration on archived rows is current metadata, not frozen historical artwork.

Reward receipts reference confirmed RewardLedger rows, not submitted amounts or client scoring.
Before/after progression is captured under the same Player lock as claims. Duplicate settlements
retain event/ledger identity with `created:false`; zero Typing wallet claims have no reward event.
Typing `score` is presentation derived from server accuracy/WPM and never an economic input.
Tests preserve an existing legitimate 150 XP row byte-for-byte and verify canonical balances after
new claims. DLX allocation/hold/forfeit totals use ledger-derived SQL and never mutate XP. Wallet,
account/on-chain features and Season 2 Warfront stay unavailable.

Mission model/Admin validation requires matching effective GameConfig key/family/repeat policy
before enabling. Invalid content/JSON or orphaned legacy definitions are quarantined from Bootstrap.
Previously verified obligations retain original config/reward provenance and idempotency. This is
not a new database publication trigger: direct bulk ORM/SQL bypass cannot make an invalid row
rewardable or crash Bootstrap, but trusted operators must use the validated Admin/service path.

## Images and navigation

Invitee photo fields return only private cache capabilities, including on cache misses and for
start-only users. Group images use known registered groups, identity-checked `getChat`, existing
`getFile`/download path validation, SSRF protections, byte/pixel limits and JPEG normalization.
Global/method Telegram budgets, fail-closed budget storage, bounded cross-worker refresh slots,
refresh leases, expiry/rotation and purge remain shared. No Bot API credentials, file IDs or direct
Telegram file URL is returned by the new fields. Missing/unconfigured images return empty strings.

Group images require explicit dedicated credentials with only `group-images/` scope; code never
falls back to avatar/default credentials. API capabilities are unguessable UUIDs and deliberately
usable by image elements without Mini App auth. Treat them as private bearer URLs: TTL/rotation,
private bounded caches, no-referrer/nosniff, no access-log sharing, private bucket and lifecycle
are operational prerequisites. Synthetic storage/transport tests do not prove Backblaze IAM,
VPN-off reachability or bucket lifecycle in production. Cache remains disabled by default.

Notification `deep_link.path` is selected from trusted source domains and existing route literals.
Payload URLs never select navigation. Read/read-all and feed pagination isolate actors; private bot
control notifications are excluded. Personalized API responses are private/no-store.

## Telegram authorization, delivery and consent

Webhook ingestion only validates/deduplicates/persists/enqueues and ACKs. Durable reply/ACK rows
publish after outer commit, so rollback emits no work. Broker failure leaves scanner recovery.
Celery late acknowledgements, PostgreSQL SKIP LOCKED/leases, due times, rate budgets, Retry-After,
queue age and UNCERTAIN terminal ambiguity remain enforced. A second worker cannot claim the same
live lease. No Bot API message is sent inside a business transaction. Network failures after dispatch
cannot guarantee physical exactly-once Telegram delivery; uncertain outcomes are never auto-resent.

Bot operator mapping, active user, permission and configured allowlist are checked on command and
again before queued panel/ACK/test delivery. Callback ACK is independently durable and does not
execute privileged actions for invalid/expired interactions. Actual broadcasts require explicit
authorized operator confirmation, existing preview flow and audit. No real campaign was sent.

Intentional one-use creator-bound Add Bot authorization may enroll only a new verified registration.
No historical enrollment. Existing subscription rows, especially revocation tombstones, are never
overwritten by automatic registration verification. Registration/creator/bot state is rechecked after
external verification under the group lock. Broadcast sends check current creator/bot Telegram
authority and subscription revision/revocation again after verification. Concurrent revocation or
observed bot loss fails closed before dispatch. An already dispatched operation cannot be recalled;
there is no promise to undo a message already handed to Telegram.

## Dynamic Welcome

No static copy/keyboard fallback. Admin creates immutable versions guarded by model validation and
PostgreSQL UPDATE/DELETE trigger. Only a singleton activation pointer changes. Allowlisted HTTPS
URLs reject credentials, unsafe escaping/redirect queries/fragments/ports; a bounded static HTML
subset validates tags, attributes, entities and text. No interpolation, scripts or arbitrary recipient.
Multiple URL/Web App keyboard rows and emojis are supported.

Create/preview/activate/test have Django permissions; activation/rollback and test require signed,
expiring actor/version/action confirmation, CSRF and reason. Tests target only the requesting
mapped/allowlisted authorized operator's own reachable chat. Create/activate/test are audited.
Activation/rollback never sends to existing Players. Canonical Player/referral resolution and
`bot-welcome:{player}` dedupe remain. BOTH B3 and B4 public gates must be true; B4 defaults false.
No active template is seeded. Tests intercept HTTP and never contact real Telegram users.

## Required review and residual operation gates

- Inspect seven additive migrations and [generated SQL](evidence/b4-migrations.sql); concurrent
  indexes need non-atomic execution and invalid-index inspection on interrupted deployment.
- Keep public Welcome disabled until explicit owner approval. Provision scoped private image
  storage and lifecycle separately before enabling cache. No credentials were broadened here.
- Configure worker consumers before opting into a separate interactive queue. Retain scanners.
  Synthetic healthy p95 proof covers real PostgreSQL/RabbitMQ/prefork/Redis budgets but not WAN,
  physical Telegram WebView, production capacity or Telegram API latency.
- Hosted dependency/container scans and exact final test evidence are recorded in the handoff.
  Preserve existing actionable-vulnerability policy; no new ignores/suppressions or dependencies.
- Frontend must pin the final exact backend SHA, preserve the B3 pin and run paired regressions.
  Historical-ranking and DLX visual controls remain owner-owned decisions.

No merge, deployment, production write, real test-send or public message is authorized/performed.
Stop for independent review; this document does not authorize activation.

## Hosted scan disposition at implementation ec6af70

[Release Scans run 38869293916](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293916)
passed. Dependency audit: no known vulnerabilities. Container: 44 HIGH package finding instances,
eight unique IDs, **0 CRITICAL, 0 fixable HIGH/CRITICAL**. Exact packages/versions are retained in
[b4-trivy-policy.json](evidence/b4-trivy-policy.json) and hosted raw JSON/SARIF artifacts. These are
the same eight no-fix IDs reviewed in the [existing baseline disposition](PHASE11_CONTAINER_SECURITY.md#final-no-fix-review).
Dockerfile, runtime privileges and dependency lock are unchanged; B4 adds no mount/ACL/Perl/terminfo
execution path. Preserve those applicability assessments for independent review:

| No-fix HIGH ID | Applicability / temporary disposition |
| --- | --- |
| CVE-2025-69720 | ncurses/terminfo tool path; application never invokes infocmp or parses attacker terminfo. Not reachable in runtime. |
| CVE-2026-16742 | systemd-homed service is absent; no init/login/home-record subsystem. Not applicable to container threat model. |
| CVE-2026-54369 | No privileged ACL pathname operations or setfacl path; app is non-root. Not reachable in runtime. |
| CVE-2026-76642 | Requires privileged mount helpers/hooks; application performs no mounts and has no mount authority. Not applicable. |
| CVE-2026-78408 | Requires privileged nsenter/cgroup authority; application never invokes nsenter. Not applicable. |
| CVE-2026-78409 | Requires local fstab-authorized privileged mount flow; no such application path. Not applicable. |
| CVE-2026-78410 | Requires restricted SUID bind-mount/ownership-hook flow; app does not mount filesystems. Not applicable. |
| CVE-2026-9538 | Archive::Tar module/path is absent and application performs no Perl archive handling. Not reachable in runtime. |

No ignore, severity suppression or weaker gate was added. Re-evaluate when upstream fixes become
available or runtime privileges/features change. A green policy result does not erase these findings
or authorize release; independent review and owner deployment/activation approval remain required.
