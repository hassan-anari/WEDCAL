# B5 Backend — implementation in progress

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

Implementation, migrations, fixtures, full PostgreSQL regression, concurrency, compatibility, quality/security CI and paired FROCAL acceptance are pending. No completed behavior or passing test is claimed by this initial handoff.
