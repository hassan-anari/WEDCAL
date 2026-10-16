# B4 backend handoff — initial coordination

Batch: P-08/P-09 plus owner's mandatory B3 production corrections, one WEDCAL Draft PR.
Branch: `ai/b4-backend-integration`, based on merged main
`1fa937af4628a10cec384b711210755ff28a6a8a`.
Frontend inventory: FROCAL main `ae91624f23a48d8a9973154520284706a9df1c21`.

Read [contract](B4_BACKEND_CONTRACT.md), the cross-repo integration register and B3 contract.
The initial publication allows frontend inventory/contract discussion while runtime changes are
underway; it does not claim final API compatibility, green CI, or batch completion.

Frontend: provide the paired B4 PR URL here/in PR comments. Consume committed DTO/OpenAPI and
fixtures only, pin final backend SHA as in B3, preserve approved visual flows and locked features.
Use actual canonical balances; no synthetic achievements/ranks/rewards or local XP calculations.
Backend will publish implementation status, typed errors, verification evidence, migration SQL,
rollout/rollback and security review before independent review.

No merge, deploy, production data modification, real test-send or public message is authorized.
Public Welcome remains disabled. All verification must use isolated synthetic data and mocked
Telegram transports. Stop after the completed green Draft PR for independent review.
