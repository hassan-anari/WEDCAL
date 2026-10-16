# B5 backend handoff — Draft PR #30

[WEDCAL Draft #30](https://github.com/hassan-anari/WEDCAL/pull/30) contains the complete owner-authorized P-10/P-11/P-12 batch, post-B4 corrections and native multimedia Broadcast. WEDCAL owns the API; FROCAL consumes it. The final full runtime SHA and hosted check links are published together in the PR's final review handoff and the paired FROCAL PR #5 conversation. Pin that runtime SHA, never the initial documentation commit `75a43e5` or intermediate runtime `af72ede`.

## Baselines and actual contracts

Backend main was fetched and checked before final validation: `6aaa362daecec86107babd95224398e7e1ac7e75` (merged B4 #29), already an ancestor of this branch. FROCAL reference main: `ef6f1fca4de541e6eb45c9b8bf3e114bbd163cfb`. No frontend code is changed here.

- [Implemented API and errors](B5_BACKEND_CONTRACT.md)
- [Generated OpenAPI and real HTTP lifecycle fixtures](fixtures/b5/README.md)
- [Django Admin and native Broadcast operator guide](B5_OPERATOR_GUIDE.md)
- [Local validation and migration evidence](evidence/B5_VALIDATION.md)

Clan core, governance, ledgers, rewards, Shop/inventory and missions reuse the existing Phase 7/8 models/services. New read contracts close roster, own-request history, equipment/effects and Trophy Hall gaps. Durable server-event adapters invoke the canonical mission progress/settlement engine; no demo rewards or parallel economic system are introduced.

## Owner decisions and consumer gaps

PH-045 is implemented for the approved Season 1 scope: same-Season Captain recreation stays blocked; confirmed original-Captain Telegram authority loss enters RECOVERY; fresh original-Captain and bot permission verification is required to clear that cause. Activity alone cannot restore it. Independent bot-loss/inactivity causes and existing governance cannot be prematurely cleared. No new automatic dissolution, grace period, deadline, penalty or succession exists. Creator Program and CLAN_HOME ownership remain exclusive.

PH-048 `banner-raised` remains unavailable with `PH-048_PRODUCT_DECISION_REQUIRED`, null progress/target and no reward, pending a concrete owner-approved server milestone predicate.

FROCAL mapping requirements:

- Consume additive `GET /recovery` fields and Captain-only `POST /recovery/verify`, including independent `reasons`. At audited FROCAL main, `ClanPage.tsx` derives recovery from the demo `supply-cache` effect; it has no authority-loss lifecycle adapter or verification action. Reuse approved Recovery presentation, report missing representation, and do not substitute the demo item state for server governance.
- Use member-only roster `membership_id` for removal targets; keep invitation URLs confined to the authenticated applicant/member. Persist own pending/accepted/uncertain/cancellation states from the actual request/invite endpoints.
- Use `telegram_group_id` as the internal candidate PK, not a client Telegram chat ID. Registration re-verifies authority.
- Use versioned server Shop/equipment/mission amounts and availability. Unknown ranks, Telegram member counts, applicant activity metrics and match percentages remain unknown; do not synthesize them from demo fixtures. Season 2 Defence/War remains unavailable.
- Banner destinations support any validated external HTTPS host. Use typed `TELEGRAM` versus `HTTPS` navigation, preserve query parameters, never render the URL as executable HTML/script, and retain empty destinations.

The paired handoff is published in FROCAL PR #5 because no open B5 frontend PR existed at task start. Frontend implementation/device acceptance is consumer-owned follow-up, not evidence claimed by this backend PR.

## Administration and migrations

In-App announcements use dedicated publish/cancel permissions, explicit signed confirmation, immutable snapshots and bounded transactional Celery batches with monotonic cursor/counts. Stable notification identity prevents duplicates; cancellation stops unprocessed work. This workflow never sends Telegram.

Native Broadcast uses copyMessage/copyMessages, actual complete preview, explicit audience/confirmation and the existing B4 eligibility. `/done N` seals albums only after all 2–10 items and the bounded quiet interval. The first complete bot-authored preview becomes the immutable copy source; later original edits/deletion cannot change the campaign. Partial/ambiguous copies become UNCERTAIN without blind retry. Private operator auth, signed callbacks, API budgets, current destination permissions, cancellation and audit remain.

Added migrations (Django graph determines ordering):

- `clans.0015_b5_mission_signals`
- `clans.0016_b5_captain_authority_recovery`
- `notifications.0009_b5_announcements_native_copy`
- `notifications.0010_b5_snapshot_guards`
- `notifications.0011_b5_frozen_native_source`

Additive tables/columns/indexes and PostgreSQL immutability guards; no historical economy/campaign/config rewrite. Rendered SQL is included in `docs/evidence/B5_*.sql`. New workers require the migrations; older workers must not consume native campaigns. Pending mission signals participate in season-close drain. Operational settings and safe rollback are in the operator guide.

## Independent review handoff

Local full PostgreSQL regression: **980 passed, 1 benchmark skipped**; the benchmark runs separately. Ruff, formatting, mypy, Django checks, migration drift, repository contract and B2/B3/B4/B5 schema checks are required. Final hosted results and exact SHA are attached to the PR before handoff; earlier green runs are not substituted for the final runtime checks.

Review PostgreSQL snapshot/source guards; announcement authorization and atomic cursor/notification writes; native source freezing and serialized preview selection; irreversible partial/ambiguous copy handling; copy API budgets; source-time economics; whole-second versus precise evidence ordering, in-flight verification fencing and independent recovery causes; voting/finalization lock order; closed credential cleanup and mission drain. The coding task stops for owner and independent review after all required hosted checks are green.

No merge, deployment, live broadcast, public test send, unrelated UI change or next phase is authorized or performed.
