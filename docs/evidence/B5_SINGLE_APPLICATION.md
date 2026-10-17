# B5 final-review correction: one active Clan application

Scope: existing WEDCAL Draft #30, branch `ai/b5-backend-integration`. Retains owner documentation commit `b171fd9b5ecaa750288281e3ec7c5c09ba1a87c2` unchanged. No B6, migration, reward change, new UI, deployment or real Telegram send.

## Authoritative invariant and response

A Soldier has zero or one active join request globally, across Clans and Seasons. Both PENDING and ACCEPTED are active. Current membership, role, moderation/progression, leave/cooldown limits and Clan/Season eligibility checks remain in force. Repeated submission to the same eligible Clan returns its existing request; existing unsafe Home Base guards may reject a replay without creating another row.

Applying elsewhere while an active request exists returns existing HTTP 409 `CLAN_STATE_CONFLICT` with `{code, detail, retryable, retry_after_seconds}`. It exposes no other request/Clan identifiers. Acceptance never cancels or transfers another application. Safely CANCELLED, DECLINED and ACTIVATED history alone does not block future eligible applications; unresolved credentials behind those labels still do.

The applicant Player row serializes all authoritative creation/acceptance and credential transitions. Optional Season-state/ClanSeason locks retain their established order; request precedes invite. Captain cancellation, manual proof and remote completion now use the same applicant-first order. Telegram calls occur after the claim transaction. Only `request_join` creates new application rows; Clan Admin models remain read-only. Existing per-Clan live-request and global accepted-path constraints remain complementary guards.

A new partial unique constraint over all historical PENDING/ACCEPTED rows would require silently choosing or rewriting legacy paths. It is unnecessary for these serialized service writes, so this correction adds no migration or automatic data rewrite.

## Deterministic legacy reconciliation

No live/production overlap is assumed. Synthetic tests explicitly construct pre-correction overlaps and inconsistent labels to prove compatibility:

1. Retain every row and its original ClanSeason/Season identity. Do not pick a pending winner, auto-cancel another request, or discard an accepted credential/reservation.
2. Block new applications while any global active path or unsafe JOIN/REJOIN credential remains. CREATING, ACTIVE, UNCERTAIN, REVOKE_PENDING, REVOKE_FAILED, MANUAL_REVIEW, retained links and unknown-risk flags are not safe release proof, including on older Seasons or terminal request labels.
3. With multiple legacy active requests, acceptance and new JOIN credential issuance fail closed until extra applications are explicitly cancelled/declined through the existing authorized flow. Existing accepted-path replay, already-committed activation, credential revocation and risk resolution remain available. Activation does not silently edit other pending history; current membership then prevents their acceptance.
4. Pending requests can be explicitly cancelled or declined. Accepted cancellation retains ACCEPTED/reserved capacity until a known credential is positively revoked or nonissuance is proven. A manually confirmed revocation, already safely revoked credential, or unambiguous creation failure after committed cancellation safely terminalizes only ACCEPTED to CANCELLED. ACTIVATED and other historical records are not rewritten.
5. A stale/inactive context is not proof that CREATING/UNCERTAIN credentials disappeared. Recovery retains their risk in MANUAL_REVIEW. Legacy terminal credential labels with surviving risk/link, or issued/revoking states with missing credential metadata, also enter the existing review workflow instead of freeing eligibility. Dedicated permission, reason and audit remain required for manual proof.
6. Never reset uncertainty, fabricate revocation proof, delete rows, change Season identity or release unsafe capacity to satisfy uniqueness. Use existing read-only Admin inspection and authorized applicant/Captain/risk-resolution operations. No new mass reconciliation request or historical migration obligation is introduced.

## PH-048 and consumer changes

PH-048's product event is decided, with implementation deferred to Season 2. Season 1 `banner-raised` remains unavailable/unobtainable, null progress/target, no unlock and no XP/DLX/other reward. Future Season 2 is the first official authoritative server-confirmed Clan War victory, at most one unlock per ClanSeason. War finality and rewards remain unapproved future design; no War logic is added.

Reporting now uses empty `state.owner_decisions_pending` and `SEASON_2_DEFERRED` Trophy metadata, replacing the stale product-decision-required label. Availability/progress/reward behavior is unchanged. Owner backlog text is retained byte-for-byte from `b171fd9`.

FROCAL Draft #6 must pin the new runtime/fixtures, respect the existing single-application UI flow, retain cancellation-pending/unsafe states and handle the same typed 409. Do not offer simultaneous applications, implicitly replace one, or claim PH-048 needs another Season 1 decision. The later [active-request visibility correction](B5_REQUEST_VISIBILITY.md) supersedes the initial Season-scoped list: every global active/unsafe request is visible independently of bounded terminal history. New row fields describe Season ownership and safe handling. Consume all rows, detect overlaps and handle generic conflicts through existing authorized lifecycle/operator resolution; do not infer release or invent a cross-Season transfer. The list root and approved UI design remain unchanged. Historical B2/B3/B4 pins remain.

## Evidence

Local PostgreSQL validation:

| Check | Result |
| --- | --- |
| Expanded full suite | **1,026 passed, 1 opt-in benchmark skipped**, 371.93s |
| Final focused single-application file | **50 passed**, 57.87s, including four additional missing-metadata cases added after the expanded run started |
| Separate B2 PostgreSQL benchmark | **1 passed**, 49.69s; `b5-single-application-b2-benchmark.json` |
| Ruff / formatting / mypy | Passed; 436 typed source files |
| Django / migration drift | No issues; no changes detected |
| Repository contract / B2/B3/B4/B5 schema checks | Passed |

These are overlapping runs, not additive counts. The separate benchmark and exact-head hosted full suite/security/container results are recorded in the final review comment on Draft #30. Earlier green runtime `c38a7a6` is not substituted for this correction's new runtime pin.

Coverage includes cross-Clan sequential/concurrent requests, same-Clan replay/concurrency, pending/accepted/terminal eligibility, cancellation and uncertain credential states, old Seasons and terminal-label risk, acceptance/application races, membership activation versus successful/failed remote revocation, legacy explicit resolution and history retention, manual proof versus Captain cancellation, delayed creation/cancellation completion, and obsolete JOIN/REJOIN issuance. The full suite also retains existing economy, governance, PH-045 and B2/B3/B4 compatibility coverage. Transports and data are synthetic and isolated.
