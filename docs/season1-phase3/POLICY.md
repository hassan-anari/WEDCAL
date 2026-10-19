# Season 1 finalized economics v1 — Gate B review docket

Status: implementation for a Draft PR. The owner authorized Gate B and approved the exact Clan thresholds (100 Contribution saturation; 10 Contribution/day consistency). This document supersedes conflicting Gate A recommendations under the final owner direction. No migration seeds or binds a policy, enables a task, freezes an Epoch, or allocates live DLX. Historical suitability and operational activation require separate owner authority.

The executable reference is `config/examples/season1.economic-policy.v1.json`; its checksum and independent approval/evidence references are stored in an immutable policy version and explicit six-Epoch binding. Existing published GameConfig history is preserved. All XP rates, inventory effects and gameplay limits remain GameConfig-controlled. The canonical Source of Truth was not edited.

## D1–D16 decision matrix

“Changed” compares with the preceding Gate A review direction. Numerical foundation accepted by the final owner is retained, except the expressly approved canonical Contribution units and simplified qualification/recognition rules.

| Decision | Disposition | Final recommendation / implementation |
|---|---|---|
| D1 Qualification | Changed by final owner simplification | Trusted observation age ≥24h by cutoff; two distinct independently meaningful Epoch-relative days, OR three actual Daily periods plus an independent Typing/Mission anchor. Tap-only and Daily-only do not qualify. Four durable outcomes: QUALIFIED, NOT_YET_QUALIFIED, PENDING, DISQUALIFIED. Review-pending is scoped. No unique-human claim. |
| D2 Epochs / pool | Unchanged | Six half-open seven-day Epochs; 60M base; 100M maximum authorized; 10M fresh/week. No seventh Epoch. |
| D3 Economic evidence / arithmetic | Unchanged foundation; explicit new receipts | Exact integers, immutable normalized receipts, unique source type/ID, policy/config references and reproducible hashes. RewardLedger totals do not define Core or Loyalty. |
| D4 Closure / late work | Changed by owner amendment | No mandatory 48h wait. Exclusive Season barrier, content-free source census, finalized receipts and explicit durable deferrals. Late unfinalized Telegram/Clan work may be first recognized in the real Epoch containing finalization. Original source timestamps remain intact. Already-finalized sources never move. Unscopable integrity gaps block. |
| D5 Category isolation | Strengthened / required | One primary monetary category per allowlisted source; Loyalty may additionally recognize distinct eligible actor days. No Claim/projection/passive-share inflation. Mapping below. |
| D6 Clan | Changed units, owner approved | 80% bounded canonical Contribution +20% days earning ≥10 Contribution; saturation 100. Includes legitimate Booster-adjusted Contribution and verified canonical CLAN_MISSION. No additional HQ/Treasury/Clan Score/captain term. |
| D7 Core | Changed from early Mission-heavy proposal | Tap25%, Typing60%, Daily10%, independent Missions5%. Daily max seven credited periods even if eight UTC dates overlap. Once-per-definition-version Mission verification counts once, before Claim. |
| D8 Creator | Revised durable evidence / bounded breadth | Historically authorized owner, eligible registered group, authenticated normalized VALID source, finalized window, distinct qualified speakers with independent Typing/Mission anchor. Shared speaker-days H, bounded valid windows W. Content-free duplicate classes survive normal content expiry; lookup fingerprints expire. |
| D9 Referral | Changed activation dependency | Existing Level5 and moderation/anti-Sybil checks plus independent, auditable participation eligibility. Actual verification time determines recognition. No completed future Epoch prerequisite. Passive share and pending invitations produce no acquisition. |
| D10 Loyalty | Unchanged bounded intent, explicit sources | 10,000×meaningful distinct Epoch-relative days/7, floored. Actual Daily, verified Typing/Mission, meaningful Contribution, eligible community activity. No raw requests, ledger counts, passive membership or Tap-only spam. |
| D11 Emission / concentration | Retained final-owner reference baseline | Minimum category score100; participation targets Clan1000/Core1000/Creator500/Referral1000/Loyalty1000; player cap1% fresh category budget; carry release≤25% current normal category budget before participation factor; 5% fresh reserve. Residual intentionally large at tiny populations. |
| D12 Allocation | Unchanged | Deterministic capped proportional waterfill, then largest integer remainders with ascending player-ID tie-break. No floating-point money. |
| D13 Carry / Growth | Unchanged | Same-category carry only; empty Growth milestones; no automatic extra40M authorization or XP conversion. Reserve never enters normal carry. |
| D14 Chronology / immutability | Unchanged protections | Existing Epoch3 operational start gate, settle ascending Epochs, immutable allocations and frozen snapshots. Database guards remain and new guards strengthen binding/source/provenance/funding. No custom strategy under a bound policy. |
| D15 Positive corrections | Changed / implemented | Funded append-only complete-category final correction round after the 45-day admission window; immutable assessments, exact positive differences, original cap and minimum score, replace Creator H rather than add old H. Full round paid atomically only if funded; otherwise durable INSUFFICIENT_RESERVE. Never rewrite normal allocations or silently prorate an approved entitlement. |
| D16 Close / operations | Clarified by explicit owner answer | No automatic weekly Claim. Retain existing audited Mission/Typing auto-settlement only at Season Close. Final reconciliation includes corrections, adjustments, holds, carry, unused reserves and residual. Unresolved economic cases prevent a falsely successful final close. No activation in this PR. |

