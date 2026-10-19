-- PLAN ONLY. NOT EXECUTED AGAINST PRODUCTION.
-- Requires a separately approved read-only connection and matching deployed schema.
-- Invoke with psql -X --set=ON_ERROR_STOP=1 --set=season_code=S1 --file=...
-- Do not print raw payloads, message bodies, initData, tokens or credentials.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL TIME ZONE 'UTC';
SELECT current_timestamp AS observed_at, current_user AS inspection_role,
       current_setting('transaction_read_only') AS read_only,
       txid_current_snapshot() AS snapshot, pg_is_in_recovery() AS replica;
SELECT id AS inspected_season_id FROM seasons_season WHERE code=:'season_code' \gset

-- A. Schema and immutable-policy inventory. Unknown/missing schema aborts; no repair.
SELECT app,name,applied FROM django_migrations
WHERE app IN ('arena','seasons','telegram','network','clans','base_tap','typing_game','missions')
ORDER BY app,name;
SELECT s.code,s.status,s.starts_at,s.ends_at,s.baseline_config_id,
       s.settlement_start_epoch,s.settlement_started_at,
       e.epoch_number,e.state,e.starts_at,e.ends_at,e.config_version_id,
       e.base_budget,e.growth_budget,e.frozen_at,e.snapshots_completed_at,
       e.settlement_started_at,e.settlement_completed_at
FROM seasons_season s JOIN arena_economicepoch e ON e.season_id=s.id
WHERE s.id=:inspected_season_id ORDER BY e.epoch_number;
SELECT c.id,c.version,c.status,c.effective_at,c.published_at,c.payload_checksum,
       c.payload->'retention' AS retention,
       c.payload->'dlx_distribution' AS distribution
FROM game_config_gameconfigversion c WHERE c.status='PUBLISHED'
ORDER BY c.effective_at;
SELECT t.tgname,c.relname,pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
WHERE NOT t.tgisinternal AND (c.relname LIKE 'arena_%' OR c.relname LIKE 'seasons_%'
 OR c.relname LIKE 'clans_clanmission%' OR c.relname='rewards_rewardledger')
ORDER BY c.relname,t.tgname;

-- B. Exact per-Epoch source census. Counts are diagnostics, not completeness.
WITH sources AS (
 SELECT season_id,source_time,player_id,'FACT:'||source_type AS source FROM arena_performancesourcefact
 UNION ALL SELECT season_id,occurred_at,player_id,'XP:'||source_type FROM rewards_rewardledger
 UNION ALL SELECT season_id,observed_at,sender_id,'NETWORK:'||classification FROM network_networkactivity
 UNION ALL SELECT season_id,observed_at,sender_id,'CLAN:'||classification FROM clans_clanactivity
 UNION ALL SELECT season_id,submitted_at,player_id,'TYPING:'||status FROM typing_game_typingattempt
 UNION ALL SELECT season_id,verified_at,player_id,'MISSION:'||status FROM missions_playermission
 UNION ALL SELECT season_id,claimed_at,player_id,'DAILY' FROM daily_dailyclaim
)
SELECT e.epoch_number,s.source,count(*) AS rows,count(DISTINCT s.player_id) AS players,
       min(s.source_time),max(s.source_time)
FROM arena_economicepoch e JOIN sources s ON s.season_id=e.season_id
 AND s.source_time>=e.starts_at AND s.source_time<e.ends_at
WHERE e.season_id=:inspected_season_id GROUP BY e.epoch_number,s.source
ORDER BY e.epoch_number,s.source;

-- C. Retention urgency, including globally unresolved ingress.
SELECT state,count(*) AS rows,count(*) FILTER (WHERE payload='{}'::jsonb) AS purged,
       count(*) FILTER (WHERE webhook_authenticated_at IS NULL) AS missing_auth_marker,
       min(received_at) AS oldest,min(payload_expires_at) FILTER(WHERE payload<>'{}'::jsonb) AS next_expiry,
       count(*) FILTER(WHERE payload<>'{}'::jsonb AND payload_expires_at<=now()+interval '48 hours') AS expires_48h
FROM telegram_telegraminboundupdate
WHERE received_at >= (SELECT starts_at FROM seasons_season WHERE id=:inspected_season_id)
 OR state NOT IN ('PROCESSED','IGNORED') GROUP BY state ORDER BY state;
SELECT e.epoch_number,n.classification,count(*) AS rows,
       count(*) FILTER(WHERE n.sender_id IS NULL) AS missing_sender,
       count(*) FILTER(WHERE n.text IS NULL) AS content_purged,
       count(*) FILTER(WHERE n.exact_fingerprint='' OR n.normalized_fingerprint='') AS fingerprint_missing,
       min(n.content_expires_at) FILTER(WHERE n.text IS NOT NULL) AS next_content_expiry,
       min(n.fingerprint_expires_at) FILTER(WHERE n.normalized_fingerprint<>'') AS next_hash_expiry,
       count(*) FILTER(WHERE i.id IS NULL OR i.webhook_authenticated_at IS NULL) AS missing_ingress_auth
