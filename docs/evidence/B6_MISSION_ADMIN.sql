BEGIN;
--
-- Change Meta options on clanmissiondefinition
--
-- (no-op)
--
-- Add field activated_at to clanmissiondefinition
--
ALTER TABLE "clans_clanmissiondefinition" ADD COLUMN "activated_at" timestamp with time zone NULL;
--
-- Add field description to clanmissiondefinition
--
ALTER TABLE "clans_clanmissiondefinition" ADD COLUMN "description" varchar(500) DEFAULT '' NOT NULL;
ALTER TABLE "clans_clanmissiondefinition" ALTER COLUMN "description" DROP DEFAULT;
--
-- Add field title to clanmissiondefinition
--
ALTER TABLE "clans_clanmissiondefinition" ADD COLUMN "title" varchar(200) DEFAULT '' NOT NULL;
ALTER TABLE "clans_clanmissiondefinition" ALTER COLUMN "title" DROP DEFAULT;
--
-- Alter field key on clanmissiondefinition
--
CREATE INDEX "clans_clanmissiondefinition_key_bf93ba07" ON "clans_clanmissiondefinition" ("key");
CREATE INDEX "clans_clanmissiondefinition_key_bf93ba07_like" ON "clans_clanmissiondefinition" ("key" varchar_pattern_ops);
--
-- Raw SQL operation
--

UPDATE clans_clanmissiondefinition SET activated_at = CURRENT_TIMESTAMP
WHERE enabled AND activated_at IS NULL;
CREATE FUNCTION clans_b6_mission_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.key, OLD.version) IS DISTINCT FROM (NEW.key, NEW.version) THEN
            RAISE EXCEPTION 'Mission version identity is immutable';
        END IF;
        IF OLD.activated_at IS NOT NULL AND OLD.activated_at IS DISTINCT FROM NEW.activated_at THEN
            RAISE EXCEPTION 'Mission activation history is immutable';
        END IF;
        IF (OLD.enabled OR OLD.activated_at IS NOT NULL OR EXISTS (
            SELECT 1 FROM clans_clanmissionprogress WHERE definition_id = OLD.id
        )) AND ROW(OLD.kind, OLD.requirement, OLD.target_units,
            OLD.reward_band, OLD.starts_at, OLD.ends_at,
            OLD.config_version_id) IS DISTINCT FROM
        ROW(NEW.kind, NEW.requirement, NEW.target_units,
            NEW.reward_band, NEW.starts_at, NEW.ends_at,
            NEW.config_version_id) THEN
            RAISE EXCEPTION 'Create a new mission version for economic changes';
        END IF;
    END IF;
    IF NEW.enabled AND NEW.activated_at IS NULL THEN
        NEW.activated_at := CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER b6_mission_definition_guard BEFORE INSERT OR UPDATE ON clans_clanmissiondefinition
FOR EACH ROW EXECUTE FUNCTION clans_b6_mission_guard();

COMMIT;
