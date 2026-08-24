# Dexo Land Backend

This repository is the independent backend for Dexo Land. It is not an extension of the legacy Dalexio exchange backend.

## Required reading

Before making changes, read both repository contracts in full:

- [`AGENTS.md`](AGENTS.md) — development and agent governance rules.
- [`DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`](DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md) — the canonical Season 1 product and backend contract.

## Phase-gated development

Every development phase uses its own pull request and must pass its required GitHub Actions checks. Green CI is necessary but is not approval to continue: the repository owner must explicitly approve progression. Coding agents must stop after a phase PR becomes green and must not start the next phase or merge a PR unless the owner explicitly authorizes it.

Branch names should identify the phase and slice, for example:

```text
ai/p00-contract-ci-gate
ai/p01-foundation-health
ai/p02-telegram-webhook
ai/p03-reward-ledger
```
