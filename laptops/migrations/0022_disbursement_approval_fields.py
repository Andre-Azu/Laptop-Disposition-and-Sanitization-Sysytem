from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("laptops", "0021_clean_remaining_assessment_audit_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="disbursement_approved",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="donationledger",
            name="rejection_reason",
            field=models.TextField(blank=True, default=""),
        ),
    ]