## Exact Qualified Player v1

Cutoff is the Epoch end; intervals are `[starts_at, ends_at)`. Age is based on durable server authentication/Mini App evidence or authenticated Telegram ingress linked to a normalized activity, never client-reported age or an unexplained `first_seen_at`. The observation must be at least24h old. Relevant historical moderation and explicit versioned negative/pending eligibility decisions apply; a flag is review-pending, not an automatic ban. Authorized reviewed clearance/disqualification is append-only.

A meaningful independent day has at least one valid Typing CREDIT or compatible independently verified personal Mission, canonical Contribution totaling≥10, or at least two distinct eligible normalized community source identities. Daily establishes a Loyalty day; the alternate qualification route needs three distinct claimed periods plus a Typing/Mission anchor. Base Tap alone, invitations, passive referral share and passive membership do not establish these routes. Every eligible player may earn in multiple categories. Eligible speakers must also have an independent Typing/Mission anchor before contributing Creator H/W; community-only participation can qualify the speaker's own Loyalty without manufacturing Creator breadth.

This raises automation cost using existing server-verifiable facts; it does not prove unique people. A coordinated network completing the same valid gameplay and activation requirements as legitimate accounts can still qualify. No sensitive identity collection, device fingerprint inference, account-linking assumption or AI content grading is added. Existing moderation and source validity controls remain in force.

## Authoritative source mapping

