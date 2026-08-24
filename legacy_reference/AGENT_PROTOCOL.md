# Agent protocol for legacy references

The phase implementation prompt must explicitly name which legacy files are allowed to be read for that phase.

## Required sequence

1. Read `AGENTS.md`.
2. Read `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`.
3. Read the current implementation for the phase dependencies.
4. Read `legacy_reference/README.md` and `legacy_reference/REJECTED_PATTERNS.md`.
5. Read **only** the legacy reference files explicitly listed in the phase prompt.
6. For each legacy mechanism, classify internally as `REUSE`, `ADAPT`, or `REJECT` before coding.
7. Implement against current models/services. Never import legacy files.
8. Recreate the useful regression scenario in the current test suite when relevant.
9. Do not change current product rules to make legacy code fit.

## Prompt pattern used by the reviewer

A future phase prompt should contain a block similar to:

```text
LEGACY REFERENCE SCOPE FOR THIS PHASE

Read:
- legacy_reference/README.md
- legacy_reference/REJECTED_PATTERNS.md
- legacy_reference/<specific-file-1>
- legacy_reference/<specific-file-2>

Do not read other legacy_reference subdirectories unless the task discovers a
concrete blocker and the repository owner/reviewer expands the scope.

Reuse only technical mechanisms marked REUSE/ADAPT.
The current Source of Truth wins on every product/economy decision.
```

This keeps token consumption bounded and prevents accidental resurrection of obsolete behavior.
