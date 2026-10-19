# Read-only economic Admin inspection

All economic audit models are registered once, including the missing Phase 3 models:
EconomicPolicyVersion, EpochPolicyBinding, EconomicRecognition, EconomicDeferral,
EconomicDeferralEvidence, EconomicDeferralResolution, EconomicReviewDecision, EpochSourceManifest,
CategoryCorrectionReserve, CorrectionAssessment, DlxCorrectionRound, DlxCorrectionEntitlement,
DlxCorrection, DlxCorrectionAttempt, DlxCorrectionAdjustment and CreatorContentClass.
PerformanceSourceFact and EpochReadinessEvaluation are included, together with new Growth funding,
GrowthEvaluation and automation control/attempt records. Existing registrations remain available.
The final continuation also registers EconomicPolicyProposal, EconomicPolicyApproval, EconomicAlert,
EconomicAlertEvent and EconomicAlertDelivery with the same read-only restrictions. Recipient routing
JSON is redacted/deferred. Proposal/approval and alert events have database immutability guards;
delivery routing is pinned. Policy registration and manual DEAD-delivery retry use permissioned
services/commands, never an editable Admin audit model.

Every audit inspector denies Add/Change/Delete, has no actions or editable inlines and uses Django
model view permissions. Explicit economic overview permission (`arena.view_economic_overview`)
is separately required for read-only Season/Epoch overview routes; active staff status is required.
The routes accept GET only. Existing reviewed correction/qualification services remain the only
business mutation workflow. Inspection cannot approve policy, adjust amounts, change source evidence,
bind policy, Freeze, settle or execute SQL.

Lists use deterministic descending IDs, 50-row pages, no show-all expansion or full-result count,
select_related, exact stable-ID/milestone search, state/category/outcome filters and bounded
latest-100 Season/Epoch filter choices. JSON, raw text, fingerprints and confidential references
are deferred from ordinary list queries. A 201-row PostgreSQL list regression verifies one 50-row
page with fewer than 30 queries. Counting a very large filtered list may still require PostgreSQL
index/table work; no unlimited history materialization is performed in Python.

Details expose only digests and a whitelist of safe economic provenance keys. Raw proof/input JSON,
secret tokens, Telegram message content, Creator fingerprints and unnecessary Telegram identities
are never rendered; Player.__str__ is deliberately avoided. Sensitive JSON lookup URL parameters
are rejected, preventing redacted-field filtering as a disclosure path. Model-level view permission
also covers inspection of policy parameters. Hidden source IDs, confidential approval/evidence
references, free-text reasons and Telegram identities cannot be probed through list filters.
Season/Epoch policy filters resolve immutable bindings; Growth Epoch filters include both the
recognition Epoch and actual receiving placements. Filters are omitted where no Epoch relation exists.
Model-level view permission is required even for redacted details. Related audit navigation uses internal IDs and permission-checked
routes. Raw evidence investigation stays in separately permissioned reviewed domain workflows.

Season pages link to canonical economics: base/max/Growth/authorized totals, placement sum,
qualified Season-to-date counter and cutoff/proof state, milestone status/provenance, Epoch states,
allocations, Carry, reserve funding/payments, outstanding deferrals/reviews, reconciliation and recorded
final residual. Epoch details link to preparation/readiness/blockers, policy/checksum, category budgets,
Growth, Freeze/settlement timestamps, allocation and correction totals and canonical reconciliation.
Missing preparation/finalization/residual evidence is reported as missing, never successful.
Current same-category Carry comes from the latest settled Epoch; historical inflow/outflow totals
are labeled separately and are not an available balance. Policy listings and related records show
key/version/checksum. Authorized policy details expose known numeric parameters, canonical
participation targets and rule identity, not arbitrary payload keys.

GrowthUnlockEvent plus its immutable GrowthEpochFunding navigation answers where each authorized
unit went. GrowthEvaluation rows preserve repeated evaluations, counts/digests, cutoff, outcome and
config/policy identity. Zero-funded NO_ELIGIBLE_MUTABLE_EPOCH decisions remain distinct from FUNDED.
Example reports are in the review artifacts; they contain synthetic identities only.
