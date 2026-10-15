import datetime
from decimal import Decimal
import uuid

import django.contrib.auth.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


COMPONENT_STATUS_CHOICES = [
    ("WORKING", "Working"),
    ("PARTIALLY_WORKING", "Partially Working"),
    ("NOT_WORKING", "Not Working"),
    ("MISSING", "Missing"),
    ("NOT_ASSESSED", "Not Assessed"),
]


def _component_status(value):
    if value in {"WORKING", "PARTIALLY_WORKING", "NOT_WORKING", "MISSING", "NOT_ASSESSED"}:
        return value
    value = str(value or "").lower()
    if value in {"passed", "pass", "fixed", "working"}:
        return "WORKING"
    if value in {"warning", "partially_working", "partially working"}:
        return "PARTIALLY_WORKING"
    if value in {"failed", "fail", "not_fixed", "not working"}:
        return "NOT_WORKING"
    if value == "missing":
        return "MISSING"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "NOT_ASSESSED"
    if score >= 8:
        return "WORKING"
    if score >= 5:
        return "PARTIALLY_WORKING"
    return "NOT_WORKING"


def _keep_latest_per_device(model, timestamp_field):
    seen = set()
    for row in model.order_by(f"-{timestamp_field}", "-pk").iterator():
        if row.device_id in seen:
            row.delete()
        else:
            seen.add(row.device_id)


