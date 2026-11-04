from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("laptops", "0018_alter_auditlog_options_alter_device_status"),
    ]

    operations = [
        migrations.AddField(model_name="sanitizationcertificate", name="sanitization_standard", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="sanitizationcertificate", name="tool_used", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="sanitizationcertificate", name="pass_count", field=models.CharField(blank=True, default="", max_length=20)),
        migrations.AddField(model_name="sanitizationcertificate", name="drive_serial_number", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="sanitizationcertificate", name="storage_media_type", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="sanitizationcertificate", name="technician_notes", field=models.TextField(blank=True, default="")),
    ]