FROM arena_economicepoch e JOIN network_networkactivity n ON n.season_id=e.season_id
 AND n.observed_at>=e.starts_at AND n.observed_at<e.ends_at
LEFT JOIN telegram_telegraminboundupdate i ON i.update_id=n.telegram_update_id
WHERE e.season_id=:inspected_season_id GROUP BY e.epoch_number,n.classification
ORDER BY e.epoch_number,n.classification;
SELECT state,count(*),min(received_at) FROM telegram_telegraminboundupdate i
WHERE i.payload='{}'::jsonb
 AND i.received_at >= (SELECT starts_at FROM seasons_season WHERE id=:inspected_season_id)
 AND NOT EXISTS(SELECT 1 FROM network_networkactivity a WHERE a.telegram_update_id=i.update_id)
 AND NOT EXISTS(SELECT 1 FROM clans_clanactivity a WHERE a.telegram_update_id=i.update_id)
GROUP BY state;

-- D. Permanent Tap/Typing evidence and exactly-once joins.
SELECT f.source_type,count(*) AS facts,
       count(*) FILTER(WHERE NOT(f.metadata ? 'accepted_taps')) AS missing_accepted_count,
       count(*) FILTER(WHERE f.source_type='BASE_TAP_V2_BATCH' AND r.id IS NULL) AS missing_ledger
FROM arena_performancesourcefact f LEFT JOIN rewards_rewardledger r
 ON f.source_type='BASE_TAP_V2_BATCH' AND r.id::text=f.source_id
WHERE f.season_id=:inspected_season_id AND f.source_type IN ('BASE_TAP_BATCH','BASE_TAP_V2_BATCH')
GROUP BY f.source_type;
SELECT settlement_state,count(*),sum(pending_points),min(first_accepted_at),
       count(*) FILTER(WHERE first_accepted_at IS NULL) AS unknown_source_time
FROM base_tap_basetapdailyaccumulator WHERE season_id=:inspected_season_id
GROUP BY settlement_state;
SELECT a.status,count(*),count(*) FILTER(WHERE a.status='VALID' AND
 (a.submitted_at IS NULL OR a.wpm IS NULL OR a.accuracy_bps IS NULL OR c.id IS NULL OR f.id IS NULL)) AS valid_missing_evidence,
 count(*) FILTER(WHERE c.id IS NOT NULL AND (c.amount<>a.reward_amount OR c.config_version_id<>a.config_version_id
 OR c.season_id<>a.season_id)) AS credit_mismatch
FROM typing_game_typingattempt a LEFT JOIN typing_game_typingwallettransaction c
 ON c.attempt_id=a.id AND c.kind='CREDIT'
LEFT JOIN arena_performancesourcefact f ON f.source_type='TYPING_CREDIT' AND f.source_id=a.public_id::text
WHERE a.season_id=:inspected_season_id GROUP BY a.status;

-- E. Vested Missions, Daily reset boundaries and unresolved outboxes.
SELECT status,count(*),count(*) FILTER(WHERE verified_at IS NULL OR config_version_id IS NULL
 OR reward_amount IS NULL) AS missing_entitlement_evidence,
 count(*) FILTER(WHERE status='CLAIMED' AND reward_ledger_id IS NULL) AS claimed_missing_ledger
FROM missions_playermission WHERE season_id=:inspected_season_id GROUP BY status;
SELECT e.epoch_number,max(d.claim_dates) AS maximum_daily_periods
FROM arena_economicepoch e LEFT JOIN LATERAL (
 SELECT player_id,count(DISTINCT claim_period) AS claim_dates FROM daily_dailyclaim d
 WHERE d.season_id=e.season_id AND d.claimed_at>=e.starts_at AND d.claimed_at<e.ends_at GROUP BY player_id
) d ON true WHERE e.season_id=:inspected_season_id GROUP BY e.epoch_number ORDER BY e.epoch_number;
SELECT o.state,count(*),min(r.occurred_at),max(o.attempts)
FROM rewards_rewardsettlementoutbox o JOIN rewards_rewardledger r ON r.id=o.reward_id
WHERE r.season_id=:inspected_season_id GROUP BY o.state;

-- F. Clan contribution/mission provenance and boundary risks.
SELECT s.source_type,count(*),sum(s.gross_amount),sum(s.contribution_amount),
 count(*) FILTER(WHERE s.gross_amount<>s.player_amount+s.treasury_amount) AS split_mismatch,
 count(*) FILTER(WHERE c.id IS NULL OR c.gross_basis<>s.gross_amount) AS contribution_link_mismatch
FROM clans_claneconomicsettlement s JOIN clans_clanseason cs ON cs.id=s.clan_season_id
LEFT JOIN clans_clancontributionentry c ON c.settlement_id=s.id
WHERE cs.season_id=:inspected_season_id GROUP BY s.source_type;
SELECT e.epoch_number,count(*) AS straddling_or_unknown_windows,
 count(*) FILTER(WHERE w.window_end IS NULL) AS unknown_end
