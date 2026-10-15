from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import RepairEvaluationForm
from .models import Device, SanitizationCertificate, SanitizationJob, TechnicalAssessment, User


class WorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="it-admin", password="test-pass", role=User.Role.IT_ADMIN, admin_approved=True)
        self.manager = User.objects.create_user(username="device-manager", password="test-pass", first_name="Device", role=User.Role.DEVICE_MANAGER)
        self.device = Device.objects.create(
            owner=self.manager,
            asset_tag="STR-LT-1001",
            manufacturer="Lenovo",
            model_name="ThinkPad T14",
            serial_number="SERIAL-1001",
            processor="Intel Core i5",
            ram_gb=16,
            storage_gb=512,
            initial_purchase_value=Decimal("100000.00"),
            year_of_purchase=timezone.now().year,
            current_net_valuation=Decimal("100000.00"),
        )

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

    def test_assessment_updates_ratio_and_valuation(self):
        assessment = TechnicalAssessment.objects.create(
            device=self.device,
            inspector=self.admin,
            **{component: TechnicalAssessment.Result.PASSED for component in TechnicalAssessment.COMPONENTS},
        )
        self.device.refresh_from_db()
        self.assertEqual(assessment.assessment_score_ratio, Decimal("1.0000"))
        self.assertEqual(self.device.current_net_valuation, Decimal("100000.00"))

    def test_failed_repair_requires_technician_comments(self):
        form = RepairEvaluationForm(data={"result": "failed", "technician_comments": "   "})
        self.assertFalse(form.is_valid())
        self.assertIn("technician_comments", form.errors)

    def test_mock_sanitization_requires_verification_and_issues_certificate(self):
        self.device.status = Device.Status.SANITIZATION
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)
        response = self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "mock"})
        self.assertRedirects(response, reverse("sanitization"))
        job = SanitizationJob.objects.get(device=self.device)
        self.assertEqual(job.current_step, 4)
        self.assertFalse(SanitizationCertificate.objects.filter(device=self.device).exists())
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "verify"})
        certificate = SanitizationCertificate.objects.get(device=self.device)
        self.assertEqual(len(certificate.sha256_hash), 64)
        self.assertEqual(self.client.get(reverse("certificate_qr", args=[certificate.pk])).status_code, 200)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.VERIFIED)
