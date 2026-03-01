"""
Create 20 dummy Consumer accounts for testing.
Usage: python manage.py seed_consumers

- Role: Consumer
- Default password: Password123!
- Creates User + ConsumerProfile (realistic names, emails, Philippine phone numbers)
- Does not modify existing users (skips if username already exists)
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from accounts.models import ConsumerProfile


User = get_user_model()
DEFAULT_PASSWORD = "Password123!"

# 20 realistic names (First, Last) – Philippine-style names
CONSUMER_DATA = [
    ("Maria", "Santos"),
    ("Juan", "Dela Cruz"),
    ("Ana", "Reyes"),
    ("Roberto", "Garcia"),
    ("Carmen", "Lopez"),
    ("Pedro", "Mendoza"),
    ("Rosa", "Fernandez"),
    ("Jose", "Ramos"),
    ("Teresa", "Gonzalez"),
    ("Antonio", "Villanueva"),
    ("Elena", "Castillo"),
    ("Miguel", "Torres"),
    ("Lourdes", "Silva"),
    ("Francisco", "Cruz"),
    ("Sofia", "Aquino"),
    ("Carlos", "Romero"),
    ("Imelda", "Pascual"),
    ("Ramon", "Ocampo"),
    ("Consuelo", "Navarro"),
    ("Felipe", "Bautista"),
]

# Barangays in/around Bayawan City area (for realism)
BARANGAYS = [
    "Amanjuan",
    "Balabag",
    "Bangon",
    "Bantayan",
    "Batawan",
    "Biasong",
    "Bulak",
    "Kalumboyan",
    "Magatas",
    "Malabugas",
    "Maninihon",
    "Nangka",
    "Naraja",
    "Poblacion",
    "San Isidro",
    "San Jose",
    "San Miguel",
    "Tabuan",
    "Tiling",
    "Villareal",
]

MUNICIPALITY = "Bayawan City"
PROVINCE = "Negros Oriental"


def make_phone(index: int) -> str:
    """Generate a Philippine mobile number (09XX XXX XXXX)."""
    # 09XX 5XX XXXX style
    base = 9000000000 + (index * 15273) % 100000000
    return f"09{base % 100000000:08d}"


def make_email(first: str, last: str, index: int) -> str:
    """Generate a unique email (lowercase, no spaces)."""
    clean_first = first.lower().replace(" ", "")
    clean_last = last.lower().replace(" ", "")
    return f"{clean_first}.{clean_last}{index}@test.consumer.local"


class Command(BaseCommand):
    help = "Create 20 dummy Consumer accounts (password: Password123!). Does not affect existing users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-create ConsumerProfile for existing consumer usernames (user record unchanged).",
        )

    def handle(self, *args, **options):
        force = options.get("force", False)
        created_count = 0
        skipped_count = 0
        profile_updated = 0

        for i, (first_name, last_name) in enumerate(CONSUMER_DATA):
            username = f"consumer{i + 1}"
            email = make_email(first_name, last_name, i + 1)
            phone = make_phone(i + 1)
            barangay = BARANGAYS[i % len(BARANGAYS)]

            user, user_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "role": User.Role.CONSUMER,
                    "is_approved": True,
                    "is_active": True,
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            if user_created:
                user.set_password(DEFAULT_PASSWORD)
                user.save()
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  Created: {username} ({user.get_full_name()})")
                )
            else:
                # Existing user – do not change password or role
                if user.role != User.Role.CONSUMER:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Skipped: {username} (existing user is not Consumer)"
                        )
                    )
                    skipped_count += 1
                    continue
                skipped_count += 1
                if not force:
                    self.stdout.write(
                        self.style.NOTICE(f"  Exists:  {username} ({user.get_full_name()})")
                    )

            # Create or update ConsumerProfile (for new users or existing Consumers)
            profile_defaults = {
                "mobile_number": phone,
                "barangay": barangay,
                "municipality": MUNICIPALITY,
                "province": PROVINCE,
                "street_address": f"Block {i + 1} Lot {((i * 7) % 50) + 1}",
                "gender": ConsumerProfile.Gender.FEMALE
                if first_name in ("Maria", "Ana", "Carmen", "Rosa", "Teresa", "Elena", "Lourdes", "Sofia", "Imelda", "Consuelo")
                else ConsumerProfile.Gender.MALE,
            }
            profile, profile_created = ConsumerProfile.objects.get_or_create(
                user=user,
                defaults=profile_defaults,
            )
            if not profile_created and force:
                for key, value in profile_defaults.items():
                    setattr(profile, key, value)
                profile.save()
                profile_updated += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {created_count} | Already existed: {skipped_count}"
                + (f" | Profiles updated: {profile_updated}" if profile_updated else "")
            )
        )
        if created_count > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Default password for new accounts: {DEFAULT_PASSWORD}"
                )
            )
