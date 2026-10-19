# PostgreSQL migration and compatibility review

No applied baseline migration is edited. New migrations are additive and do not seed configs,
approve policies, bind Seasons, authorize jobs, allocate DLX or rewrite balances/history.

| Migration | Change | Lock/write implications |
| --- | --- | --- |
| arena 0020 | three operational/funding tables, indexes and uniqueness; replace fixed ≤6 check with positive Epoch check | new tables are empty; brief schema lock on small Epoch table; no million-player table rewrite |
| arena 0021 | active Season setup immutability; new-style Epoch topology/funding validation; append-only Growth/control/funding and compatible-writer guards | trigger DDL takes relation locks; no data backfill; existing history remains untouched |
| arena 0022 | reserves and player caps use base+Growth with overflow-safe numeric intermediates | replaces functions, preserves triggers; Growth-zero historical formulas retain outcomes |
| arena 0023 | exact GameConfig/policy reference/duration agreement and immutable new-style Epoch commitments | trigger DDL; no data changes |
| arena 0025 | approved pre-Freeze recipient provenance in funding guard | function replacement; immutable history unaffected |
| arena 0026–0027 | durable evaluation history and deferred authorization/placement conservation | new empty table/index and constraint trigger; no history backfill |
| arena 0024 | numeric `div()` for exact floor quotient at signed-bigint extremes | avoids PostgreSQL `/` significant-digit rounding; no stored recalculation |

Unchanged protections include Phase-1 Freeze readiness, append-only allocations/source receipts,
policy/version immutability, canonical source fencing, correction funding, source deletion guards
and Season-close barriers. Source write barriers still reject old workers; schema 4 is unsupported
by pre-Phase-4 parsers. Binding consistency is also checked in PostgreSQL for new-style configs.
Category reserves/caps always use their original frozen commitments and bound policy.

Forward procedure: pause economic orchestration, apply migrations with the ordinary release
migration job, deploy compatible producers and workers together, inspect state/config/policy
compatibility, then request separately authorized activation. Migrations do not themselves grant it.
No Production action is included in this PR.

Rollback is an operational pause first, not reversal of rewards. Do not run pre-Phase-4 software
against active schema-4 Seasons. Keep schema/history and deploy a forward compatibility fix where
necessary. Reversing 0020 reinstates a ≤6 check and removes funding/control tables; it is not safe
once a Season has used those features. Reverse migration must fail rather than delete/reprice data.
0022/0024 reverse function definitions remove Growth/extreme-budget support and require the same
compatibility restriction. Do not truncate data to make a downgrade succeed. Empty disposable
forward/reverse/forward checks are useful migration tests, not a Production rollback certification.

Historical schema 2/3 configs keep fixed official semantics; the new topology parser is selected by
schema identity, not Demo/Official application branches. Existing published payloads/checksums,
Epoch dates and policy IDs are unchanged. New policy rule documents are never inferred from old
Growth scaffolding. The owner addendum replaces the earlier proposal with the documented pre-Freeze rule; normal examples disable Growth.
