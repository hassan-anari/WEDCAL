from django.contrib import admin
from django.http import HttpRequest

from apps.base_tap.models import BaseTapDailyAccumulator, BaseTapReceipt


class ReadOnlyBaseTapAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


admin.site.register(BaseTapDailyAccumulator, ReadOnlyBaseTapAdmin)
admin.site.register(BaseTapReceipt, ReadOnlyBaseTapAdmin)
