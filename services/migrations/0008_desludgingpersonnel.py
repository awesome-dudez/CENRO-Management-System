from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0007_alter_servicerequest_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="DesludgingPersonnel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("full_name", models.CharField(max_length=255)),
                (
                    "role",
                    models.CharField(
                        choices=[("DRIVER", "Driver"), ("HELPER", "Helper")],
                        max_length=10,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name_plural": "Desludging personnel",
                "ordering": ["role", "full_name"],
            },
        ),
    ]
