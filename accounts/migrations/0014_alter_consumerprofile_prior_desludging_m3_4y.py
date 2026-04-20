# Generated manually — round decimal m³ to whole numbers then store as integer.

from decimal import Decimal

from django.db import migrations, models


def round_prior_volume_to_whole_m3(apps, schema_editor):
    ConsumerProfile = apps.get_model("accounts", "ConsumerProfile")
    for row in ConsumerProfile.objects.all().only("id", "prior_desludging_m3_4y"):
        v = row.prior_desludging_m3_4y
        if v is None:
            continue
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            n = 0
        if n < 0:
            n = 0
        ConsumerProfile.objects.filter(pk=row.pk).update(
            prior_desludging_m3_4y=Decimal(str(n)),
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_consumerprofile_last_cycle_request_date"),
    ]

    operations = [
        migrations.RunPython(round_prior_volume_to_whole_m3, noop_reverse),
        migrations.AlterField(
            model_name="consumerprofile",
            name="prior_desludging_m3_4y",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Desludging volume in whole cubic meters (m³) from manual/pre-system records in the past 4 years.",
            ),
        ),
    ]
