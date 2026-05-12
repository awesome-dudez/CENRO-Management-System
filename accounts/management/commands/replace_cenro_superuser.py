"""
Replace all Django superusers with a single CENRO super admin account.

Deletes every User with is_superuser=True, then creates one new superuser.
Also removes any existing row with the target username (so the username is free).

Password: pass --password once, or set environment variable (recommended):

  PowerShell:
    $env:CENRO_REPLACE_SUPERUSER_PASSWORD = 'your-secure-password'
    python manage.py replace_cenro_superuser --username cenro_admin

  bash:
    export CENRO_REPLACE_SUPERUSER_PASSWORD='your-secure-password'
    python manage.py replace_cenro_superuser --username cenro_admin
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Delete existing superusers and create one new superuser (CENRO admin)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default="cenro_admin",
            help="Login username for the new superuser (default: cenro_admin).",
        )
        parser.add_argument(
            "--email",
            default="cenro.admin@bayawan.local",
            help="Email for the new superuser.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Plain password (avoid in shared shells). Prefer env CENRO_REPLACE_SUPERUSER_PASSWORD.",
        )

    def handle(self, *args, **options):
        username = (options["username"] or "").strip()
        if not username:
            raise CommandError("Username must not be empty.")

        password = (options["password"] or "").strip() or (
            os.environ.get("CENRO_REPLACE_SUPERUSER_PASSWORD") or ""
        ).strip()
        if not password:
            raise CommandError(
                "Provide a password via --password or set environment variable "
                "CENRO_REPLACE_SUPERUSER_PASSWORD."
            )

        email = (options["email"] or "").strip() or f"{username}@local.invalid"

        with transaction.atomic():
            removed_same = User.objects.filter(username=username).delete()
            removed_supers = User.objects.filter(is_superuser=True).delete()

            user = User.objects.create_superuser(
                username=username,
                email=email,
                password=password,
            )
            user.role = User.Role.ADMIN
            user.is_approved = True
            user.must_change_password = False
            user.save(update_fields=["role", "is_approved", "must_change_password"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Superuser setup complete: username={username!r}, email={email!r}. "
                "Log in at /accounts/login/ or /admin/."
            )
        )
        self.stdout.write(
            f"(Removed {removed_same[0]} user row(s) with that username, "
            f"{removed_supers[0]} row(s) from other superuser deletions incl. cascades.)"
        )
