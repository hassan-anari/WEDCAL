BEGIN;
--
-- Add field captain_authority_last_attempt_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "captain_authority_last_attempt_at" timestamp with time zone NULL;
--
-- Add field bot_admin_observed_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "bot_admin_observed_at" timestamp with time zone NULL;
--
-- Add field bot_admin_recovery_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "bot_admin_recovery_at" timestamp with time zone NULL;
--
-- Add field captain_authority_lost_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "captain_authority_lost_at" timestamp with time zone NULL;
--
-- Add field captain_authority_observed_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "captain_authority_observed_at" timestamp with time zone NULL;
--
-- Add field captain_inactivity_recovery_at to clanseason
--
ALTER TABLE "clans_clanseason" ADD COLUMN "captain_inactivity_recovery_at" timestamp with time zone NULL;
CREATE INDEX "clans_clanseason_captain_authority_last_attempt_at_24101ea5" ON "clans_clanseason" ("captain_authority_last_attempt_at");
CREATE INDEX "clans_clanseason_captain_authority_lost_at_4e9a80e4" ON "clans_clanseason" ("captain_authority_lost_at");
COMMIT;
