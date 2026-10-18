"""In-App lifecycle notifications derived from committed Clan source identities."""

from django.db import transaction

from apps.notifications.services import emit_notification_safely
from apps.players.models import Player


def notify_clan_event(
    *, recipient: Player, event_key: str, source_type: str, source_id: int
) -> None:
    # No payload credentials and no new Telegram delivery policy.
    transaction.on_commit(
        lambda: emit_notification_safely(
            recipient=recipient,
            event_key=event_key,
            source_domain="CLAN",
            source_type=source_type,
            source_id=str(source_id),
            idempotency_key=f"clan-lifecycle:{event_key}:{source_id}:{recipient.pk}",
        )
    )
