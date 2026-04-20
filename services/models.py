from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL

# Admin / audit: public-property requests within Bayawan skip inspection fee and get ₱0 computation (see ServiceComputation.calculate_charges).
PUBLIC_BAYAWAN_NO_FEES_FLAG = "[PUBLIC_BAYAWAN_NO_FEES]"


class ServiceRequest(models.Model):
    class ServiceType(models.TextChoices):
        RESIDENTIAL_DESLUDGING = "RESIDENTIAL_DESLUDGING", "Residential Septage Desludging"
        COMMERCIAL_DESLUDGING = "COMMERCIAL_DESLUDGING", "Commercial Septage Desludging"
        GRASS_CUTTING = "GRASS_CUTTING", "Grass Cutting"

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        INSPECTION_FEE_DUE = "INSPECTION_FEE_DUE", "Inspection Fee Due"
        INSPECTION_FEE_AWAITING_VERIFICATION = "INSPECTION_FEE_AWAITING_VERIFICATION", "Inspection Fee Awaiting Verification"
        EXPIRED = "EXPIRED", "Expired"
        UNDER_REVIEW = "UNDER_REVIEW", "Under Review"
        INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED", "Inspection Scheduled"
        INSPECTED = "INSPECTED", "Inspected"
        COMPUTATION_SENT = "COMPUTATION_SENT", "Computation Sent"
        AWAITING_PAYMENT = "AWAITING_PAYMENT", "Awaiting Payment Verification"
        PAID = "PAID", "Paid"
        DESLUDGING_SCHEDULED = "DESLUDGING_SCHEDULED", "Desludging Scheduled"
        COMPLETED = "COMPLETED", "Completed"
        # Grass cutting: pay at Treasurer, upload receipt, then admin confirms or cancels
        GRASS_PENDING_PAYMENT = "GRASS_PENDING_PAYMENT", "Pending Payment"
        GRASS_PAYMENT_AWAITING_VERIFICATION = (
            "GRASS_PAYMENT_AWAITING_VERIFICATION",
            "Payment Receipt Awaiting Verification",
        )
        CANCELLED = "CANCELLED", "Cancelled"

    class PublicPrivate(models.TextChoices):
        PUBLIC = "PUBLIC", "Public"
        PRIVATE = "PRIVATE", "Private"

    consumer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="service_requests")
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests_made",
        help_text="Account that submitted the request when requesting for another person.",
    )
    client_name = models.CharField(max_length=255, help_text="Client / Establishment Name")
    request_date = models.DateField(default=timezone.now)
    contact_number = models.CharField(max_length=20)

    class LocationMode(models.TextChoices):
        PIN = "PIN", "Pin on Map"
        TEXT = "TEXT", "Type Address"

    location_mode = models.CharField(
        max_length=10,
        choices=LocationMode.choices,
        default=LocationMode.PIN,
    )
    barangay = models.CharField(max_length=255)
    address = models.CharField(max_length=500)
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    service_type = models.CharField(max_length=30, choices=ServiceType.choices)
    connected_to_bawad = models.BooleanField(default=False)
    bawad_proof = models.FileField(upload_to="bawad_proofs/", null=True, blank=True)
    public_private = models.CharField(
        max_length=10,
        choices=PublicPrivate.choices,
        default=PublicPrivate.PRIVATE,
    )
    client_signature = models.FileField(upload_to="client_signatures/", null=True, blank=True)

    location_photo_1 = models.ImageField(upload_to="location_photos/", null=True, blank=True)
    location_photo_2 = models.ImageField(upload_to="location_photos/", null=True, blank=True)

    notes = models.TextField(blank=True)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.SUBMITTED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    assigned_inspector = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_assigned",
        limit_choices_to={"role__in": ["ADMIN", "STAFF"]},
    )
    inspection_date = models.DateField(null=True, blank=True)
    scheduled_desludging_date = models.DateField(null=True, blank=True)

    fee_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    fee_notes = models.CharField(max_length=255, blank=True)

    # Inspection fee tracking (for first-time desludging customers)
    inspection_fee_receipt = models.FileField(
        upload_to="inspection_fee_receipts/", null=True, blank=True
    )
    inspection_fee_paid = models.BooleanField(default=False)

    # Grass Cutting application fields (used only when service_type is GRASS_CUTTING)
    grasscutting_date = models.DateField(null=True, blank=True)
    grasscutting_personnel = models.PositiveIntegerField(null=True, blank=True)
    grasscutting_hours = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    treasurer_receipt = models.FileField(upload_to="treasurer_receipts/", null=True, blank=True)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    cubic_meters = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, default=0)

    # When inspection is waived: driver/helpers before computation (personnel count).
    waived_crew_driver_name = models.CharField(max_length=255, blank=True)
    waived_crew_helper1_name = models.CharField(max_length=255, blank=True)
    waived_crew_helper2_name = models.CharField(max_length=255, blank=True)
    waived_crew_helper3_name = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"{self.get_service_type_display()} - {self.consumer} ({self.barangay})"

    @classmethod
    def expire_stale_requests(cls) -> dict:
        """
        Two-phase cleanup for requests with no progress:
        1. Day 4+: send a warning notification (once) that the request will
           expire in 3 days if no action is taken.
        2. Day 7+: set status to EXPIRED and notify the consumer.

        "No progress" = ``updated_at`` hasn't changed (no save on the record).
        Only affects requests still in an initial/waiting status.
        Returns ``{"warned": int, "expired": int}``.
        """
        now = timezone.now()
        warning_cutoff = now - timedelta(days=4)
        expiry_cutoff = now - timedelta(days=7)

        stale_statuses = [
            cls.Status.SUBMITTED,
            cls.Status.INSPECTION_FEE_DUE,
            cls.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
        ]

        warned_count = 0
        to_warn = (
            cls.objects.filter(
                status__in=stale_statuses,
                updated_at__lt=warning_cutoff,
                updated_at__gte=expiry_cutoff,
            )
            .select_related("consumer")
        )
        for sr in to_warn:
            already_warned = Notification.objects.filter(
                related_request=sr,
                user=sr.consumer,
                message__contains="will expire in 3 days",
            ).exists()
            if not already_warned:
                Notification.objects.create(
                    user=sr.consumer,
                    message=(
                        f"Request #{sr.id} will expire in 3 days if no progress "
                        "is made. Please complete any required actions to keep "
                        "your request active."
                    ),
                    notification_type=Notification.NotificationType.STATUS_CHANGE,
                    related_request=sr,
                )
                warned_count += 1

        to_expire = cls.objects.filter(
            status__in=stale_statuses,
            updated_at__lt=expiry_cutoff,
        )
        expired_ids = list(to_expire.values_list("id", flat=True))
        if expired_ids:
            cls.objects.filter(id__in=expired_ids).update(status=cls.Status.EXPIRED)
            for sr in cls.objects.filter(id__in=expired_ids).select_related("consumer"):
                Notification.objects.create(
                    user=sr.consumer,
                    message=(
                        f"Request #{sr.id} has expired due to no activity for "
                        "7 days. Please submit a new request if you still need "
                        "this service."
                    ),
                    notification_type=Notification.NotificationType.STATUS_CHANGE,
                    related_request=sr,
                )

        return {"warned": warned_count, "expired": len(expired_ids)}

    @property
    def is_within_bayawan(self) -> bool:
        """
        Determine if this request is within Bayawan City based on coordinates when available.
        Falls back to a simple barangay string check for legacy records.
        """
        try:
            if self.gps_latitude is not None and self.gps_longitude is not None:
                from services.location import within_service_bounds

                return within_service_bounds(
                    float(self.gps_latitude),
                    float(self.gps_longitude),
                )
        except Exception:
            pass
        return bool(self.barangay and self.barangay != "Outside Bayawan City")

    @property
    def waived_inspection_crew_ready(self) -> bool:
        """True when waived desludging request has a driver assigned (helpers optional)."""
        if "[NO_INSPECTION_FEE]" not in (self.notes or ""):
            return False
        if self.service_type not in (
            self.ServiceType.RESIDENTIAL_DESLUDGING,
            self.ServiceType.COMMERCIAL_DESLUDGING,
        ):
            return True
        return bool((self.waived_crew_driver_name or "").strip())

    @property
    def waived_inspection_personnel_count(self) -> int:
        """Headcount for meals/transport (1 driver + filled helper slots)."""
        n = 1
        for h in (
            self.waived_crew_helper1_name,
            self.waived_crew_helper2_name,
            self.waived_crew_helper3_name,
        ):
            if (h or "").strip():
                n += 1
        return max(1, n)

    @property
    def consumer_is_bayawan_city_resident(self) -> bool:
        """
        True when the registered consumer's profile municipality is Bayawan City.
        Used with is_within_bayawan for first-N-km distance waivers on computation.
        """
        try:
            return self.consumer.consumer_profile.is_bayawan_city_municipality
        except Exception:
            return False

    @property
    def bawad_free_eligible(self) -> bool:
        """BAWAD members get free service if < 5 m3 used within 4-year cycle."""
        if not self.connected_to_bawad:
            return False
        from dashboard.models import ConfigurableRate
        limit = ConfigurableRate.get("bawad_free_limit_m3", 5)
        cycle_years = ConfigurableRate.get("bawad_cycle_years", 4)
        cutoff = timezone.now().date() - timedelta(days=int(cycle_years) * 365)
        used = (
            ServiceRequest.objects.filter(
                consumer=self.consumer,
                service_type__in=[
                    self.ServiceType.RESIDENTIAL_DESLUDGING,
                    self.ServiceType.COMMERCIAL_DESLUDGING,
                ],
                status=self.Status.COMPLETED,
                request_date__gte=cutoff,
            )
            .exclude(pk=self.pk)
            .aggregate(total=models.Sum("cubic_meters"))["total"]
            or 0
        )
        return used < limit

    @property
    def qualifies_public_bayawan_no_fees(self) -> bool:
        """Public septage requests with location in Bayawan City: no inspection fee; computation is ₱0."""
        if self.service_type not in (
            self.ServiceType.RESIDENTIAL_DESLUDGING,
            self.ServiceType.COMMERCIAL_DESLUDGING,
        ):
            return False
        if self.public_private != self.PublicPrivate.PUBLIC:
            return False
        return self.is_within_bayawan

    def apply_public_bayawan_inspection_fee_waiver(self, *, notify_user=None) -> bool:
        """
        Mark first-time inspection fee as satisfied and record policy note. Idempotent.
        Does not change status (caller may set UNDER_REVIEW, etc.).
        """
        if not self.qualifies_public_bayawan_no_fees:
            return False
        notes = self.notes or ""
        if PUBLIC_BAYAWAN_NO_FEES_FLAG in notes:
            if not self.inspection_fee_paid:
                self.inspection_fee_paid = True
                self.save(update_fields=["inspection_fee_paid"])
            return True
        self.inspection_fee_paid = True
        line = (
            f"{PUBLIC_BAYAWAN_NO_FEES_FLAG} Inspection fee waived — public property within Bayawan City "
            "(no treasurer inspection fee; computation charges are ₱0 per policy)."
        )
        self.notes = (notes + "\n" if notes else "") + line
        self.save(update_fields=["inspection_fee_paid", "notes"])
        if notify_user:
            Notification.objects.create(
                user=notify_user,
                message=(
                    f"Request #{self.id}: No ₱150 inspection fee is required — public property within Bayawan City."
                ),
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=self,
            )
        return True


