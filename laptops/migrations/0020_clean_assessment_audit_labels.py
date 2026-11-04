from django.db import migrations


def clean_assessment_audit_labels(apps, schema_editor):
    AuditLog = apps.get_model("laptops", "AuditLog")
    old_label = "55" + "-question IT assessment completed"
    AuditLog.objects.filter(action_stage__icontains=old_label).update(
        action_stage="IT assessment completed"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("laptops", "0019_sanitization_certificate_parameters"),
    ]

    operations = [
        migrations.RunPython(clean_assessment_audit_labels, migrations.RunPython.noop),
    ]
