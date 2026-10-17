# B5 active join-request visibility correction

Scope: existing WEDCAL Draft #30, following runtime `70f27ec73b926e969c77341f2db98edaba48dcab`. The owner-relayed FROCAL real-API regression showed that 25 newer terminal requests hid an accepted invitation and older-Season active reservations were excluded. This correction is part of B5, with no migration, new reward, UI redesign, merge, deployment or real Telegram send. Owner PH-048 documentation commit `b171fd9b5ecaa750288281e3ec7c5c09ba1a87c2` remains unchanged.

## Implemented behavior

`GET /api/v1/clans/join-requests/current` retains its list root and existing fields. Its single application SQL statement now returns:

1. Every authenticated owner's PENDING/ACCEPTED application across every Season, and every request with an unsafe JOIN credential, regardless of terminal-history limit/offset. No legacy winner is silently selected, including more than 100 overlapping active paths.
2. Only safe terminal current-Season history is paginated, newest first, using `limit` (default 20, clamped 1–100) and additive `offset` (default 0, clamped to zero). Blocking paths are returned first, newest first within their section; each row appears once. The total list can exceed 100.

Additive `clan_season_id`, `season_id`, `season_code`, `is_current_season`, `is_active`, `reserves_capacity`, `credential_unresolved`, `blocks_new_application` and `handling` describe original ownership and lifecycle. The unsafe credential SQL predicate is shared with the existing write-side eligibility check: issued/creating/uncertain/revoking/manual states, unknown-risk flag or retained link. A terminal request label cannot paginate an unresolved credential away. `reserves_capacity` reflects the existing ACCEPTED reservation accounting, rather than inventing new reservations behind terminal labels.

`handling` is HISTORY for safe terminal history, OPERATOR_REQUIRED for older-Season blocking paths or terminal labels with unresolved credentials, and otherwise CURRENT_SEASON. CURRENT_SEASON describes ownership; it is not permission or proof of mutation eligibility. With no current eligible Season, blocking paths remain readable, all require authorized handling, and no terminal history is returned. The response never includes invite URLs, Telegram identifiers or another applicant's data. Existing authentication and private/no-store protections remain.

`GET /invite/current` rejects multiple active applications or a sole older-Season active application using the existing safe HTTP 409 CLAN_STATE_CONFLICT, before credential lookup/creation. It does not select the newest legacy path or extend an old Season's issuance/activation window. Current single-path credential behavior remains. Existing explicit safe cancellation, revocation and source-time activation obligations remain in their established services; displaying a row never cancels, transfers or activates it. Unsafe accepted cancellation continues to retain ACCEPTED and capacity until safe terminalization.

## Consumer selection and paired fixtures

FROCAL Draft #6 must pin the new actual runtime from Draft #30's final handoff, consume the complete list and the additive typed fields, and separate blocking/active paths from history. Never choose response[0], apply a frontend history slice to active rows, infer release from clicking Cancel, or remap older IDs into the current Season. Detect every overlapping path before choosing a single current-Season application; use existing authorized lifecycle/operator handling for legacy and older-Season records. `/invite/current` returns a typed conflict for ambiguity rather than an implicit credential winner. A list with no blocking JOIN records still does not prove general eligibility: membership, unsafe REJOIN credentials and other existing server checks remain independent.

The generated OpenAPI includes the new fields and history-only offset. Regenerated B5 lifecycle/HTTP fixtures retain actual response values. New real PostgreSQL HTTP fixtures:

- `request-history-isolation-http.json`: accepted failed-revocation reservation, followed by the default history page despite 26 newer terminal rows.
- `request-visibility-http.json`: every older/current legacy active path despite offset 999, with explicit original Season IDs and safe typed ambiguous-invite conflict.

No frontend implementation or device acceptance is claimed here. FROCAL must publish its updated consumer SHA, verify the full list rather than selecting its first entry, and rerun paired real-API scenarios against the new exact backend SHA. Historical B2/B3/B4 pins remain.

## Regression and validation evidence

`apps/clans/tests/test_b5_request_visibility.py` uses real PostgreSQL transactions and authenticated signed synthetic Mini App HTTP calls. Coverage includes 26 newer terminal rows at default/min/max history limits and past-end offsets; older-Season accepted requests; all legacy overlapping active rows; 101 pending paths; inconsistent terminal labels backed by unsafe old credentials; no current Season; history-only pagination and one application SQL/no N+1; authentication/owner isolation; fail-closed old/ambiguous invite access; explicit resolution of a current overlap; retained failed-revocation reservations; and unchanged old-Season activation boundaries. Telegram transports are synthetic only.

Fixture generation passed 89 authenticated/domain integration tests. The final full suite also includes the additional current-Season overlap-resolution regression. Ruff/format, mypy (437 source files), Django checks, repository contract, migration drift and B2/B3/B4/B5 OpenAPI gates passed locally. Final full-run, benchmark and exact-head hosted results are published in the final Draft #30 handoff; previous green heads are not substituted.
