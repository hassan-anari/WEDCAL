"""Additive workflow provenance guards; no live policy, authority or alert destination is seeded."""

from django.db import migrations

FORWARD = r"""
CREATE TRIGGER arena_policy_proposal_immutable BEFORE UPDATE OR DELETE
ON arena_economicpolicyproposal FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE TRIGGER arena_policy_approval_immutable BEFORE UPDATE OR DELETE
ON arena_economicpolicyapproval FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE TRIGGER arena_alert_event_immutable BEFORE UPDATE OR DELETE
ON arena_economicalertevent FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE FUNCTION arena_validate_policy_approval() RETURNS trigger AS $$
DECLARE proposal arena_economicpolicyproposal; policy arena_economicpolicyversion;
BEGIN
 SELECT * INTO proposal FROM arena_economicpolicyproposal WHERE id=NEW.proposal_id FOR UPDATE;
 SELECT * INTO policy FROM arena_economicpolicyversion WHERE id=NEW.policy_id;
 IF NEW.approved_by_id=proposal.submitted_by_id OR
 (proposal.key,proposal.version,proposal.payload,proposal.checksum) IS DISTINCT FROM
 (policy.key,policy.version,policy.payload,policy.checksum) OR
 NEW.approval_reference IS DISTINCT FROM policy.approval_reference
 THEN RAISE EXCEPTION 'Policy approval requires independent actor and exact proposed document';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_validate_policy_approval BEFORE INSERT ON arena_economicpolicyapproval
 FOR EACH ROW EXECUTE FUNCTION arena_validate_policy_approval();
CREATE FUNCTION arena_alert_delivery_pin() RETURNS trigger AS $$
BEGIN
 IF (OLD.event_id,OLD.route_key,OLD.route_config) IS DISTINCT FROM
 (NEW.event_id,NEW.route_key,NEW.route_config)
 THEN RAISE EXCEPTION 'Alert delivery routing and provenance are immutable'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_alert_delivery_pin BEFORE UPDATE ON arena_economicalertdelivery
 FOR EACH ROW EXECUTE FUNCTION arena_alert_delivery_pin();
"""
REVERSE = r"""
DROP TRIGGER arena_policy_proposal_immutable ON arena_economicpolicyproposal;
DROP TRIGGER arena_policy_approval_immutable ON arena_economicpolicyapproval;
DROP TRIGGER arena_alert_event_immutable ON arena_economicalertevent;
DROP TRIGGER arena_validate_policy_approval ON arena_economicpolicyapproval;
DROP FUNCTION arena_validate_policy_approval();
DROP TRIGGER arena_alert_delivery_pin ON arena_economicalertdelivery;
DROP FUNCTION arena_alert_delivery_pin();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0028_policy_registration_operational_alerts")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
