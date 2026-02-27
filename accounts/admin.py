from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import ConsumerProfile, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "email", "first_name", "last_name", "role", "is_approved", "is_active"]
    list_filter = ["role", "is_approved", "is_active"]
    fieldsets = BaseUserAdmin.fieldsets + (("CENRO Info", {"fields": ("role", "is_approved")}),)


@admin.register(ConsumerProfile)
class ConsumerProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "barangay", "municipality", "province"]
    search_fields = ["user__username", "barangay", "municipality", "province", "street_address"]
