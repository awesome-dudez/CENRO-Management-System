from django.contrib import admin

from .models import (
    CompletionInfo,
    DesludgingPersonnel,
    InspectionDetail,
    Notification,
    ServiceEquipment,
    ServiceRequest,
)


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    list_display = ["id", "consumer", "service_type", "barangay", "status", "request_date", "created_at"]
    list_filter = ["service_type", "status", "barangay"]
    search_fields = ["consumer__username", "barangay", "address", "client_name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["user", "message", "notification_type", "is_read", "created_at"]
    list_filter = ["is_read", "notification_type", "created_at"]
    search_fields = ["user__username", "message"]


@admin.register(InspectionDetail)
class InspectionDetailAdmin(admin.ModelAdmin):
    list_display = ["service_request", "inspection_date", "inspected_by"]


@admin.register(DesludgingPersonnel)
class DesludgingPersonnelAdmin(admin.ModelAdmin):
    list_display = ["full_name", "role", "is_active", "created_at"]
    list_filter = ["role", "is_active"]


@admin.register(ServiceEquipment)
class ServiceEquipmentAdmin(admin.ModelAdmin):
    list_display = ["unit_number", "notes", "is_active", "created_at"]
    list_filter = ["is_active"]


@admin.register(CompletionInfo)
class CompletionInfoAdmin(admin.ModelAdmin):
    list_display = ["service_request", "date_completed", "driver_name"]
