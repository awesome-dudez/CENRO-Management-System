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

    def save(self, *args, **kwargs):
        if self.first_name:
            self.first_name = self.first_name.strip().title()
        if self.last_name:
            self.last_name = self.last_name.strip().title()
        super().save(*args, **kwargs)

    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

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
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    @property
    def full_address(self) -> str:
        parts = [self.street_address, self.barangay, self.municipality, self.province]
        return ", ".join(p for p in parts if p)

    def __str__(self) -> str:
        return f"{self.user.get_full_name()} - {self.barangay or self.municipality}"

