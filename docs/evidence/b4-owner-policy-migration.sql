BEGIN;
--
-- Remove constraint broadcast_exact_target from model broadcastrecipient
--
ALTER TABLE "notifications_broadcastrecipient" DROP CONSTRAINT "broadcast_exact_target";
--
-- Add field audience_policy_version to broadcastcampaign
--
ALTER TABLE "notifications_broadcastcampaign" ADD COLUMN "audience_policy_version" smallint DEFAULT 1 NOT NULL CHECK ("audience_policy_version" >= 0);
--
-- Alter field audience_policy_version on broadcastcampaign
--
-- (no-op)
--
-- Add field group to broadcastrecipient
--
ALTER TABLE "notifications_broadcastrecipient" ADD COLUMN "group_id" bigint NULL CONSTRAINT "notifications_broadc_group_id_e8385ae8_fk_telegram_" REFERENCES "telegram_telegramgroup"("id") DEFERRABLE INITIALLY DEFERRED; SET CONSTRAINTS "notifications_broadc_group_id_e8385ae8_fk_telegram_" IMMEDIATE;
--
-- Alter field audience_type on broadcastcampaign
--
-- (no-op)
--
-- Create constraint broadcast_unique_chat on model broadcastrecipient
--
ALTER TABLE "notifications_broadcastrecipient" ADD CONSTRAINT "broadcast_unique_chat" UNIQUE ("campaign_id", "telegram_user_id");
--
-- Create constraint broadcast_exact_target on model broadcastrecipient
--
ALTER TABLE "notifications_broadcastrecipient" ADD CONSTRAINT "broadcast_exact_target" CHECK ((("group_id" IS NULL AND "group_subscription_id" IS NULL AND "player_id" IS NOT NULL AND "target_kind" = 'USER') OR ("player_id" IS NULL AND "target_kind" = 'GROUP' AND (("group_id" IS NOT NULL AND "group_subscription_id" IS NULL) OR ("group_id" IS NULL AND "group_subscription_id" IS NOT NULL)))));
--
-- Raw SQL operation
--

            CREATE OR REPLACE FUNCTION notification_campaign_definition_guard()
            RETURNS trigger AS $$
            BEGIN
              IF (OLD.state <> 'DRAFT' OR NEW.state <> 'DRAFT') AND (
                NEW.audience_type IS DISTINCT FROM OLD.audience_type OR
                NEW.title_snapshot IS DISTINCT FROM OLD.title_snapshot OR
                NEW.body_snapshot IS DISTINCT FROM OLD.body_snapshot OR
                NEW.purpose IS DISTINCT FROM OLD.purpose
              ) THEN
                RAISE EXCEPTION 'Campaign content and audience are immutable after draft';
              END IF;
              IF OLD.state <> 'DRAFT' AND
                 NEW.audience_policy_version IS DISTINCT FROM OLD.audience_policy_version THEN
                RAISE EXCEPTION 'Active campaign audience policy is immutable';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            
CREATE INDEX "notifications_broadcastrecipient_group_id_e8385ae8" ON "notifications_broadcastrecipient" ("group_id");
COMMIT;
