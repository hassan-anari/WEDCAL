# Read-only FROCAL compatibility and handoff

Consumer inspected: `hassan-anari/FROCAL` main
`eb76307615322016bc39509c467f64be0268423a`. The checkout was used only as a read-only reference;
its Git status remained clean. No frontend commit, issue/comment, deployment or UI redevelopment
is included. The original integration contract's older fixed-topology descriptions are historical;
the owner's configurable-Season instruction governs new schema-4 Seasons.

## Exact findings and affected consumer files

| Consumer location at inspected SHA | Current behavior | Required frontend handoff |
| --- | --- | --- |
| `src/integrations/api/bootstrapContract.ts:47` | Season Zod object lists start/end/status/points; unknown `timing` is stripped | Add optional nullable `timing`; if present, validate every required timing field |
| `src/integrations/api/bootstrap.ts:9,46` | ACTIVE maps to live; raw remote object is retained after parsing; normalized Season carries ID/name/status | Keep ACTIVE compatibility, preserve parsed timing and gameplay gate; derive a separate closing presentation |
| `src/integrations/api/BootstrapProvider.tsx:43` | Canonical updates spread the existing remote Season | Retain timing across updates and scoped cache transitions; no new economic computation |
| `src/pages/ArenaPage.tsx:538–548` | Product progress/countdown uses server starts_at/ends_at/server_time | This is valid product-period math. Use economic_ends_at for an earning countdown and distinguish closing; do not label all remaining product days as earning days |
| `src/integrations/api/arena.ts:27` | Arena season schema exposes dates, not Epoch topology | Current Season timing comes from Bootstrap. Do not invent a duplicate constant topology in Arena |
| `src/integrations/api/bootstrap.contract.test.ts`, `bootstrap.test.ts` | Existing fixtures omit configurable timing | Add the profile, unknown-timing, cutover and closing cases below |
| `src/game/arena/arenaCatalog.ts:14–15`, `src/mocks/fixtures/bootstrap.ts` | Explicit local/demo days/progress fixtures | Keep local fixtures separate from remote authority; create varied test profiles without application Demo branches |
| `scripts/contracts/backend-bootstrap-schemas.py`, `backend-bootstrap-schema.json`, `generate_bootstrap_fixture.py` | Pinned backend snapshots/fixture generation | Refresh from the final PR runtime/schema SHA only after frontend work is authorized |

No fixed six-Epoch, 42-day, 45-day or three-day closing calculation was found in the production
remote Season rendering. The actual gaps are discarded additive timing and absence of a closing-aware
earning presentation. Other occurrences of 42/45 are asset sizes, animation timings, mock scores or
locked future War demonstrations; they are not active Season economics and should not be rewritten.

## Authoritative backend contract

`GET /api/v1/bootstrap`, contract version 1.1, remains compatible. `season` stays nullable and
`season.status` keeps existing values. The additive nullable `season.timing` contains:

| Field | Authority / interpretation |
| --- | --- |
| product_period_days | pinned Season baseline; total product duration |
| epoch_count | pinned economic topology, no extra closing Epoch |
| epoch_days | pinned economic Epoch length |
| closing_days | pinned product-end minus economic-end window |
| economic_ends_at | authoritative end of new economic earning |
| settlement_start_epoch | current audited operational gate, initialized from baseline |

`season.starts_at/ends_at`, `server_time` and `gameplay.reward_earning_allowed` are already provided.
The top-level effective GameConfig may change prospective gameplay tuning; it must not replace
the pinned Season timing. A timing object absent/null means unknown topology, never an implicit
6/42/45 preset. Product countdown can still use provided dates. Keep server-earned balances and
manual wallet claim/Close semantics authoritative. Closing disables new earning; it does not imply
all vested claims or audited Close work are unavailable.

The settlement gate is an earliest operational authorization, not a promised payout time. Epoch n
cannot settle before its cutoff/source/readiness/Freeze requirements. Do not infer confirmed DLX
from elapsed time, budgets, XP or a locally predicted settlement date.

## Proof and required frontend tests

The unchanged consumer bootstrapContract.ts and b3Contracts.ts were copied into an isolated harness,
compiled with the lockfile's TypeScript 5.9.3 and Zod 4.6.7, and fed **actual backend-produced**
registered-policy 15-day/45-day closing Bootstrap fixtures. Both parsed successfully, retained known
fields/gameplay=false, and stripped timing as expected. Results are in
`inspection/frontend-compatibility.json`; fixtures are in `inspection/bootstrap/` and CI artifacts.
This proves old-parser additive compatibility, not new UI behavior or paired staging acceptance.
To reproduce, copy only those two unmodified consumer files to a disposable harness, install
`zod@4.6.7 typescript@5.9.3` there with `--ignore-scripts`, compile with
`tsc --module commonjs --target es2020 --esModuleInterop --skipLibCheck`, then call
`remoteBootstrapSchema.parse` on each checked-in Bootstrap fixture. Verify accepted known fields
and that timing is stripped. The evidence records source and fixture hashes; no frontend install,
build or code modification is needed in the reference checkout.

Required separately authorized FROCAL tests:

1. 15=2×7+1, gate 1: Earning ends at day 14, closing lasts one day; no third economic Epoch.
2. 45=6×7+3, gate 3: Earning ends day 42, product ends day 45; gate begins day 14 and promises no
   allocation. Existing Official behavior and balances remain intact.
3. Another valid topology, such as 17=3×5+2, proving no Demo/Official conditional branch.
4. Start/exact Epoch boundary/economic cutoff/last closing instant/product end; server clock offset,
   monotonic elapsed display or refetch, reload and background resume. Clamp product/earning progress.
5. Missing/null/invalid timing: accept reviewed legacy absence, reject malformed present fields,
   show unknown economic timing, never substitute fixed economic dates.
6. Preserve timing through BootstrapProvider canonical updates and player/Season/config cache changes;
   ordinary gameplay config cutovers cannot alter a Season countdown/topology.
7. Audited settlement-gate changes update the displayed operational gate; no inferred paid state.
8. Respect reward_earning_allowed during closing while preserving vested claim behavior; new earning
   rejected by the server must not fall back to local/mock rewards.
9. All five categories and confirmed off-chain/nonclaimable DLX remain server-sourced; no fabricated
   price, transfer, wallet or XP conversion. Preserve locked future-season previews.

Backend timing/schema support and fixtures are complete first. Frontend implementation and its
device/browser tests require separate authorization; no paired frontend release acceptance is claimed.
