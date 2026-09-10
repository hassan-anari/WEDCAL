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

admin.site.register(
    [
        Clan,
        ClanInvite,
        ClanJoinRequest,
        ClanMembership,
        ClanMembershipEvent,
        ClanSeason,
        PlayerSeasonClanState,
    ]
)
