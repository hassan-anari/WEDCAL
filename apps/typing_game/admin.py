from django.contrib import admin
from django.core.exceptions import ValidationError

from apps.typing_game.models import (
    TypingAttempt,
    TypingClaim,
    TypingPhrase,
    TypingWallet,
    TypingWalletTransaction,
)


@admin.register(TypingPhrase)
class TypingPhraseAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("key", "version", "enabled")

    def save_model(self, request, obj, form, change):  # type: ignore[no-untyped-def]
        if change and TypingAttempt.objects.filter(phrase=obj).exists():
            original = TypingPhrase.objects.get(pk=obj.pk)
            if original.text != obj.text:
                raise ValidationError("Create a new phrase version instead of changing used text")
        super().save_model(request, obj, form, change)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False


admin.site.register(TypingAttempt, ReadOnlyAdmin)
admin.site.register(TypingWallet, ReadOnlyAdmin)
admin.site.register(TypingWalletTransaction, ReadOnlyAdmin)
admin.site.register(TypingClaim, ReadOnlyAdmin)