def migrate_legacy_data(apps, schema_editor):
    database = schema_editor.connection.alias
    User = apps.get_model("laptops", "User")
    Device = apps.get_model("laptops", "Device")
    OwnerSelfAssessment = apps.get_model("laptops", "OwnerSelfAssessment")
    TechnicalAssessment = apps.get_model("laptops", "TechnicalAssessment")
    RepairEvaluation = apps.get_model("laptops", "RepairEvaluation")
    SanitizationJob = apps.get_model("laptops", "SanitizationJob")
    SanitizationCertificate = apps.get_model("laptops", "SanitizationCertificate")
    DonationLedger = apps.get_model("laptops", "DonationLedger")

    _keep_latest_per_device(OwnerSelfAssessment.objects.using(database), "created_at")
    _keep_latest_per_device(TechnicalAssessment.objects.using(database), "created_at")
    _keep_latest_per_device(SanitizationCertificate.objects.using(database), "issued_at")
    _keep_latest_per_device(DonationLedger.objects.using(database), "donated_at")

    for user in User.objects.using(database).all().iterator():
        if user.role == "manager":
            user.role = "DEVICE_MANAGER"
            user.admin_approval_status = "APPROVED"
        elif user.role == "it_admin":
            user.role = "IT_ADMIN"
            user.admin_approval_status = "APPROVED" if user.admin_approved else "PENDING"
        user.save(update_fields=["role", "admin_approval_status"])

    status_map = {
        "registered": "REGISTERED",
        "awaiting_assessment": "REGISTERED",
        "self_assessed": "SELF_ASSESSED",
        "pending_processing": "PENDING_PROCESSING",
        "sanitization": "SELF_ASSESSED",
        "repair": "SELF_ASSESSED",
        "verified": "IT_VERIFIED",
        "sanitized": "SANITIZED",
        "failed": "FAILED",
        "completed": "COMPLETED",
        "donated": "DONATED",
    }
    manufacturer_map = {"Hewlett Packard": "HP", "Hewlett-Packard": "HP", "Hewlett-Packard (HP)": "HP", "hp": "HP", "asus": "ASUS"}
    processor_brands = ("Intel", "AMD", "Apple", "Qualcomm", "MediaTek")
    for device in Device.objects.using(database).all().iterator():
        year = device.year_of_purchase or datetime.date.today().year
        device.purchase_date = datetime.date(max(1900, min(year, datetime.date.today().year)), 1, 1)
        capacity = getattr(device, "storage_gb", None) or 256
        device.storage_info = f"{capacity}GB"
        device.make = manufacturer_map.get(device.make, device.make)
        processor_text = (device.processor or "").strip()
        device.processor = next((brand for brand in processor_brands if processor_text.lower().startswith(brand.lower())), "Intel")
        device.status = status_map.get(device.status, device.status.upper())
        device.save(update_fields=["purchase_date", "storage_info", "make", "processor", "status"])

    pathway_map = {"sanitize": "SANITIZATION", "sanitization": "SANITIZATION", "repair": "REPAIR", "both": "BOTH"}
    for assessment in OwnerSelfAssessment.objects.using(database).all().iterator():
        scores = assessment.scores or {}
        assessment.screen_status = _component_status(scores.get("screen_status", scores.get("screen", scores.get("display", "WORKING"))))
        assessment.keyboard_status = _component_status(scores.get("keyboard_status", scores.get("keyboard", "WORKING")))
        assessment.battery_status = _component_status(scores.get("battery_status", scores.get("battery", scores.get("battery_health", "WORKING"))))
        assessment.chassis_status = _component_status(scores.get("chassis_status", scores.get("chassis", "WORKING")))
        assessment.storage_status = _component_status(scores.get("storage_status", scores.get("storage", "WORKING")))
        assessment.chosen_pathway = pathway_map.get(str(assessment.chosen_pathway).lower(), "SANITIZATION")
        assessment.notes = ""
        assessment.save(update_fields=["screen_status", "keyboard_status", "battery_status", "chassis_status", "storage_status", "chosen_pathway", "notes"])

    for assessment in TechnicalAssessment.objects.using(database).all().iterator():
        assessment.screen_status = _component_status(getattr(assessment, "display", "NOT_ASSESSED"))
        assessment.keyboard_status = _component_status(assessment.keyboard)
        assessment.battery_status = _component_status(getattr(assessment, "battery_health", "NOT_ASSESSED"))
        assessment.chassis_status = "NOT_ASSESSED"
        assessment.storage_status = _component_status(assessment.storage)
        assessment.save(update_fields=["screen_status", "keyboard_status", "battery_status", "chassis_status", "storage_status"])

    for evaluation in RepairEvaluation.objects.using(database).all().iterator():
        old_result = str(evaluation.result).lower()
        evaluation.component_name = "General Hardware"
        evaluation.repair_result = "FIXED" if old_result == "passed" else "NOT_FIXED"
        evaluation.comments = (evaluation.technician_comments or "").strip() or "No technician comments were recorded for this legacy repair."
        evaluation.save(update_fields=["component_name", "repair_result", "comments"])

    for job in SanitizationJob.objects.using(database).all().iterator():
        job.status = "VERIFIED" if job.current_step >= 5 else "PASS" if job.current_step >= 4 else "PENDING"
        job.save(update_fields=["status"])

    for donation in DonationLedger.objects.using(database).all().iterator():
        status = str(donation.payout_status).upper()
        donation.payout_status = status if status in {"PENDING", "APPROVED", "PAID", "REJECTED"} else "PENDING"
        device = Device.objects.using(database).get(pk=donation.device_id)
        years_used = max(0, datetime.date.today().year - device.purchase_date.year)
        value = Decimal(str(device.current_net_valuation or 0))
        if value <= 0:
            value = (device.initial_value_kes * (Decimal("0.85") ** years_used)).quantize(Decimal("0.01"))
        donation.donation_value_kes = value
        donation.save(update_fields=["payout_status", "donation_value_kes"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("laptops", "0001_initial")]

    operations = [
        migrations.RenameField("user", "phone", "phone_number"),
        migrations.AddField("user", "admin_approval_status", models.CharField(default="APPROVED", max_length=20)),
        migrations.RenameField("device", "asset_tag", "device_name"),
        migrations.RenameField("device", "manufacturer", "make"),
        migrations.RenameField("device", "initial_purchase_value", "initial_value_kes"),
        migrations.AddField("device", "purchase_date", models.DateField(null=True)),
        migrations.AddField("device", "storage_info", models.CharField(blank=True, default="", max_length=100)),
        migrations.RenameField("ownerselfassessment", "pathway", "chosen_pathway"),
        migrations.AddField("ownerselfassessment", "screen_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("ownerselfassessment", "keyboard_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("ownerselfassessment", "battery_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("ownerselfassessment", "chassis_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("ownerselfassessment", "storage_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("ownerselfassessment", "notes", models.TextField(blank=True, null=True)),
        migrations.RenameField("technicalassessment", "inspector", "assessed_by"),
        migrations.AddField("technicalassessment", "screen_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("technicalassessment", "keyboard_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("technicalassessment", "battery_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("technicalassessment", "chassis_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="NOT_ASSESSED", max_length=20)),
        migrations.AddField("technicalassessment", "storage_status", models.CharField(choices=COMPONENT_STATUS_CHOICES, default="WORKING", max_length=20)),
        migrations.AddField("repairevaluation", "component_name", models.CharField(default="General Hardware", max_length=50)),
        migrations.AddField("repairevaluation", "repair_result", models.CharField(default="FIXED", max_length=20)),
        migrations.AddField("repairevaluation", "comments", models.TextField(blank=True, default="")),
        migrations.AddField("sanitizationjob", "status", models.CharField(default="PENDING", max_length=30)),
        migrations.RenameField("sanitizationcertificate", "certificate_id", "cert_code"),
        migrations.RenameField("sanitizationcertificate", "sha256_hash", "crypto_hash"),
        migrations.RenameField("sanitizationcertificate", "generated_at", "issued_at"),
        migrations.RenameField("donationledger", "payout_amount", "cashback_kes"),
        migrations.RenameField("donationledger", "status", "payout_status"),
        migrations.RenameField("donationledger", "mpesa_reference", "transaction_ref"),
        migrations.RenameField("donationledger", "created_at", "donated_at"),
        migrations.RenameField("donationledger", "donor", "donated_by"),
        migrations.AddField("donationledger", "donation_value_kes", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
        migrations.RenameField("auditlog", "action", "action_stage"),
        migrations.RenameField("auditlog", "details", "description"),
        migrations.RenameField("auditlog", "created_at", "timestamp"),
        migrations.RunPython(migrate_legacy_data, noop_reverse),
        migrations.RemoveField("user", "admin_approved"),
        migrations.AlterField("user", "role", models.CharField(choices=[("IT_ADMIN", "IT Administrator"), ("DEVICE_MANAGER", "Device Manager")], default="DEVICE_MANAGER", max_length=20)),
        migrations.AlterField("user", "phone_number", models.CharField(blank=True, max_length=15, null=True)),
        migrations.AlterField("user", "admin_approval_status", models.CharField(choices=[("PENDING", "Pending Approval"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")], default="APPROVED", max_length=20)),
        migrations.AlterField("device", "device_name", models.CharField(help_text="Nickname for your device", max_length=100)),
        migrations.AlterField("device", "make", models.CharField(choices=[("Dell", "Dell"), ("HP", "HP (Hewlett-Packard)"), ("Lenovo", "Lenovo"), ("Acer", "Acer"), ("ASUS", "ASUS"), ("Apple", "Apple"), ("Toshiba", "Toshiba")], max_length=50)),
        migrations.AlterField("device", "processor", models.CharField(choices=[("Intel", "Intel"), ("AMD", "AMD"), ("Apple", "Apple"), ("Qualcomm", "Qualcomm"), ("MediaTek", "MediaTek")], max_length=50)),
        migrations.AlterField("device", "ram_gb", models.IntegerField(default=8)),
        migrations.AlterField("device", "storage_info", models.CharField(default="256GB SSD", max_length=100)),
        migrations.AlterField("device", "initial_value_kes", models.DecimalField(decimal_places=2, default=50000.0, max_digits=10)),
        migrations.AlterField("device", "purchase_date", models.DateField(default=datetime.date.today)),
        migrations.AlterField("device", "status", models.CharField(choices=[("REGISTERED", "Registered"), ("SELF_ASSESSED", "Self Assessed"), ("PENDING_PROCESSING", "Pending Processing"), ("IT_VERIFIED", "IT Verified"), ("SANITIZED", "Sanitization Completed"), ("FAILED", "Sanitization Failed"), ("COMPLETED", "Completed"), ("DONATED", "Donated for Parts")], default="REGISTERED", max_length=30)),
        migrations.RemoveField("device", "storage_gb"),
        migrations.RemoveField("device", "screen_inches"),
        migrations.RemoveField("device", "year_of_purchase"),
        migrations.RemoveField("device", "refurbished"),
        migrations.RemoveField("device", "is_public"),
        migrations.RemoveField("device", "current_net_valuation"),
        migrations.AlterField("device", "device_name", models.CharField(help_text="Nickname for your device", max_length=100)),
        migrations.AlterField("ownerselfassessment", "device", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="self_assessment", to="laptops.device")),
        migrations.AlterField("ownerselfassessment", "chosen_pathway", models.CharField(choices=[("SANITIZATION", "Sanitization"), ("REPAIR", "Repair"), ("BOTH", "Repair and sanitization")], default="SANITIZATION", max_length=50)),
        migrations.RemoveField("ownerselfassessment", "owner"),
        migrations.RemoveField("ownerselfassessment", "scores"),
        migrations.RemoveField("ownerselfassessment", "backup_confirmed"),
        migrations.AlterField("technicalassessment", "device", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="technical_assessment", to="laptops.device")),
        migrations.RemoveField("technicalassessment", "boot"),
        migrations.RemoveField("technicalassessment", "keyboard"),
        migrations.RemoveField("technicalassessment", "storage"),
        migrations.RemoveField("technicalassessment", "cpu"),
        migrations.RemoveField("technicalassessment", "bluetooth"),
        migrations.RemoveField("technicalassessment", "usb"),
        migrations.RemoveField("technicalassessment", "display"),
        migrations.RemoveField("technicalassessment", "battery_health"),
        migrations.RemoveField("technicalassessment", "ram"),
        migrations.RemoveField("technicalassessment", "wifi"),
        migrations.RemoveField("technicalassessment", "camera"),
        migrations.RemoveField("technicalassessment", "assessment_score_ratio"),
        migrations.RemoveField("technicalassessment", "notes"),
        migrations.AlterField("repairevaluation", "repair_result", models.CharField(choices=[("FIXED", "Fixed"), ("NOT_FIXED", "Not Fixed")], max_length=20)),
        migrations.AlterField("repairevaluation", "comments", models.TextField(help_text="Mandatory explanation if component is Not Fixed")),
        migrations.RemoveField("repairevaluation", "result"),
        migrations.RemoveField("repairevaluation", "technician_comments"),
        migrations.RemoveField("sanitizationjob", "current_step"),
        migrations.RemoveField("sanitizationjob", "updated_at"),
        migrations.RemoveField("sanitizationjob", "started_by"),
        migrations.AlterField("sanitizationcertificate", "cert_code", models.CharField(default=uuid.uuid4, max_length=50, unique=True)),
        migrations.AlterField("sanitizationcertificate", "crypto_hash", models.CharField(default="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", max_length=64)),
        migrations.AlterField("sanitizationcertificate", "device", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="certificate", to="laptops.device")),
        migrations.AlterField("sanitizationcertificate", "verified_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        migrations.RemoveField("sanitizationcertificate", "verification_code"),
        migrations.RemoveField("sanitizationcertificate", "qr_payload"),
        migrations.AlterField("donationledger", "device", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="donation_ledger", to="laptops.device")),
        migrations.AlterField("donationledger", "donated_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="donation_rewards", to=settings.AUTH_USER_MODEL)),
        migrations.AlterField("donationledger", "donation_value_kes", models.DecimalField(decimal_places=2, max_digits=10)),
        migrations.AlterField("donationledger", "cashback_kes", models.DecimalField(decimal_places=2, max_digits=10)),
        migrations.AlterField("donationledger", "payout_status", models.CharField(choices=[("PENDING", "Pending approval"), ("APPROVED", "Approved"), ("PAID", "Paid"), ("REJECTED", "Rejected")], default="PAID", max_length=20)),
        migrations.AlterField("donationledger", "transaction_ref", models.CharField(default="MPESA-MOCK-TXN", max_length=50)),
        migrations.RemoveField("donationledger", "updated_at"),
        migrations.RemoveField("donationledger", "approved_by"),
        migrations.AlterField("auditlog", "action_stage", models.CharField(default="REGISTERED", max_length=50)),
        migrations.AlterField("auditlog", "actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        migrations.AlterField("auditlog", "device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audit_logs", to="laptops.device")),
        migrations.AddConstraint("device", models.UniqueConstraint(fields=("owner", "device_name"), name="unique_owner_device_nickname")),
    ]
