# B6 initial code and contract audit

Authoritative base: `b4c72f292ab0ea967d62af7b8e5fdcab1f27e03f`, fetched from main on 2026-10-17. Isolated branch: `ai/b6-backend-operations`. B5 #30 and frontend routing #7 are merged. Routing is outside this change.

Read: root AGENTS, Season 1 Source of Truth, economy contract and sample configurations, integration register, B5 contract/handoff/operator and correction evidence, mission/Clan/Telegram/notification/Arena/Daily/avatar implementations, migration graph, directly related regression tests and CI workflows. The owner instruction supersedes the older Source of Truth's private invite DM requirement; authenticated credential retrieval and verified activation remain mandatory. No canonical source document or published economy is rewritten.

## Confirmed implementation gaps

- Personal Admin lacks preview/controlled activation; validation does not check reversed availability or Telegram chat identity. Existing family/repeat/config and progress protections must remain.
- Clan mission definitions lack Admin, display text and definition-history guards. B5 already has durable source-time signals and automatic settlement; use that engine and published reward bands.
- Applicants omit Arena rank and canonical Daily streak. Batch reads must validate projection generations and distinguish unavailable/reconciling from unranked.
- Welcome identity is permanently `bot-welcome:{player}`. Change to incoming update identity while retaining immutable queued snapshots, shared API budgets, retry/uncertainty and activation gates.
- Invite creation emits a private credential delivery. Remove it and fence old queued deliveries at dispatch; preserve all credential states, reservations, revocation and authenticated retrieval.
- Eligible groups omit Network exclusion, although registration checks it under the group lock. Apply the same exclusion to list/fresh checks and retain final locking. Network registration already requires a server-owned unconsumed intent; Clan add-bot must never mint one.
- Existing secure GroupAvatar cache supports registered Clan homes but not pre-registration candidates. Populate existing image_url and permit refresh only for known authorized candidates.
- Treasury history is not an activity feed. Add a bounded own-Clan read over durable records; do not infer achievements from raw Telegram activity.

## Boundaries and blockers

No product decision blocks A–E/G/H. Backend owns the additive feed contract. Owner clarified there is no active B6 frontend PR: create a dedicated FROCAL coordination issue, publish exact backend SHA/schema/examples/migrations there, and require explicit frontend acknowledgement and pin before frontend implementation/paired testing. Backend implementation/tests continue independently. Initial proposal was posted to historical FROCAL #6 before this clarification; the dedicated issue supersedes that destination.

PH-037 (required branch rules) and PH-043 (Season-brief product evidence) remain public-release gates. No production verification, rewards, broadcasts, merge or deployment is authorized. Local dependency/runtime repair is needed before claiming tests; CI and isolated PostgreSQL evidence will be recorded separately. B5's same-Season recreation restriction, independent recovery causes and deferred Season 2 War remain binding.
