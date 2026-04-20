from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0005_recalculate_computation_charges"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicecomputation",
            name="waive_meals_transport_charge",
            field=models.BooleanField(
                default=False,
                help_text="Admin: waive meals & transportation charge for this computation.",
            ),
        ),
        migrations.AddField(
            model_name="servicecomputation",
            name="waive_wear_charge",
            field=models.BooleanField(
                default=False,
                help_text="Admin: waive wear & tear (20% of fixed trucking + distance travel) for this computation.",
            ),
        ),
    ]
