from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, RepairEvaluationForm
from .models import Device, DonationLedger, OwnerSelfAssessment, RepairEvaluation, SanitizationCertificate, SanitizationJob, TechnicalAssessment, User


class WorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="it-admin", password="test-pass", role="IT_ADMIN", admin_approval_status="APPROVED")
        self.manager = User.objects.create_user(username="device-manager", password="test-pass", first_name="Device")
        self.device = self.make_device()

    def make_device(self, **overrides):
        values = {
            "owner": self.manager,
            "device_name": "Campus laptop",
            "make": "Lenovo",
            "model_name": "ThinkPad",
            "serial_number": f"SERIAL-{Device.objects.count() + 1}",
            "processor": "Intel",
            "ram_gb": 16,
            "storage_info": "512GB SSD",
            "initial_value_kes": Decimal("100000.00"),
            "purchase_date": date.today(),
        }
        values.update(overrides)
        return Device.objects.create(**values)

    def test_admin_pages_render_for_approved_admin(self):
        self.client.force_login(self.admin)
        for name in ("overview", "devices", "assessments", "sanitization", "repairs", "disposition", "profile", "certificates"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_manager_pages_render_and_admin_pages_are_denied(self):
        self.client.force_login(self.manager)
        for name in ("home", "my_devices", "register_device", "self_assessment", "disposition", "profile"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("overview")).status_code, 403)
        self.assertEqual(self.client.get(reverse("devices")).status_code, 403)

    def test_device_name_is_unique_per_owner_and_serial_is_global(self):
        other_owner = User.objects.create_user(username="another-manager", password="test-pass")
        duplicate_nickname = self.make_device(owner=other_owner, serial_number="SERIAL-OTHER")
        self.assertEqual(duplicate_nickname.device_name, self.device.device_name)
        form_data = {
            "device_name": "Campus laptop",
            "make": "Dell",
            "model_name": "Latitude",
            "serial_number": self.device.serial_number,
            "processor": "Intel",
            "ram_gb": 8,
            "storage_info": "256GB SSD",
            "initial_value_kes": "50000.00",
            "purchase_date": date.today().strftime("%Y-%m"),
        }
        form = DeviceRegistrationForm(data=form_data, owner=self.manager)
        self.assertFalse(form.is_valid())
        self.assertIn("device_name", form.errors)
        self.assertIn("serial_number", form.errors)

    def test_assessment_lookup_allows_active_device_and_rejects_completed(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("assessments"), {"device": self.device.pk}).status_code, 200)
        self.device.status = Device.Status.SANITIZED
        self.device.save(update_fields=["status"])
        response = self.client.get(reverse("assessments"), {"device": self.device.pk})
        self.assertRedirects(response, reverse("assessments"))

    def test_radio_self_assessment_and_repair_comment_validation(self):
        form = OwnerSelfAssessmentForm(data={
            "device": self.device.pk,
            "screen_status": "PARTIALLY_WORKING",
            "keyboard_status": "WORKING",
            "battery_status": "NOT_ASSESSED",
            "chassis_status": "WORKING",
            "storage_status": "WORKING",
            "chosen_pathway": "REPAIR",
            "backup_confirmed": "on",
        }, user=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["screen_status"], "PARTIALLY_WORKING")
        repair = RepairEvaluationForm(data={"component_name": "Screen", "repair_result": "NOT_FIXED", "comments": "   "})
        self.assertFalse(repair.is_valid())
        self.assertIn("comments", repair.errors)

    def test_mock_sanitization_pass_verifies_and_issues_certificate(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.device.status = Device.Status.SELF_ASSESSED
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "pass"})
        job = SanitizationJob.objects.get(device=self.device)
        self.assertEqual(job.status, SanitizationJob.PASS)
        self.assertFalse(SanitizationCertificate.objects.filter(device=self.device).exists())
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "verify"})
        certificate = SanitizationCertificate.objects.get(device=self.device)
        self.assertEqual(len(certificate.crypto_hash), 64)
        self.assertEqual(self.client.get(reverse("certificate_qr", args=[certificate.pk])).status_code, 200)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.SANITIZED)

    def test_mock_sanitization_fail_records_failure_note(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.client.force_login(self.admin)
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "fail", "failure_notes": "Storage check failed"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.FAILED)
        self.assertEqual(self.device.failure_notes, "Storage check failed")
        self.assertEqual(SanitizationJob.objects.get(device=self.device).status, SanitizationJob.FAIL)

    def test_ewaste_metric_is_donation_value_not_cashback(self):
        DonationLedger.objects.create(device=self.device, donated_by=self.manager, donation_value_kes=Decimal("100000"), cashback_kes=Decimal("3000"), payout_status=DonationLedger.Status.PAID)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("overview"))
        self.assertEqual(response.context["ewaste_recovered"], Decimal("100000"))
