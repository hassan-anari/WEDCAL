from django.contrib import admin

from apps.network.models import (
    CreatorDailyAllowance,
    CreatorGroupRegistration,
    CreatorRegistrationIntent,
    CreatorWindowSettlement,
    NetworkActivity,
)

admin.site.register(
    (
        CreatorDailyAllowance,
        CreatorGroupRegistration,
        CreatorRegistrationIntent,
        CreatorWindowSettlement,
        NetworkActivity,
    )
)
