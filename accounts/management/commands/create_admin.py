"""
Create or reset the Django admin superuser.
Usage: python manage.py create_admin

Default credentials (change after first login in production):
  Username: admin
  Password: admin123
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


User = get_user_model()

# Default credentials — change in production
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"
DEFAULT_EMAIL = "admin@cenro.local"


class Command(BaseCommand):
    help = "Create or reset the admin superuser (username: admin, password: admin123)."

    def handle(self, *args, **options):
        user, created = User.objects.update_or_create(
            username=DEFAULT_USERNAME,
            defaults={
                "email": DEFAULT_EMAIL,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
                "is_approved": True,
                "role": User.Role.ADMIN,
            },
        )
        user.set_password(DEFAULT_PASSWORD)
        user.save()

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Admin user created. Username: {DEFAULT_USERNAME}, Password: {DEFAULT_PASSWORD}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Admin user reset. Username: {DEFAULT_USERNAME}, Password: {DEFAULT_PASSWORD}"
                )
            )
        self.stdout.write("Log in at: http://127.0.0.1:8000/admin/")
