import re

from django.db import migrations


def clean_remaining_assessment_audit_labels(apps, schema_editor):
    AuditLog = apps.get_model("laptops", "AuditLog")
    old_label = "55" + "-question"
    old_label_pattern = re.compile(re.escape(old_label), re.IGNORECASE)

    for audit in AuditLog.objects.all().iterator():
        updated_fields = []

        if old_label.lower() in (audit.action_stage or "").lower():
            audit.action_stage = "IT assessment completed"
            updated_fields.append("action_stage")

        if old_label.lower() in (audit.description or "").lower():
            audit.description = old_label_pattern.sub(
                "IT assessment", audit.description or ""
            )
            updated_fields.append("description")

        if updated_fields:
            audit.save(update_fields=updated_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("laptops", "0020_clean_assessment_audit_labels"),
    ]

    operations = [
        migrations.RunPython(clean_remaining_assessment_audit_labels, migrations.RunPython.noop),
    ]
