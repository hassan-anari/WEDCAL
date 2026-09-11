from django.contrib import admin

from apps.clans.models import (
    Clan,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanSeason,
    PlayerSeasonClanState,
)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Phase 7A inspection only; all transitions must use domain services."""

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False


@admin.register(ClanInvite)
class ClanInviteAdmin(ReadOnlyAdmin):
    exclude = ("invite_link",)
    list_display = ("id", "purpose", "state", "attempt_count", "revoke_attempts", "updated_at")


for model in (
    Clan,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanSeason,
    PlayerSeasonClanState,
):
    admin.site.register(model, ReadOnlyAdmin)
