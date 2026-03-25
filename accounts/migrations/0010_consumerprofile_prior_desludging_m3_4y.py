from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_user_is_legacy_record"),
    ]

    operations = [
        migrations.AddField(
            model_name="consumerprofile",
            name="prior_desludging_m3_4y",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Desludging volume (m³) from manual/pre-system records in the past 4 years.",
                max_digits=7,
            ),
        ),
    ]

