BEGIN;
--
-- Raw SQL operation
--

CREATE FUNCTION b5_announcement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Announcement audit history cannot be deleted';
  END IF;
  IF OLD.state <> 'DRAFT' AND NEW.state = 'DRAFT' THEN
    RAISE EXCEPTION 'Confirmed announcements cannot return to draft';
  END IF;
  IF NEW.cursor < OLD.cursor OR NEW.processed_count < OLD.processed_count THEN
    RAISE EXCEPTION 'Announcement progress is monotonic';
  END IF;
  IF OLD.state <> 'DRAFT' AND ROW(NEW.public_id, NEW.template_id, NEW.audience, NEW.player_id,
      NEW.channel, NEW.title_snapshot, NEW.body_snapshot, NEW.recipient_upper_bound,
      NEW.estimated_recipients, NEW.confirmed_by_id, NEW.confirmed_at, NEW.created_by_id)
    IS DISTINCT FROM ROW(OLD.public_id, OLD.template_id, OLD.audience, OLD.player_id,
      OLD.channel, OLD.title_snapshot, OLD.body_snapshot, OLD.recipient_upper_bound,
      OLD.estimated_recipients, OLD.confirmed_by_id, OLD.confirmed_at, OLD.created_by_id) THEN
    RAISE EXCEPTION 'Confirmed announcement snapshots are immutable';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER b5_announcement_guard BEFORE UPDATE OR DELETE ON notifications_inappannouncement
FOR EACH ROW EXECUTE FUNCTION b5_announcement_guard();

CREATE FUNCTION b5_source_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Broadcast source history cannot be deleted';
  END IF;
  IF OLD.sealed_at IS NOT NULL AND ROW(NEW.operator_id, NEW.chat_id, NEW.media_group_id,
       NEW.messages, NEW.sealed_at) IS DISTINCT FROM ROW(OLD.operator_id, OLD.chat_id,
       OLD.media_group_id, OLD.messages, OLD.sealed_at) THEN
    RAISE EXCEPTION 'Sealed broadcast source is immutable';
  END IF;
  IF OLD.state = 'INVALID' AND NEW.state <> 'INVALID' THEN
    RAISE EXCEPTION 'Invalid sources must be recomposed';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER b5_source_guard BEFORE UPDATE OR DELETE ON notifications_broadcastsource
FOR EACH ROW EXECUTE FUNCTION b5_source_guard();

CREATE FUNCTION b5_template_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.event_key, NEW.channel, NEW.version, NEW.title, NEW.body)
      IS DISTINCT FROM ROW(OLD.event_key, OLD.channel, OLD.version, OLD.title, OLD.body) THEN
    RAISE EXCEPTION 'Notification template versions are immutable';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER b5_template_guard BEFORE UPDATE ON notifications_notificationtemplate
FOR EACH ROW EXECUTE FUNCTION b5_template_guard();

CREATE FUNCTION b5_campaign_source_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (OLD.state <> 'DRAFT' OR NEW.state <> 'DRAFT') AND
     NEW.copy_source_id IS DISTINCT FROM OLD.copy_source_id THEN
    RAISE EXCEPTION 'Confirmed campaign source is immutable';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER b5_campaign_source_guard BEFORE UPDATE ON notifications_broadcastcampaign
FOR EACH ROW EXECUTE FUNCTION b5_campaign_source_guard();

COMMIT;
