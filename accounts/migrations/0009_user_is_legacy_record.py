from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_user_must_change_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="is_legacy_record",
            field=models.BooleanField(
                default=False,
                help_text="True when this consumer account was created by admin from pre-system/manual records.",
            ),
        ),
    ]

