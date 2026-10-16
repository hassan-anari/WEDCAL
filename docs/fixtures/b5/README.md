# Actual B5 fixtures

`openapi.json` is generated from the current Ninja router, including transitive schemas and stable error envelopes. `clan-http.json`, `lifecycle-http.json` and `authority-http.json` are responses produced by isolated PostgreSQL HTTP integration tests, using signed synthetic Mini App identities and synthetic Telegram transports. No production Player, credential, campaign or historical obligation is represented.

Regenerate OpenAPI: `uv run python scripts/export_b5_openapi.py`; CI runs `--check`.

Regenerate HTTP fixtures: `B5_EXPORT_FIXTURES=1 uv run pytest apps/clans/tests/test_b5_integration.py apps/clans/tests/test_b5_authority_recovery.py -q` with isolated PostgreSQL/Redis/broker test settings. Timestamps/UUIDs reflect test generation; field names, state codes and amounts come from real services/config. Synthetic high-balance source data exists only to exercise Shop/HQ states, not as a production reward recommendation.

Consumer must pin the runtime SHA in B5 handoff/PR, preserve B2/B3/B4 fixtures, map absent/unknown fields explicitly and never fall back to demo economics. Treasury/history and private lifecycle data require membership/authentication. No Telegram identifiers/media file IDs or real private URLs are exported.
