# Data migration: recalculate Distance Travel Fee and Wear & Tear for all existing computations

from django.db import migrations


def recalculate_charges(apps, schema_editor):
    """Use the current model's calculate_charges() so Distance Travel Fee and Wear & Tear are correct."""
    from dashboard.models import ServiceComputation
    for comp in ServiceComputation.objects.all():
        comp.calculate_charges()
        comp.save()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0004_add_distance_travel_fee'),
    ]

    operations = [
        migrations.RunPython(recalculate_charges, noop),
    ]
