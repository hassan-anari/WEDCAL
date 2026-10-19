BEGIN;
--
-- Add field evidence_metadata to playermedalaward
--
ALTER TABLE "profile_collection_playermedalaward" ADD COLUMN "evidence_metadata" jsonb DEFAULT '{}'::jsonb NOT NULL;
ALTER TABLE "profile_collection_playermedalaward" ALTER COLUMN "evidence_metadata" DROP DEFAULT;
COMMIT;
