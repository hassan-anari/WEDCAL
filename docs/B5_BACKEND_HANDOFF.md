# B5 Backend — Draft PR #30

Owner-authorized batch: P-10/P-11/P-12 together, post-B4 avatar/Welcome/In-App announcement/banner corrections, and native multimedia Telegram Broadcast. One Draft PR; no merge, deployment, live send or B6 work.

## Audited baseline

Current main: `6aaa362daecec86107babd95224398e7e1ac7e75` (merged B4 #29).

Reuse `apps/clans/{services,governance,economy,missions,activity}.py`, existing models, ledgers, tasks and router. Existing routes already cover role, eligible groups, registration, discovery/details, requests/decisions/cancellation, invites, membership, home, contribution, treasury, shop, inventory, missions, governance, leave and recovery. They are the baseline, not speculative new APIs.

## Contract gaps being closed

- Roster membership identifiers needed for removal cases; own pending/accepted/rejected requests after refresh.
- Clan identity, safe avatar and Captain/roster read metadata; internal group PK clarification (`telegram_group_id` is not a raw Telegram chat ID).
- Effective item rules, active effects, purchase/use limits and availability; Trophy Hall state.
- Typed stable errors and real HTTP fixtures across membership/governance/economy.
- Safe blocked/pending/uncertain/failed/recovered states without exposing private invite credentials across players.

FROCAL is the consumer. Map existing UI requirements to the implemented router, record any missing data before inventing shapes. This initial planning commit is **not** an implementation pin. A new exact runtime SHA, generated OpenAPI/fixtures and validation evidence will replace it when ready.

## Owner decisions remaining isolated

PH-045: same-Season recreation after dissolution and detailed Telegram authority-loss recovery triggers remain owner-owned. Existing resolved invariant: ACTIVE/RECOVERY ClanSeason exclusively owns CLAN_HOME; non-INACTIVE Creator registrations cannot coexist; dissolution does not reactivate Creator state. Preserve accepted-request no-TTL reservation and unsafe-credential retention (PH-047 resolved).

PH-048: `banner-raised` has no concrete server-owned first shared milestone predicate; keep evaluation fail-closed and award no points. Other approved achievements remain usable.

## Additional corrections

- Admit verified `profile_photos/` alongside `photos/`; preserve all retrieval/sanitization controls and safe categorical errors.
- Normalize textarea CRLF/CR to LF before Welcome validation/persistence; retain immutable HTML/keyboard versions.
- Separate Django Admin In-App announcements from templates and Telegram campaigns: immutable confirmation, dedicated permissions, bounded durable processing and exactly one logical notification per selected existing Player.
- Any validated HTTPS banner destination is owner approved; retain strict URL structure validation and empty links.
- Broadcast composer captures an original private operator message/album and copies via copyMessage/copyMessages. Durable source, bounded album collection, actual preview, explicit confirmation, existing audience policy, safe partial/ambiguous handling. Synthetic transports only.

## Validation / delivery state

Implementation is present in this branch: [actual contract](B5_BACKEND_CONTRACT.md), [operator guide](B5_OPERATOR_GUIDE.md), [generated schema/fixtures](fixtures/b5/).

The targeted B4 corrections passed (62 tests), and the initial native-copy/Clan suite exercised 230 passing tests before fixing three integration test/route expectations. A later isolated HTTP/Welcome fixture run passed 19 tests. These are intermediate overlapping runs, not a full-regression claim. Full final PostgreSQL, migration, quality/security CI and paired FROCAL acceptance remain under verification.

FROCAL main audited: `ef6f1fca4de541e6eb45c9b8bf3e114bbd163cfb`. No open B5 frontend PR was found at task start; no frontend code is changed here. Consumer handoff must map actual fields, use typed destination navigation and pin the new runtime commit rather than the initial handoff commit. Physical Telegram/device and frontend paired acceptance remain separate evidence.

Migrations: `clans.0015_b5_mission_signals`; `notifications.0009_b5_announcements_native_copy`; `notifications.0010_b5_snapshot_guards`. Additive tables/columns and PostgreSQL guards only; no historical economy/campaign/config rewrite. Native source/announcement confirmation guards prevent post-confirmation content mutation. Pending Clan mission signals participate in season-close readiness.

Shared edits are limited to authenticated cache/error metadata, three configured worker/processing settings, native Telegram copy result/budget classification, the existing Typing source adapter, and one season-close drain provider. Reuse canonical Clan services, source-time eligibility, ledgers, rewards and queue infrastructure.

No merge, deployment, real sends or B6 work. Exact runtime SHA and hosted CI/review evidence will be appended after validation.