| Existing earning source | Primary performance | Additional Loyalty day / exclusions |
|---|---|---|
| BASE_TAP_V2_BATCH / eligible legacy BASE_TAP_BATCH facts | Core Tap accepted counts, validated against config and canonical v2 ledger where applicable | No Tap-only Loyalty or qualification; temporary lease/receipt is not sole history |
| VALID Typing attempt + completed wallet CREDIT + source fact | Core Typing source-time quality | Meaningful day/anchor; exclude TYPING_CLAIM, passive TYPING_REFERRAL_CREDIT and later share settlement |
| DailyClaim + matching DAILY canonical reward | Core Daily distinct claim periods, capped7 | Distinct participation day; no fabricated Daily from arbitrary XP |
| Activated compatible independent personal Mission + immutable verification fact | Core Mission once per key/version, maximum4/week | Verification-day participation and anchor. Exclude Typing wrappers, incompatible INTERNAL brief, repeated Claims, unactivated definitions |
| ClanEconomicSettlement + matching ClanContributionEntry | Clan actual Contribution, including approved Booster and valid CLAN_MISSION participant settlement | Contribution≥10/day meaningful; no extra HQ/Treasury/ClanScore/captain reward score |
| Finalized eligible Creator windows + historically authorized owner + authenticated VALID speaker sources | Creator H/W | Speaker activity may establish a meaningful community day; no owner credit for own speech, unverified activity or expired unknown evidence |
| New REFERRAL_ACTIVATION fact + verified Level5 relation and activation audit | Referral acquisition of inviter, actual verification time | Invitation/passive share/Claim add nothing; acquisition alone is not participation |
| Eligible Clan normalized community activity | No additional monetary category beyond canonical Contribution | Distinct eligible speaker days establish Loyalty; no second Core/Creator monetary score |
| Admin XP, captain reward, HQ/Treasury, projections, Claim/repeated settlement | No independent DLX performance | Canonical underlying allowlisted sources may already count; derived XP does not re-enter scoring |

Mission key allowlisting is necessary but insufficient: published GameConfig must enable the verifier and compatible definition; definition must actually be activated and its destination approved. `read-season-brief` is not active merely because code exists. A server dwell/token proves only completion of that verification strategy, never reading comprehension or human uniqueness. No new Mission or earning rate is introduced.

## Exact scores and test vectors

All scores use scale10,000. `F(x,k)=floor(10000*x/(x+k))`. Every day is an elapsed24h bucket relative to Epoch start. Integer floors occur exactly where shown, not after arbitrary floating-point rearrangement.

**Core** = `floor(2500*sum(F(taps[d],500))/(7*10000)) + floor(6000*sum(top3 quality[d])/(7*3*100)) + floor(1000*min(distinct actual Daily periods,7)/7) + floor(500*min(distinct independent key/version verifications,4)/4)`.

Typing quality per VALID CREDIT is `floor(min(wpm,100)*accuracy_bps/10000)`; retain the best3 per day. Five valid attempts do not create five full-scoring slots. Eight overlapping UTC Daily dates still score≤1000. Verification in Week1 and Claim in Week2 creates no Week2 Mission performance. Two definitions→250 Mission units, maximum4→500. Completing all four in Week1 versus Week6 gives the same six-week total, with otherwise identical full activity: `[5750,5250,5250,5250,5250,5250]` versus `[5250,5250,5250,5250,5250,5750]`, total32,000. The 5% maximum limits the continuing disadvantage for early completers; no recurring replacement reward is invented.

Tap accepted counts `[0,1,499,500,501,1000,100000]` give F `[0,19,4994,5000,5004,6666,9950]`. Daily taps500, three quality50 Typing credits/day, seven Daily periods and two Missions produce Core `1250+3000+1000+250=5500`.

**Clan** = `floor(8000*sum(F(canonicalContribution[d],100))/(7*10000)) + floor(2000*count(d where Contribution[d]>=10)/7)`.

Seven equal daily Contribution values0/10/40/80/100/115 give scores0/2727/4285/5555/6000/6278. Six100 days plus one0 gives5142. The current audited 1500bps Contribution Booster on gross100 records115 Contribution; its legitimate increase improves the score. Canonical CLAN_MISSION Contribution is included once. Character-only scoring is replaced: two players with equal message lengths but Contribution40/day versus80/day now differ4285 versus5555. A Mission participant with verified Contribution counts even without additional message characters. No linguistic quality metric or synthetic Mission multiplier is used.

**Creator:** aggregate historical eligible source receipts across groups of the same owner. For an owner/speaker/recognized-day, require≥2 distinct duplicate classes, ≥2 windows, and≥1800s original source-time span. Only qualified, independently anchored speakers other than owner qualify. Each speaker/recognized-day has exactly10,000 H units shared equally among all eligible owners, with ascending owner-ID remainder tie-break; extra groups never multiply that speaker-day. Let H be those fixed-point units. A W window requires≥3 distinct eligible speakers and≥600 nonduplicated characters; retain at most4 W/day (28/week), suppress duplicate content classes across that owner's groups/day. Score = `floor(8000*H/(H+20*10000))+floor(2000*W/28)`.

