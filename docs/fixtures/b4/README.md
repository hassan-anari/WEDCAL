# B4 runtime fixture provenance

`openapi.json` is generated from actual Ninja routers and their transitive schemas by
`python scripts/export_b4_openapi.py`; CI rejects drift. Paths include B3-compatible surfaces
and implemented B4 additions. Typed success/error bodies are defined in these schemas.

HTTP fixtures are generated using isolated PostgreSQL tests in
`apps/phase4_tests/test_b4_contracts.py` with `B4_EXPORT_FIXTURES=1`. They use synthetic
configuration, identities and timestamps; numbers are not production economy policy:

- `profile-earn.json`: authenticated Profile, claim/canonical progression and confirmed receipts.
- `typing-history.json`: real valid/rejected submissions, keyset pagination and omitted unfinished run.
- `rankings.json`: real projected pages, drift failure and immutable archive behavior.
- `dlx-confirmed.json`: settled allocation and audited hold/release/forfeit/reversal, no XP mutation.

UUIDs, timestamps and cache capabilities are examples, never production identifiers or live
credentials. Preserve frontend B3 history and pin B4 fixtures with the exact runtime commit.
