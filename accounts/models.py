from __future__ import annotations

import random
import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        STAFF = "STAFF", "Staff"
        CONSUMER = "CONSUMER", "Consumer"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CONSUMER,
    )
    is_approved = models.BooleanField(
        default=False,
        help_text="For staff and consumer accounts that require admin approval.",
    )
    must_change_password = models.BooleanField(
        default=False,
        help_text="If true, user must change password on next login (staff temp passwords; consumer after admin reset).",
    )
    is_legacy_record = models.BooleanField(
        default=False,
        help_text="True when this consumer account was created by admin from pre-system/manual records.",
    )

    def save(self, *args, **kwargs):
        if self.first_name:
            self.first_name = self.first_name.strip().title()
        if self.last_name:
            self.last_name = self.last_name.strip().title()
        super().save(*args, **kwargs)

    def is_admin(self) -> bool:
        """True if user is Admin role or Django superuser (super admin)."""
        return self.role == self.Role.ADMIN or self.is_superuser

    def is_staff_member(self) -> bool:
        return self.role == self.Role.STAFF

    def is_consumer(self) -> bool:
        return self.role == self.Role.CONSUMER

    @property
    def gender(self) -> str:
        try:
            return self.consumer_profile.gender
        except ConsumerProfile.DoesNotExist:
            return "MALE"

    @property
    def profile_picture_url(self) -> str:
        from cenro_mgmt.media_utils import file_url_if_exists

        try:
            return file_url_if_exists(self.consumer_profile.profile_picture)
        except ConsumerProfile.DoesNotExist:
            pass
        return ""


class ConsumerProfile(models.Model):
    class Gender(models.TextChoices):
        MALE = "MALE", "Male"
        FEMALE = "FEMALE", "Female"
        NON_BINARY = "NON_BINARY", "Non-binary"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="consumer_profile")
    profile_picture = models.ImageField(upload_to="profile_pictures/", null=True, blank=True)
    gender = models.CharField(max_length=20, choices=Gender.choices, default=Gender.MALE)
    birthdate = models.DateField(null=True, blank=True)
    mobile_number = models.CharField(max_length=20, blank=True)
    street_address = models.CharField(max_length=500, blank=True)
    barangay = models.CharField(max_length=255, blank=True)
    municipality = models.CharField(max_length=255, blank=True)
    province = models.CharField(max_length=255, blank=True)
    prior_desludging_m3_4y = models.PositiveIntegerField(
        default=0,
        help_text="Desludging volume in whole cubic meters (m³) from manual/pre-system records in the past 4 years.",
    )
    last_cycle_request_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date when the account last had a desludging request counted for the current cycle (manual/pre-system).",
    )
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    @property
    def full_address(self) -> str:
        parts = [self.street_address, self.barangay, self.municipality, self.province]
        return ", ".join(p for p in parts if p)

    @property
    def profile_picture_safe_url(self) -> str:
        from cenro_mgmt.media_utils import file_url_if_exists

        return file_url_if_exists(self.profile_picture)

    @property
    def is_bayawan_city_municipality(self) -> bool:
        """True when profile municipality indicates Bayawan City (for resident distance rules)."""
        m = (self.municipality or "").strip().casefold()
        if not m:
            return False
        if m in ("bayawan city", "city of bayawan", "bayawan"):
            return True
        return "bayawan" in m and "city" in m

    def __str__(self) -> str:
        return f"{self.user.get_full_name()} - {self.barangay or self.municipality}"


class PasswordResetToken(models.Model):
    """One-time password reset token stored in the database."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_tokens",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    code = models.CharField(max_length=6, blank=True)   # 6-digit OTP alternative
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def create_for_user(cls, user, minutes: int = 15) -> "PasswordResetToken":
        """Invalidate any existing tokens for this user, then issue a fresh one."""
        cls.objects.filter(user=user, is_used=False).update(is_used=True)
        otp = "".join(str(random.randint(0, 9)) for _ in range(6))
        return cls.objects.create(
            user=user,
            token=secrets.token_urlsafe(40),
            code=otp,
            expires_at=timezone.now() + timezone.timedelta(minutes=minutes),
        )

    def is_valid(self) -> bool:
        return not self.is_used and timezone.now() <= self.expires_at

    def invalidate(self) -> None:
        self.is_used = True
        self.save(update_fields=["is_used"])

    def __str__(self) -> str:
        return f"ResetToken({self.user.username}, expires={self.expires_at:%Y-%m-%d %H:%M})"


class ProfileContactChangeToken(models.Model):
    """OTP sent to the user's current email before applying new email/mobile on the profile."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="profile_contact_change_tokens",
    )
    new_email = models.EmailField()
    new_mobile = models.CharField(max_length=20)
    sent_to_email = models.EmailField()
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def create_for_user(cls, user: "User", new_email: str, new_mobile: str, minutes: int = 15):
        cls.objects.filter(user=user, is_used=False).update(is_used=True)
        otp = "".join(str(random.randint(0, 9)) for _ in range(6))
        return cls.objects.create(
            user=user,
            new_email=new_email.strip(),
            new_mobile=new_mobile,
            sent_to_email=(user.email or "").strip(),
            code=otp,
            expires_at=timezone.now() + timezone.timedelta(minutes=minutes),
        )

    def is_valid(self) -> bool:
        return not self.is_used and timezone.now() <= self.expires_at

    def invalidate(self) -> None:
        self.is_used = True
        self.save(update_fields=["is_used"])

    def __str__(self) -> str:
        return f"ContactChangeToken({self.user.username}, expires={self.expires_at:%Y-%m-%d %H:%M})"


class ProfileContactChangeRequest(models.Model):
    """Consumer could not access the old inbox; admin reviews and applies new contact details."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="profile_contact_change_requests",
    )
    proposed_email = models.EmailField()
    proposed_mobile = models.CharField(max_length=20)
    previous_email = models.EmailField(blank=True)
    previous_mobile = models.CharField(max_length=20, blank=True)
    customer_reason = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="profile_contact_change_decisions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ContactChangeRequest({self.user.username}, {self.status})"