Permanent receipts contain random opaque duplicate-class IDs and normalized source facts, historical owner, speaker, config, window and timestamps. Raw text and fingerprint lookup are not needed to reproduce an already sealed H/W score. Fingerprint lookup is removed at original expiry. Once expired, a never-sealed historical event cannot be reconstructed by guessing a new class: it becomes an evidence-gap case. A source already recognized is skipped before fingerprint checks. Ten owners sharing the same three full-week speakers receive total H21 speaker-days, not210; repeated replay is invariant. W repetition alone cannot create unlimited emission because emission also bounds total eligible H.

**Referral** = `F(number of distinct newly verified activation relations,5)`; one→1666, five→5000. Only actual verified acquisitions count. Level3 at Week1 cutoff means0; verified at Level5 in Week2 means Week2 acquisition. Activation uses independently proven participation without requiring a future Epoch decision; self/cycle, configured age, moderation and existing checks remain.

**Loyalty** = `floor(10000*distinctMeaningfulDays/7)`, maximum10,000. Derived requests/ledger events cannot add days.

## Emission, carry and residual

For each category/Week, B is fresh budget, C prior same-category carry. Reserve R=`floor(B*500/10000)`; normal N=`B-R`. Minimum eligible score is100. Effective participants P=number of eligible category players, except Creator P=`min(owners, H/(7*10000))`. Use exact rational P. Target T is1000 except Creator500. Participation factor a=`min(1,P/T)`.

Normal emission limit E=`min(N+C, floor((N+min(C,floor(N*2500/10000)))*a))`. Allocate min(E, aggregate player caps) proportionally; individual cap=`floor(B*100/10000)`. Carry out=`B+C-normalAllocated-R`. Conservation per row: `B+C = allocated + reserve + carryOut`. Correction spending consumes reserve, never carry. Remaining reserves are final Season residual.

| Population, all five categories genuinely eligible | Week1 normal DLX | Week2 normal DLX | Six-week normal DLX | Final residual including unused reserves |
|---|---:|---:|---:|---:|
| 2 | 22,800 | 28,499 | 165,295 | 59,834,705 |
| 1,000 | 9,500,000 | 9,500,000 | 57,000,000 | 3,000,000 |
| 10,000 | 9,500,000 | 9,500,000 | 57,000,000 | 3,000,000 |

These are hypothetical per-category beneficiary upper bounds, not a complete account/referral lifecycle or a prediction of Production eligibility. In particular two accounts cannot both acquire each other through a prohibited Referral cycle; the all-category two-beneficiary row must never be read as permission to do so. Two-player Creator H can count eligible conversation but cannot satisfy the three-speaker W requirement; positive H remains sufficient for the minimum category score. Creator H always conserves one eligible speaker-day/account/day. Other categories assume actual qualifying activity and activated Referral relations. Fixed-point rounding explains28,499 rather than28,500. At1000 versus10000 participants, the same pool is divided among more players, not multiplied.

A feasible two-player scenario with two valid Typing days each and no Clan/Creator/acquisition earns Core+Loyalty only: Week1 5,700; Weeks2–6 7,125 each; six-week normal allocation41,325 and final residual59,958,675. This is also exercised in the PostgreSQL six-Epoch test.

Large tiny-population residual is intentional under the accepted conservative targets. Early eligible players receive positive allocations; they do not automatically receive millions merely for arriving first. The cost is materially lower early total distribution, which is an explicit economic tradeoff. Carry cannot later be dumped automatically: each later Week can release at most25% of that Week's normal category budget, further reduced by participation and player caps. Core example:≤475,000 carry release/week before participation. Reserve3M total is already within60M; unused reserve and final carry remain unallocated; Growth[] unlocks none of the extra40M.

