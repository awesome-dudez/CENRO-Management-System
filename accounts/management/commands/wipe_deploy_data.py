"""
Remove test/demo transactional data before production deploy.

Deletes (database only):
  - All service requests and cascaded rows (computations, schedules, completion, etc.)
  - All consumers and staff (every User except superusers)
  - Notifications, password-reset tokens, profile contact tokens/requests
  - Django sessions and admin log entries

Preserves:
  - All User rows with is_superuser=True (passwords unchanged unless --recreate-admin)
  - DesludgingPersonnel and ServiceEquipment (reference lists for forms)

Re-seeds:
  - ConfigurableRate (billing constants from model defaults)
  - ChargeCategory (Residential + Commercial rows required for computations)

Usage (local SQLite or Render shell):
  python manage.py wipe_deploy_data --yes

Optional:
  python manage.py wipe_deploy_data --yes --recreate-admin
      # resets username "admin" to password admin123 (see create_admin command)

Do NOT run against a database you need to keep unless you have backups.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import PasswordResetToken, ProfileContactChangeRequest, ProfileContactChangeToken
from dashboard.models import ChargeCategory, ConfigurableRate
from services.models import Notification, ServiceRequest

User = get_user_model()


def _seed_charge_categories() -> None:
    categories = [
        {
            "category": ChargeCategory.Category.RESIDENTIAL,
            "base_rate": Decimal("100.00"),
            "description": "Residential property septage declogging service charges",
        },
        {
            "category": ChargeCategory.Category.COMMERCIAL,
            "base_rate": Decimal("150.00"),
            "description": "Commercial property septage declogging service charges",
        },
    ]
    for row in categories:
        ChargeCategory.objects.get_or_create(
            category=row["category"],
            defaults={"base_rate": row["base_rate"], "description": row["description"]},
        )


class Command(BaseCommand):
    help = "Wipe demo/test DB data while keeping superusers and form reference data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm destructive wipe (required).",
        )
        parser.add_argument(
            "--recreate-admin",
            action="store_true",
            help="After wipe, reset/create admin user via create_admin (admin / admin123).",
        )

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError(
                "Refusing to run without --yes. This permanently deletes almost all user "
                "and request data. Example: python manage.py wipe_deploy_data --yes"
            )

        with transaction.atomic():
            n_sessions = Session.objects.count()
            Session.objects.all().delete()

            n_logs = LogEntry.objects.count()
            LogEntry.objects.all().delete()

            PasswordResetToken.objects.all().delete()
            ProfileContactChangeToken.objects.all().delete()
            ProfileContactChangeRequest.objects.all().delete()

            Notification.objects.all().delete()

            n_sr = ServiceRequest.objects.count()
            ServiceRequest.objects.all().delete()

            superusers = list(User.objects.filter(is_superuser=True).values_list("pk", flat=True))
            deleted_total, _ = User.objects.filter(is_superuser=False).delete()

            ConfigurableRate.objects.all().delete()
            ConfigurableRate.seed_defaults()

            ChargeCategory.objects.all().delete()
            _seed_charge_categories()

        self.stdout.write(
            self.style.SUCCESS(
                f"Wipe complete: removed {n_sr} service request(s), "
                f"{deleted_total} object(s) across related tables (users, profiles, etc.), "
                f"{n_sessions} session(s), {n_logs} admin log entr(y/ies). "
                f"Kept {len(superusers)} superuser account(s)."
            )
        )
        self.stdout.write(
            "Re-seeded ConfigurableRate and ChargeCategory. "
            "DesludgingPersonnel and ServiceEquipment were not removed."
        )
        self.stdout.write(
            self.style.WARNING(
                "Recreate staff accounts and consumer registrations as needed. "
                "Uploaded files under MEDIA_ROOT may still exist until you clear storage."
            )
        )

        if options["recreate_admin"]:
            call_command("create_admin")
            self.stdout.write(self.style.SUCCESS("Ran create_admin (admin / admin123)."))

        if not User.objects.filter(is_superuser=True).exists():
            self.stdout.write(
                self.style.ERROR(
                    "No superuser found. Create one before using the admin UI: "
                    "python manage.py create_admin   OR   python manage.py createsuperuser"
                )
            )
