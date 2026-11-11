from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("laptops", "0022_disbursement_approval_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="device",
            name="status",
            field=models.CharField(
                choices=[
                    ("REGISTERED", "Registered"),
                    ("PENDING_Admin_assesment", "Pending IT Admin Assessment"),
                    ("IT_ADMIN_ASSESMENT_IN_PROGRESS", "IT Assessment In Progress"),
                    ("IT_Admin_assesment_complete", "IT Assessment Complete"),
                    ("Sanitization_Processing", "Sanitization Processing"),
                    ("Repair_Processing", "Repair Processing"),
                    ("REPAIR_AND_SANITIZATION_PENDING", "Repair and Sanitization Pending"),
                    ("DISPOSITION_PENDING", "Disposition Pending"),
                    ("DISBURSEMENT_DENIED", "Disbursement Denied"),
                    ("SCHEDULED_FOR_COLLECTION", "Scheduled for Collection"),
                    ("COLLECTED", "Collected"),
                    ("SANITIZATION_FAILED", "Sanitization Failed"),
                    ("FAILED", "Repair Failed"),
                    ("SANITIZED", "SANITIZED"),
                    ("COMPLETED", "Completed"),
                    ("DONATED", "Donated"),
                ],
                default="REGISTERED",
                max_length=40,
            ),
        ),
    ]
