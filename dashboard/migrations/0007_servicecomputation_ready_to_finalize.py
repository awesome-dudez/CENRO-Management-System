from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0006_servicecomputation_waive_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicecomputation",
            name="ready_to_finalize",
            field=models.BooleanField(
                default=False,
                help_text="Set when charges are saved from the edit screen; enables Finalize on the letter.",
            ),
        ),
    ]
