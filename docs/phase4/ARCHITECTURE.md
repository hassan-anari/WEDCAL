# Configurable Season architecture

The owner Phase 4 instruction supersedes the historical fixed Season-1 topology for **new schema 4
configurations**, without rewriting the canonical historical contract or published schema 2/3 payloads.
Published GameConfig is the setup entry point. `dlx_distribution.economic_policy` explicitly names
an immutable `EconomicPolicyVersion` by key/version/checksum. There are no copied economic formulas
in the GameConfig. An approved policy must already exist before new-style publication. Its
approval reference is independently auditable; existence does not activate workers.

Publication validates the complete existing gameplay configuration plus setup. Activation validates
the selected document, its checksum and approval, checks `policy.days == timing.epoch_days`, initializes
exactly the configured Epoch count and binds the same policy to each Epoch under the Season barrier.
The administrator supplies source-coverage evidence and needs `arena.bind_economic_policy`.
The admin activation view displays the published config identity/checksum and collects evidence.
Draft Season dates must match the approved product period. No database seeding, policy approval,
Season activation, worker authorization, XP reset or settlement is performed by migrations.

## Ownership and immutability map

| Values | Single authoritative object/field | Pinning boundary |
| --- | --- | --- |
| Total duration, Epoch count/days, closing days | baseline GameConfig `dlx_distribution.timing.*` | Season activation; all Epoch boundaries derived from it |
| Default settlement gate | baseline GameConfig `default_settlement_start_epoch` | copied to audited operational Season field on activation |
| Operational gate changes | `Season.settlement_start_epoch` + `SeasonSettlementControlEvent` | mutable by audited control before settlement starts; never by ordinary republication |
| Base/maximum budgets | baseline GameConfig `pool.base_dlx_amount/max_dlx_amount` | activation; base commitment also immutable on each new-style Epoch |
| Category weights | baseline GameConfig `category_weights_bps` | activation; category commitments on Freeze |
| Milestone keys, thresholds, amounts | baseline GameConfig `pool.growth_unlock.milestones` | activation; unique unlock event and normalized funding placements |
| Growth counting/placement | economic policy `growth.rule/approval_reference` | independent approval and Season binding; exactly one rule |
| Qualification age/days, source recognition, scoring weights/saturation, category participation targets, minimum score, cap, Carry release, reserve fraction | bound `EconomicPolicyVersion.payload` | immutable document and per-Epoch binding |
| Daily amounts/reset, Typing attempts/formula, supported Tap settings, personal Mission rules, progression thresholds, Referral/Creator rates and anti-abuse, retention, Clan activities/Treasury/item prices/effects | effective immutable GameConfig namespaces | existing source-time or transaction/burst entitlement boundary; source config FK persists |
| Clan Mission target/requirements/window/kind/reward-band selection | versioned `ClanMissionDefinition` and activation history | activation or prior use; new semantics require a new version |
| Clan Mission reward-band amounts | definition's compatible GameConfig `clan.missions.reward_bands` | definition + ClanSeason config compatibility; never repriced mid-Season |
| Ranking minimum/ties, medals/season honors | baseline GameConfig | Season; immutable final standing/award records |
| Ratios denominators, ledger conservation, allowed category identities, source authenticity, uniqueness, state transitions, bigint/storage bounds, authorization limits, immutable history | code + PostgreSQL constraints/triggers | structural invariants, never editable economy JSON |
| Batch sizes, queue scans, retries, task time limits, connection pools | runtime settings or explicit bounded service arguments | operational process configuration; no effect on earned amounts |

`config/schemas/season-setup.schema.json` documents setup fields. `parse_economy_sync` and
`selected_economic_policy` enforce cross-field constraints that JSON Schema alone cannot express.
Examples: beta 15=2×7+1; official 45=6×7+3; alternate 23=3×7+2. PostgreSQL tests also use 17=3×5+2
and 26=8×3+2, each with a compatible pinned policy. Examples preserve existing gameplay values.
Their policy has Growth disabled; importing examples is not approval or activation.

## Exact accounting

Base B over n Epochs: each gets floor(B/n), with B mod n units assigned one each to earliest Epochs.
Schema 4 uses canonical category identity order, flooring each weighted portion and assigning the
remainder to the final category. Legacy category order/semantics remain unchanged. Base and Growth
are split independently with exact remainders. Fresh category funding is their sum; participation,
caps and reserves all use that sum. Same-category Carry materializes only from the previous settled
Epoch. No money crosses categories, no closing Epoch is fabricated and no XP converts into DLX.
See [Growth proof](GROWTH_POLICY.md) and measured/synthetic evidence.

## Prospective publication

Ordinary tuning can publish future snapshots with the identical active Season setup/policy reference.
A changed setup cannot become effective during an activated Season, even at an Epoch boundary.
It may be scheduled for a later Season after the current product window. Gameplay sources retain
old immutable config references: in-flight Tap bursts, Typing credits, Mission entitlements,
Clan settlements and purchases do not reprice when the global effective config changes.
Daily reset-policy changes still require the old and new reset boundaries to coincide. Existing
legacy Tap publication constraints remain. No Clan Mission band migration/repricing is introduced.

## Frontend dependency

Bootstrap has additive nullable `season.timing` containing total days, Epoch count/days, closing days,
economic end and settlement gate from the **pinned baseline**. All OpenAPI exports are updated.
Clients should consume it and the supplied start/end timestamps instead of assuming 6/42/45 or
seven economic weeks. Existing routes and off-chain/nonclaimable balance semantics are preserved.
No FROCAL files or frontend implementation were changed.
