"""Safe In-App copy only. Never interpolate payloads or Telegram credentials."""

from apps.notifications.models import Notification

CLAN_COPY = {
    "CLAN_INVITE_READY": (
        "Clan invitation ready",
        "Open Clan Status in the app to securely access your personal invitation. "
        "Joining Telegram still requires membership verification.",
    ),
    "CLAN_JOIN_ACCEPTED": (
        "Clan request accepted",
        "Your request was accepted. Open Clan Status to check your invitation and next steps.",
    ),
    "CLAN_JOIN_DECLINED": (
        "Clan request declined",
        "Your join request was declined. Open Clan Status to review your options.",
    ),
    "CLAN_MEMBERSHIP_ACTIVATED": (
        "Clan membership active",
        "Your Clan membership is now active. Open Clan Status to view your Clan.",
    ),
    "CLAN_ACCESS_LOST": (
        "Clan Home access lost",
        "Your Clan membership is preserved. Open Clan Status to check secure rejoin options.",
    ),
    "CLAN_ACCESS_RESTORED": (
        "Clan Home access restored",
        "Your Telegram Home access has been verified again. Open Clan Status for current details.",
    ),
    "CLAN_GOVERNANCE_OPENED": (
        "Clan vote opened",
        "A Clan vote is open. Open Clan Status to review the case and voting eligibility.",
    ),
    "CLAN_GOVERNANCE_FINALIZED": (
        "Clan vote concluded",
        "A Clan vote has concluded. Open Clan Status to review its decision and outcome.",
    ),
    "CLAN_INACTIVITY_WARNING": (
        "Clan activity reminder",
        "Your Clan activity needs attention. Open Clan Status to review your membership status.",
    ),
    "CLAN_BOT_ADMIN_LOST": (
        "Clan bot permissions need attention",
        "The bot lost required Home permissions. Open Clan Status to review recovery requirements.",
    ),
    "CLAN_CAPTAIN_AUTHORITY_LOST": (
        "Clan recovery",
        "Restore your Telegram group administrator authority to recover Clan management.",
    ),
    "CLAN_RECOVERY_ENTERED": (
        "Clan entered recovery",
        "Your Clan needs recovery. Open Clan Status to review restrictions and next steps.",
    ),
    "CLAN_RECOVERY_CANCELLED": (
        "Clan recovery resolved",
        "Your Clan returned to active status. Open Clan Status for current details.",
    ),
    "CLAN_BOT_ADMIN_RESTORED": (
        "Clan bot permissions restored",
        "Home bot permissions were restored. Open Clan Status to check any remaining recovery.",
    ),
    "CLAN_CAPTAIN_AUTHORITY_RESTORED": (
        "Captain authority restored",
        "Captain authority was verified again. Open Clan Status to check any remaining recovery.",
    ),
}


def default_clan_copy(event_key: str, source_domain: str) -> tuple[str, str]:
    if source_domain.casefold() not in {"clan", "clans"}:
        return "", ""
    return CLAN_COPY.get(event_key, ("", ""))


def display_copy(notification: Notification) -> tuple[str, str]:
    """Fill blank legacy fields without writing records or consulting mutable templates."""
    title, body = default_clan_copy(notification.event_key, notification.source_domain)
    return (
        notification.title if notification.title.strip() else title,
        notification.body if notification.body.strip() else body,
    )
