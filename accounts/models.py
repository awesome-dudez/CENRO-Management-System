from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


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
        try:
            if self.consumer_profile.profile_picture:
                return self.consumer_profile.profile_picture.url
        except ConsumerProfile.DoesNotExist:
            pass
        return ""


class ConsumerProfile(models.Model):
    class Gender(models.TextChoices):
        MALE = "MALE", "Male"
        FEMALE = "FEMALE", "Female"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="consumer_profile")
    profile_picture = models.ImageField(upload_to="profile_pictures/", null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, default=Gender.MALE)
    birthdate = models.DateField(null=True, blank=True)
    mobile_number = models.CharField(max_length=20, blank=True)
    street_address = models.CharField(max_length=500, blank=True)
    barangay = models.CharField(max_length=255, blank=True)
    municipality = models.CharField(max_length=255, blank=True)
    province = models.CharField(max_length=255, blank=True)
    prior_desludging_m3_4y = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=0,
        help_text="Desludging volume (m³) from manual/pre-system records in the past 4 years.",
    )
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    @property
    def full_address(self) -> str:
        parts = [self.street_address, self.barangay, self.municipality, self.province]
        return ", ".join(p for p in parts if p)

    def __str__(self) -> str:
        return f"{self.user.get_full_name()} - {self.barangay or self.municipality}"

