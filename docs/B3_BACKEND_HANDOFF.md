# B3 backend handoff — in progress

Branch `ai/b3-backend-integration`, targeting `main`; one Draft PR. No merge/deployment/production
mutation or Telegram mass sending. Owner explicitly authorized B3, including session recovery,
banners and bot operations; older roadmap batch scope is expanded by that instruction.

Read [B3_BACKEND_CONTRACT.md](B3_BACKEND_CONTRACT.md) for implemented versus pending routes.
Initial implementation includes fenced-client auto-acquisition prevention, exact-generation
best-effort release, explicit ownership read states, and the shared local limiter retry fix.

Initial PostgreSQL tests cover phone/desktop switching, old-generation rejection, expiry,
stale release safety, immutable lost-response receipt recovery and temporary limiter retries.
Full regression/latency/security/CI evidence is still pending. The final handoff will record
exact SHA, migrations, commands, counts, CI links, latency and compatibility limitations.

FROCAL: preserve local outbox evidence, use explicit takeover only after a user click, never
loop takeover after `GENERATION_STALE`, and distinguish ownership, storage, auth and transport
failures. All added snapshots use server data; sample frontend earnings/tiers/group counts must
be removed as production authority. Pending schemas in the contract are not integration-ready.

Discovery: inspected both actual main HEADs and repository instructions; frontend has no root
AGENTS.md at the pinned HEAD. Reviewed the Season Source of Truth, economy contract, integration
roadmap and actual existing Daily/Mission/Typing/Referral/Network services. Local source was
synchronized to the exact B2 tree, then Git transport verified outside the network-isolated
command sandbox. No B2 economy rebuild or historical configuration update is included.
