# Legacy Reference Pack

**Status: NON-AUTHORITATIVE HISTORICAL REFERENCE**

This directory is a curated extraction from the old Dalexio backend. It exists to avoid re-developing already-proven technical mechanisms while preventing legacy business rules or architecture from leaking into the new Dexo Land backend.

Source archive SHA-256: `edd8d5e2d5e31fa50108bf6b98fb989ff3682e3b3b55bf0568c370b31521ba41`

## Hard rules

1. Never import anything from `legacy_reference/` into production code.
2. Files use `.py.txt` intentionally so Ruff/mypy/Django do not treat them as application modules.
3. `AGENTS.md` and `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md` always override legacy behavior.
4. Reuse algorithms, defensive checks, transaction/concurrency patterns, migration techniques, and regression scenarios only when the manifest marks them `REUSE` or `ADAPT`.
5. `REJECT` means historical lesson only. Do not copy it.
6. Do not scan the whole pack in every phase. Read only the files explicitly assigned in the phase prompt (see `PHASE_READING_MAP.md`).
7. The original ZIP is intentionally not committed here.

## Why the full legacy repository is not included

The old repository mixes useful Telegram/Redis/concurrency code with obsolete exchange coupling, direct score mutation, old referral economics, Redis-buffer correctness, and removed product behavior. A curated pack reduces token usage and copy-by-accident risk.
