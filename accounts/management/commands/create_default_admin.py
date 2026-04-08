import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Creates a default superuser from environment variables if none exists."

    def handle(self, *args, **kwargs):
        User = get_user_model()

        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")

        if not username or not password:
            self.stdout.write(
                self.style.WARNING(
                    "Skipping admin creation: DJANGO_SUPERUSER_USERNAME or "
                    "DJANGO_SUPERUSER_PASSWORD not set."
                )
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(
                self.style.SUCCESS(f'Admin "{username}" already exists. Skipping.')
            )
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(
            self.style.SUCCESS(f'Superuser "{username}" created successfully.')
        )
