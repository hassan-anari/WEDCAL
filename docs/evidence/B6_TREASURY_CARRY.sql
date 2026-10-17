BEGIN;
--
-- Add field treasury_remainder to claneconomicaccumulator
--
ALTER TABLE "clans_claneconomicaccumulator" ADD COLUMN "treasury_remainder" integer DEFAULT 0 NOT NULL CHECK ("treasury_remainder" >= 0);
ALTER TABLE "clans_claneconomicaccumulator" ALTER COLUMN "treasury_remainder" DROP DEFAULT;
--
-- Create constraint clan_treasury_remainder_range on model claneconomicaccumulator
--
ALTER TABLE "clans_claneconomicaccumulator" ADD CONSTRAINT "clan_treasury_remainder_range" CHECK ("treasury_remainder" < 10000);
COMMIT;
