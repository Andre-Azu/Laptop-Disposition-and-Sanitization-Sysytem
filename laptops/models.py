import hashlib
import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        DEVICE_MANAGER = "manager", "Device Manager"
        IT_ADMIN = "it_admin", "IT Administrator"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.DEVICE_MANAGER)
    phone = models.CharField(max_length=30, blank=True)
    admin_approved = models.BooleanField(default=False)

    def is_it_admin(self):
        return self.is_superuser or (self.role == self.Role.IT_ADMIN and self.admin_approved)


class Device(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"
        AWAITING_ASSESSMENT = "awaiting_assessment", "Awaiting assessment"
        PENDING_PROCESSING = "pending_processing", "Pending processing"
        SANITIZATION = "sanitization", "Sanitization queued"
        REPAIR = "repair", "Repair queued"
        VERIFIED = "verified", "IT verified"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed sanitization"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    asset_tag = models.CharField(max_length=40, unique=True)
    manufacturer = models.CharField(max_length=80)
    model_name = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=100, unique=True)
    processor = models.CharField(max_length=120, blank=True)
    ram_gb = models.PositiveSmallIntegerField(default=8)
    storage_gb = models.PositiveIntegerField(default=256)
    screen_inches = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    initial_purchase_value = models.DecimalField(max_digits=12, decimal_places=2)
    year_of_purchase = models.PositiveSmallIntegerField()
    refurbished = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.REGISTERED)
    current_net_valuation = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.manufacturer} {self.model_name} ({self.asset_tag})"

    def update_valuation(self, assessment_score_ratio=Decimal("0")):
        years_used = max(0, timezone.now().year - self.year_of_purchase)
        condition_factor = Decimal(str(assessment_score_ratio))
        depreciation_factor = max(Decimal("0.10"), Decimal("1.00") - Decimal("0.15") * years_used)
        self.current_net_valuation = (self.initial_purchase_value * depreciation_factor * condition_factor).quantize(Decimal("0.01"))
        self.save(update_fields=["current_net_valuation", "updated_at"])
        return self.current_net_valuation


class TechnicalAssessment(models.Model):
    class Result(models.TextChoices):
        PASSED = "passed", "Passed"
        WARNING = "warning", "Warning"
        FAILED = "failed", "Failed"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="assessments")
    inspector = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="technical_assessments")
    boot = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    keyboard = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    storage = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    cpu = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    bluetooth = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    usb = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    display = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    battery_health = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    ram = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    wifi = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    camera = models.CharField(max_length=10, choices=Result.choices, default=Result.PASSED)
    assessment_score_ratio = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("1.0000"))
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    COMPONENTS = ("boot", "keyboard", "storage", "cpu", "bluetooth", "usb", "display", "battery_health", "ram", "wifi", "camera")

    def save(self, *args, **kwargs):
        scores = {self.Result.PASSED: Decimal("1"), self.Result.WARNING: Decimal("0.5"), self.Result.FAILED: Decimal("0")}
        self.assessment_score_ratio = sum((scores[getattr(self, name)] for name in self.COMPONENTS), Decimal("0")) / Decimal(len(self.COMPONENTS))
        super().save(*args, **kwargs)
        self.device.update_valuation(self.assessment_score_ratio)


class OwnerSelfAssessment(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="self_assessments")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="self_assessments")
    pathway = models.CharField(max_length=12, choices=(("sanitize", "Sanitization"), ("repair", "Repair"), ("both", "Both")))
    scores = models.JSONField(default=dict)
    backup_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class AuditLog(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="device_audit_logs")
    action = models.CharField(max_length=120)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} - {self.device.asset_tag}"


class SanitizationCertificate(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="certificates")
    certificate_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    sha256_hash = models.CharField(max_length=64, unique=True)
    verification_code = models.CharField(max_length=24, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="verified_certificates")
    qr_payload = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.sha256_hash:
            payload = f"{self.device.asset_tag}:{self.certificate_id}:{timezone.now().isoformat()}"
            self.sha256_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not self.verification_code:
            self.verification_code = self.sha256_hash[:16].upper()
        if not self.qr_payload:
            self.qr_payload = f"Certificate {self.certificate_id} | SHA-256 {self.sha256_hash}"
        super().save(*args, **kwargs)


class SanitizationJob(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="sanitization_jobs")
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="sanitization_jobs")
    current_step = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(5)])
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class RepairEvaluation(models.Model):
    class Result(models.TextChoices):
        PASSED = "passed", "Passed"
        WARNING = "warning", "Warning"
        FAILED = "failed", "Failed"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="repair_evaluations")
    technician = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="repair_evaluations")
    result = models.CharField(max_length=10, choices=Result.choices, default=Result.WARNING)
    technician_comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CollectionSlot(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="collection_slots")
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="collection_slots")
    scheduled_for = models.DateTimeField()
    location = models.CharField(max_length=160, default="Strathmore University")
    approved = models.BooleanField(default=False)
    collected = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class DonationLedger(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="donation_entries")
    donor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="donation_rewards")
    payout_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    mpesa_reference = models.CharField(max_length=40, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_donations")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)