FROM arena_economicepoch e JOIN clans_clanactivitywindow w ON w.season_id=e.season_id
 AND w.window_start<e.ends_at AND (w.window_end>e.ends_at OR w.window_end IS NULL)
WHERE e.season_id=:inspected_season_id GROUP BY e.epoch_number ORDER BY e.epoch_number;
SELECT d.requirement,count(*),count(*) FILTER(WHERE s.activation_id IS NULL) AS missing_activation,
 count(*) FILTER(WHERE s.source_id NOT LIKE 'clan-activity:%' AND s.source_id NOT LIKE 'contribution:%'
 AND s.source_id NOT LIKE 'typing-run:%') AS unknown_root_identity
FROM clans_clanmissionsourceevent s JOIN clans_clanmissiondefinition d ON d.id=s.definition_id
JOIN clans_clanseason cs ON cs.id=s.clan_season_id WHERE cs.season_id=:inspected_season_id GROUP BY d.requirement;
SELECT s.requires_review,count(*),max(s.attempts),min(s.occurred_at)
FROM clans_clanmissionsignal s JOIN clans_clanmembership m ON m.id=s.membership_id
WHERE m.season_id=:inspected_season_id AND s.processed_at IS NULL GROUP BY s.requires_review;

-- G. Historical eligibility and confirmed-accounting census. No PII/content export.
SELECT policy_key,policy_version,decision,count(*),min(effective_at),max(effective_at),max(created_at)
FROM arena_qualificationdecision GROUP BY policy_key,policy_version,decision;
SELECT r.status,count(*),count(*) FILTER(WHERE r.status='VERIFIED'
 AND (r.verified_at IS NULL OR r.verified_config_id IS NULL)) AS missing_verification
FROM referrals_referralrelation r WHERE r.attributed_at <
 (SELECT ends_at FROM seasons_season WHERE id=:inspected_season_id) GROUP BY r.status;
SELECT e.epoch_number,count(a.id),coalesce(sum(a.amount),0) AS confirmed
FROM arena_economicepoch e LEFT JOIN arena_dlxallocation a ON a.epoch_id=e.id
WHERE e.season_id=:inspected_season_id GROUP BY e.epoch_number ORDER BY e.epoch_number;
SELECT b.category,sum(b.allocated_amount),sum(b.carry_in),sum(b.carry_out),sum(b.residual_amount)
FROM arena_epochcategorybudget b JOIN arena_economicepoch e ON e.id=b.epoch_id
WHERE e.season_id=:inspected_season_id GROUP BY b.category;
SELECT count(*) AS open_anomalies FROM arena_epochanomaly a JOIN arena_economicepoch e ON e.id=a.epoch_id
LEFT JOIN arena_epochanomalyresolution r ON r.anomaly_id=a.id
WHERE e.season_id=:inspected_season_id AND r.id IS NULL;
-- H. Historical owner/authorization and activated Mission provenance.
-- to_jsonb handles owner columns absent before Gate B without inventing a backfill.
SELECT count(*) AS registration_events,
 count(*) FILTER(WHERE to_jsonb(h)->>'owner_id' IS NULL) AS historical_owner_missing,
 count(*) FILTER(WHERE h.reward_eligible) AS eligible_states
FROM network_creatorregistrationstateevent h
JOIN network_creatorgroupregistration r ON r.id=h.registration_id
WHERE h.occurred_at < (SELECT ends_at FROM seasons_season WHERE id=:inspected_season_id);
SELECT w.state,count(*) AS windows,
 count(*) FILTER(WHERE to_jsonb(w)->>'owner_id' IS NULL) AS owner_missing,
 count(*) FILTER(WHERE w.finalized_at IS NULL) AS finalization_missing,
 count(*) FILTER(WHERE w.state='REWARDED' AND w.reward_id IS NULL) AS reward_missing
FROM network_creatorwindowsettlement w WHERE w.season_id=:inspected_season_id GROUP BY w.state;
SELECT d.key,d.version,d.enabled,d.activated_at,d.verification_family,d.repeat_policy,
 count(p.id) AS verified_rows,
 count(*) FILTER(WHERE p.verified_at<d.activated_at) AS verification_before_activation
FROM missions_missiondefinition d LEFT JOIN missions_playermission p
 ON p.definition_id=d.id AND p.season_id=:inspected_season_id AND p.verified_at IS NOT NULL
GROUP BY d.id ORDER BY d.key,d.version;
SELECT count(*) AS observed_players,
 count(*) FILTER(WHERE p.first_mini_app_at IS NULL AND
 to_jsonb(p)->>'first_authenticated_at' IS NULL) AS no_direct_trusted_age_marker
FROM players_player p WHERE p.created_at <
 (SELECT ends_at FROM seasons_season WHERE id=:inspected_season_id);
-- Corroborate missing direct age markers through authenticated normalized source links;
-- never reinterpret first_seen_at as proof of authentication.
COMMIT;