class InspectionDetail(models.Model):
    service_request = models.OneToOneField(
        ServiceRequest, on_delete=models.CASCADE, related_name="inspection_detail"
    )
    inspection_date = models.DateField()
    inspected_by = models.CharField(max_length=255)
    inspector_signature = models.FileField(upload_to="inspector_signatures/", null=True, blank=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Inspection for {self.service_request} on {self.inspection_date}"


class DesludgingPersonnel(models.Model):
    """Named drivers and helpers for dropdowns on completion forms (managed under Staff)."""

    class Role(models.TextChoices):
        DRIVER = "DRIVER", "Driver"
        HELPER = "HELPER", "Helper"

    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=10, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["role", "full_name"]
        verbose_name_plural = "Desludging personnel"

    def __str__(self) -> str:
        return f"{self.get_role_display()}: {self.full_name}"


class ServiceEquipment(models.Model):
    """Decloggers and other units; managed under Admin → Equipment, chosen on completion forms."""

    unit_number = models.CharField(max_length=50, unique=True)
    notes = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["unit_number"]
        verbose_name_plural = "Service equipment"

    def __str__(self) -> str:
        return self.unit_number


class CompletionInfo(models.Model):
    service_request = models.OneToOneField(
        ServiceRequest, on_delete=models.CASCADE, related_name="completion_info"
    )
    date_completed = models.DateField()
    time_required = models.CharField(max_length=100, help_text="e.g. 2 hours 30 mins")
    witnessed_by_name = models.CharField(max_length=255, blank=True)
    witnessed_by_signature = models.FileField(upload_to="witness_signatures/", null=True, blank=True)
    equipment = models.ForeignKey(
        ServiceEquipment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completions",
    )
    declogger_no = models.CharField(max_length=50, blank=True)
    fuel_consumption = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    fuel_unit = models.CharField(max_length=20, default="liters")

    driver_name = models.CharField(max_length=255)
    driver_signature = models.FileField(upload_to="driver_signatures/", null=True, blank=True)

    helper1_name = models.CharField(max_length=255, blank=True)
    helper1_signature = models.FileField(upload_to="helper_signatures/", null=True, blank=True)
    helper2_name = models.CharField(max_length=255, blank=True)
    helper2_signature = models.FileField(upload_to="helper_signatures/", null=True, blank=True)
    helper3_name = models.CharField(max_length=255, blank=True)
    helper3_signature = models.FileField(upload_to="helper_signatures/", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Completion for {self.service_request} on {self.date_completed}"

    @property
    def personnel_count(self) -> int:
        count = 1  # driver
        if self.helper1_name:
            count += 1
        if self.helper2_name:
            count += 1
        if self.helper3_name:
            count += 1
        return count


class ServiceRequestChangeLog(models.Model):
    """Audit log for admin edits to Grass Cutting request details (and optionally other types)."""
    service_request = models.ForeignKey(
        ServiceRequest,
        on_delete=models.CASCADE,
        related_name="change_logs",
    )
    changed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(help_text="Admin reason for the change")
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Change log for request #{self.service_request_id} at {self.created_at}"


class Notification(models.Model):
    class NotificationType(models.TextChoices):
        REQUEST_SUBMITTED = "REQUEST_SUBMITTED", "Request Submitted"
        INSPECTOR_ASSIGNED = "INSPECTOR_ASSIGNED", "Inspector Assigned"
        COMPUTATION_READY = "COMPUTATION_READY", "Computation Ready"
        PAYMENT_UPLOADED = "PAYMENT_UPLOADED", "Payment Uploaded"
        DESLUDGING_SCHEDULED = "DESLUDGING_SCHEDULED", "Desludging Scheduled"
        STATUS_CHANGE = "STATUS_CHANGE", "Status Change"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=500)
    notification_type = models.CharField(
        max_length=30,
        choices=NotificationType.choices,
        default=NotificationType.STATUS_CHANGE,
    )
    related_request = models.ForeignKey(
        ServiceRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    link = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Notification for {self.user}: {self.message[:50]}"
