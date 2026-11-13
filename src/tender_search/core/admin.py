from django.contrib import admin
from . import models

admin.site.site_header = "TenderSearch — администрирование"
admin.site.site_title = "TenderSearch"
admin.site.index_title = "Администрирование"

@admin.register(models.Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "status", "deadline", "checked_at")
    search_fields = ("number", "name")

@admin.register(models.Lot)
class LotAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "amount", "status")
    search_fields = ("number", "name")

for model in (models.Customer, models.Document, models.DocumentVersion, models.SearchProfile,
              models.SearchRun, models.Match, models.Favorite, models.Notification):
    admin.site.register(model)


@admin.register(models.TelegramSubscription)
class TelegramSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("owner", "enabled", "updated_at")
    exclude = ("link_hash", "link_expires_at")

@admin.register(models.TelegramDelivery)
class TelegramDeliveryAdmin(admin.ModelAdmin):
    list_display = ("owner", "profile", "state", "created_at")
    list_filter = ("state",)
    readonly_fields = ("owner", "profile", "lot", "announcement_key", "state", "error", "message_id", "retry_at")
    def has_add_permission(self, request): return False
