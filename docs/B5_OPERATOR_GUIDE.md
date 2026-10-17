# B5 operator guide

## Django Admin: In-App announcements

1. Create or select an enabled **IN_APP NotificationTemplate** version. Template editing alone publishes nothing. Titles/bodies are displayed as text by the existing Notification Center, not Telegram HTML.
2. Create **In-App announcement**, choose ALL existing Players or ONE selected Player, and save the draft. Draft authors need the usual announcement add/change/view permissions and template/Player lookup access.
3. Open **Preview / confirm / cancel**. Review exact title, body, channel, audience and estimated recipient count. ALL is bounded by the existing Player-ID upper fence shown in the preview; later Players are excluded.
4. An operator with `notifications.publish_inappannouncement` enters a reason, explicitly checks confirmation and publishes the signed preview. A changed draft requires a new preview. The Admin request creates no recipient notifications.
5. Celery Beat processes bounded transactional batches. Admin shows Pending, Processing, Completed, Failed or Cancelled, estimated/processed counts, attempts and a safe error category. A failed batch rolls back notifications and cursor; retries resume safely. A failed announcement can be explicitly retried from its durable cursor with publish permission.
6. `notifications.cancel_inappannouncement` stops unprocessed work. Already committed notifications remain. Completion and cancellation serialize with each bounded batch; concurrent workers cannot duplicate recipients.

Confirmed snapshots are immutable at PostgreSQL level. A selected template's later enabled state or a newer version does not change the confirmed content. One stable notification identity exists per announcement/Player. Read/unread and per-Player isolation use the existing Notification Center. **No Telegram messages are sent by this workflow.**

## Private bot `/admin`: native multimedia Broadcast

Use the existing allowlisted, mapped, enabled BotAdminOperator with `notifications.manage_broadcasts`. Open `/admin` privately, choose Broadcast, and send ONE original copyable message or ONE album. Forwarded/operator-mismatched/unauthenticated input is rejected. Protected and Telegram-uncopyable service/payment/giveaway content is unsupported; a quiz without a known correct answer cannot be copied.

Text/entities, photos/captions, video, voice, audio, documents, animations, video notes, stickers, contacts, location and applicable other native-copyable content use `copyMessage`. Photo/video, document or audio albums use `copyMessages` with sorted original message IDs. No media re-upload or attribution forwarding occurs.

For an album, wait for every item and send `/done N` with the exact number originally sent (2–10). Telegram supplies no end-of-album marker/count, so the explicit count is required in addition to a bounded quiet interval. Pending persisted parts block sealing. Capture expires after the configured bound; recompose an incomplete/expired album. A newly arriving part after sealing invalidates the source and stops remaining work; already in-flight copies cannot be recalled.

Choose Users, Groups or Both. Started private contacts and known currently authorized bot-admin groups are eligible; there is no opt-in or Creator registration requirement. Review the **actual native copy preview**, item count and estimated audiences. Confirmation is blocked until the complete preview succeeds. A failed/partial preview requires resolving or recomposing; a description alone cannot authorize a campaign.

Explicit confirmation queues the existing campaign. Progress/Refresh/Cancel remain available. Group permissions are rechecked at dispatch and private eligibility is rechecked; blocked/not-found recipients are skipped. Frozen source message deletion, protection or Telegram limitations fail explicitly. After the first complete preview, keep the bot-authored preview source messages while the campaign runs. Originals may then be edited or deleted without changing the frozen campaign.

429/local admission refusals use bounded durable Retry-Wait without consuming send attempts. Copy methods share bot-wide send, method and per-chat budgets; album admission accounts for item count. Timeout, lease expiry, malformed success or partial/skipped album copies become **UNCERTAIN** and are not blindly retried. Recorded destination IDs/counts support audit without logging file IDs/private content. Cancelling stops unprocessed work and does not retract sent messages. Do not manually reset UNCERTAIN to Pending: inspect actual delivery before any owner-approved resolution.

## Development and rollout

Use synthetic transports and isolated databases for all tests. No live sends, broadcasts or deployment are authorized by this PR. New migration order: Clan mission signal table; notification source/announcement tables and additive fields; PostgreSQL snapshot guards. Apply additive migrations before new workers. Keep current disabled/controlled public flags and credentials separate; no published config/history rewrite or historical campaign obligation is assumed.

On rollback, stop new announcements/native campaigns and their workers, cancel/drain pending work with the new code, retain source snapshots, notifications, ledgers and uncertainty audit. Older workers do not understand native-copy campaigns: they must not consume them. Keep new code installed for existing obligations; do not downgrade the schema or reinterpret native sources as text.

Operational configuration: `ANNOUNCEMENT_BATCH_SIZE` (default 100, runtime clamp 1–500), `ANNOUNCEMENT_MAX_ATTEMPTS` (5), `BROADCAST_ALBUM_QUIET_SECONDS` (2), `BROADCAST_ALBUM_CAPTURE_SECONDS` (120), `CLAN_MISSION_SIGNAL_MAX_ATTEMPTS` (5), `CLAN_AUTHORITY_RECHECK_SECONDS` (60 minimum retry spacing; existing worker cadence is 300 seconds). Existing Telegram budgets/session/queue-age settings remain authoritative. Beat and subscribed workers are required; scanners recover broker/worker interruption from PostgreSQL.

Native campaign source is frozen by an atomic first-successful-preview reference. Bot-authored preview message IDs become the source for all subsequent previews and deliveries, so editing/deleting an original operator message cannot change confirmed content. Deleting the bot-authored source can make copying unavailable and is handled explicitly. Concurrent preview completions cannot replace the frozen source.
