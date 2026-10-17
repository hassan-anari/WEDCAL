BEGIN;
--
-- Change Meta options on missiondefinition
--
-- (no-op)
--
-- Add field activated_at to missiondefinition
--
ALTER TABLE "missions_missiondefinition" ADD COLUMN "activated_at" timestamp with time zone NULL;
--
-- Raw SQL operation
--

UPDATE missions_missiondefinition SET activated_at = CURRENT_TIMESTAMP
WHERE enabled AND activated_at IS NULL;
CREATE FUNCTION missions_b6_mission_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.key, OLD.version) IS DISTINCT FROM (NEW.key, NEW.version) THEN
            RAISE EXCEPTION 'Mission version identity is immutable';
        END IF;
        IF OLD.activated_at IS NOT NULL AND OLD.activated_at IS DISTINCT FROM NEW.activated_at THEN
            RAISE EXCEPTION 'Mission activation history is immutable';
        END IF;
        IF (OLD.enabled OR OLD.activated_at IS NOT NULL OR EXISTS (
            SELECT 1 FROM missions_playermission WHERE definition_id = OLD.id
        )) AND ROW(OLD.verification_family, OLD.repeat_policy, OLD.verification_parameters,
            OLD.available_from, OLD.available_until) IS DISTINCT FROM
        ROW(NEW.verification_family, NEW.repeat_policy, NEW.verification_parameters,
            NEW.available_from, NEW.available_until) THEN
            RAISE EXCEPTION 'Create a new mission version for economic changes';
        END IF;
    END IF;
    IF NEW.enabled AND NEW.activated_at IS NULL THEN
        NEW.activated_at := CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER b6_mission_definition_guard BEFORE INSERT OR UPDATE ON missions_missiondefinition
FOR EACH ROW EXECUTE FUNCTION missions_b6_mission_guard();

COMMIT;
