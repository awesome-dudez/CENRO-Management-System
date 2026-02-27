from django.contrib import admin

from .models import ChargeCategory, ConfigurableRate, DecloggingApplication, MembershipRecord, ServiceComputation


@admin.register(ConfigurableRate)
class ConfigurableRateAdmin(admin.ModelAdmin):
    list_display = ["key", "value", "description"]
    search_fields = ["key", "description"]


@admin.register(ChargeCategory)
class ChargeCategoryAdmin(admin.ModelAdmin):
    list_display = ["category", "base_rate"]


@admin.register(ServiceComputation)
class ServiceComputationAdmin(admin.ModelAdmin):
    list_display = ["service_request", "total_charge", "payment_status"]
    list_filter = ["payment_status"]


@admin.register(DecloggingApplication)
class DecloggingApplicationAdmin(admin.ModelAdmin):
    list_display = ["service_request", "applicant_name", "is_signed"]


@admin.register(MembershipRecord)
class MembershipRecordAdmin(admin.ModelAdmin):
    list_display = ["user", "is_active", "total_paid"]
