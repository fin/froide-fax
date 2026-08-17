from django.contrib import admin

from django.utils.translation import gettext_lazy as _

from .models import FaxOverride, Signature


class SignatureAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "timestamp",
    )
    date_hierarchy = "timestamp"
    raw_id_fields = ("user",)
    search_fields = ("user__email",)


admin.site.register(Signature, SignatureAdmin)


class FaxOverrideAdmin(admin.ModelAdmin):
    list_display = ("publicbody", "enabled", "effective_number", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("publicbody__name", "fax_number")
    raw_id_fields = ("publicbody",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description=_("effective number"))
    def effective_number(self, obj):
        return obj.number or "-"


admin.site.register(FaxOverride, FaxOverrideAdmin)
