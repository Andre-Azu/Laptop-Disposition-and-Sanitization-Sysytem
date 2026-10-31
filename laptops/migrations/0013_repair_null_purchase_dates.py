import datetime

from django.db import migrations


def repair_null_purchase_dates(apps, schema_editor):
    Device = apps.get_model("laptops", "Device")
    today = datetime.date.today()

    for device in Device.objects.filter(purchase_date__isnull=True):
        purchase_date = None
        if device.acquisition_period:
            try:
                year, month = (int(part) for part in device.acquisition_period.split("-", 1))
                purchase_date = datetime.date(year, month, 1)
            except (TypeError, ValueError):
                pass

        device.purchase_date = purchase_date or today
        device.save(update_fields=["purchase_date"])


class Migration(migrations.Migration):
    dependencies = [
        ("laptops", "0012_device_acquisition_period"),
    ]

    operations = [
        migrations.RunPython(repair_null_purchase_dates, migrations.RunPython.noop),
    ]
