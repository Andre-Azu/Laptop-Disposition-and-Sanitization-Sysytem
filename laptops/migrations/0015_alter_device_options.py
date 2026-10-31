from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("laptops", "0014_alter_device_status"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="device",
            options={"ordering": ["-created_at", "-pk"]},
        ),
    ]
