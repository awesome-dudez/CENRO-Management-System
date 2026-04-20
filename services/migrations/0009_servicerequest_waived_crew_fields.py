from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0008_desludgingpersonnel"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicerequest",
            name="waived_crew_driver_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="waived_crew_helper1_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="waived_crew_helper2_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="waived_crew_helper3_name",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