Reproduce all figures: `.venv/bin/python scripts/simulate_season1_economics.py` → `simulations.json`.

## Synthetic adversarial results and limits

The script runs100 and1000 coordinated accounts, including ten repeated Creator-owner groups. Tap-only and Daily-only networks fail qualification; invitations and passive share yield no acquisition. A Creator network without independent speaker anchors gets0 H/score/allocation. With100 or1000 fully eligible independently anchored speakers shared among ten owners, total H remains respectively700/7000 speaker-days; highest owner score8222/9777 and fresh Creator allocation38,000 for the ten-owner cohort in either case. Additional groups do not multiply global H, and owner participation target prevents the cohort consuming2M.

A fully coordinated network that supplies valid independent gameplay, actual Level5 activations and valid source evidence is not magically detectable as fake humans: all-category100-account upper scenario allocates8,265,000 over six weeks;1000 allocates57,000,000. Those bounds are disclosed, not advertised as Sybil-proof. Review must rely on existing authoritative source patterns/moderation and durable evidence. No invasive identity controls or hidden linking assumptions are added.

## Late beneficiaries and closing

Normal weekly settlement excludes unresolved individual exposures without declaring them validated. Real global failures (unknown impact, missing/stale manifest, incompatible writers/policy, corrupted immutable accounting) still block Freeze. A known pending invitation and unpressed Claim require no economic case and never block weekly Freeze.

Late synchronous already-earned evidence retains source Epoch; if frozen, it produces a funded case. Unfinalized eligible Telegram/Clan source uses the first finalization Epoch when a real unfrozen target exists, preserving original event time and XP semantics. Week6 late work has no Week7: refresh produces a Week6 correction/evidence case. Raw work cases are resolved only by a real recognition, linked beneficiary deferral, explicit rejected source, or permissioned evidenced nonperformance disposition; processed flags alone are insufficient. Window/ingress cases without a provable handoff remain explicitly unresolved for operator review.

During admission, append reviewed assessments and, when new proof changes the result, append a superseding input-digest revision. Missing source proof cannot be closed using a baseline score from other receipts. A conclusively ineligible player may receive an evidence-backed NO_ELIGIBLE_ENTITLEMENT disposition; pending review and missing proof remain unresolved. After the45-day admission window, assess complete corrected category scores from canonical evidence and eligibility review; seal one final category round. Recompute the original bounded category allocation once for the entire corrected cohort. Positive differences create immutable entitlements; negative differences do not rewrite original recipients. This one cohort calculation is necessary because computing each omitted player independently can overstate their combined entitlement. Previously allocated players can also have a justified positive difference from increased supported participation; the same round captures it. Creator H replaces each reviewed player's prior H, never adds it twice. Score<100 is excluded. Original allocation plus correction cannot exceed original cap. This is off-chain accounting, not a new earning rate.

Pay the entire unpaid round atomically only when its exact sum fits the original category reserve. Insufficient funds create durable attempts/cases and block final successful reconciliation; no cross-category transfer, new issuance or silent proportional haircut is permitted. A separately authorized policy decision is needed for an underfunded unresolved case. New claims after the final round remain durable unresolved cases, not extra payments. Current moderation can place paid correction credits on hold. Adjustments are append-only and balances include normal and correction credits in one SQL snapshot.

Final conservation: `60M + authorizedGrowth = normalAllocations + paidCorrections + finalCategoryCarry + unusedReserves`. Holds/forfeitures remain explicit subledgers of issued allocations, not a second spendable reserve. Final Close requires Day45 and all30 category rounds sealed, so it cannot prematurely discard the three-day admission window. No vested XP or Mission entitlement is silently erased; audited Season Close auto-settlement remains as expressly approved, without automatic weekly claims.
