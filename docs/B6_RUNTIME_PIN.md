# B6 runtime/source pin

Runtime implementation and API/schema pin: **`13e7b1988181980b6ad80b69a8e9f28347e31802`**.

This supersedes initial implementation `63b1c2c95f618266c9a47638308b70df372c7dcc` with the Admin stale-edit fence. The additive API schema is unchanged. Subsequent documentation/representative-fixture-only commits do not change this runtime; the exact final review/artifact head and its required CI are posted in [Draft PR #31](https://github.com/hassan-anari/WEDCAL/pull/31).

Pinned [contract](https://github.com/hassan-anari/WEDCAL/blob/13e7b1988181980b6ad80b69a8e9f28347e31802/docs/B6_BACKEND_CONTRACT.md), [OpenAPI](https://github.com/hassan-anari/WEDCAL/blob/13e7b1988181980b6ad80b69a8e9f28347e31802/docs/fixtures/b6/openapi.json), [operator guide](https://github.com/hassan-anari/WEDCAL/blob/13e7b1988181980b6ad80b69a8e9f28347e31802/docs/B6_OPERATOR_GUIDE.md), [security/economic review](https://github.com/hassan-anari/WEDCAL/blob/13e7b1988181980b6ad80b69a8e9f28347e31802/docs/B6_SECURITY_ECONOMIC_REVIEW.md).

Required migrations: missions.0003, clans.0017, clans.0018, as named in the review. No production migration/backfill has run. Prospective Treasury carry starts at zero per ClanSeason/config; historical records remain unchanged. No numerical rebalance, deployment or live bot send is authorized.

Frontend coordination: [FROCAL #8](https://github.com/hassan-anari/FROCAL/issues/8). FROCAL must explicitly acknowledge and pin this exact WEDCAL runtime revision (or the explicitly posted final artifact head with identical runtime/schema) before implementation or paired tests. WEDCAL does not assume receipt or acceptance. When a frontend B6 Draft exists, link it there. Routing fix #7 remains unchanged.

Runtime check runs: [Contract](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192356), [Foundation](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192378), [Release Scans](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192307). Final states and test counts must be read from the completed checks/PR handoff, not inferred from these links existing.
