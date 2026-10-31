import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, RepairEvaluationForm
from .models import Device, DonationLedger, ITAssessment, OwnerSelfAssessment, RepairEvaluation, SanitizationCertificate, SanitizationJob, TechnicalAssessment, User


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
            "purchase_date": datetime.date.today(),
            "acquisition_period": "2024-03",
        }
        values.update(overrides)
        return Device.objects.create(**values)

    def test_admin_pages_render_for_approved_admin(self):
        self.client.force_login(self.admin)
        for name in ("overview", "devices", "assessments", "sanitization", "repairs", "disposition", "profile", "certificates"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_assessment_portal_cards_filter_pending_incomplete_completed_devices(self):
        incomplete = self.make_device(device_name="Incomplete laptop")
        completed = self.make_device(device_name="Completed laptop")

        self.device.status = Device.Status.PENDING_PROCESSING
        self.device.save(update_fields=["status"])
        incomplete.status = Device.Status.IT_IN_PROGRESS
        incomplete.save(update_fields=["status"])
        completed.status = Device.Status.IT_VERIFIED
        completed.save(update_fields=["status"])
        ITAssessment.objects.create(device=completed, assessed_by=self.admin, completed_at=datetime.datetime.now(datetime.timezone.utc), notes="Verified")

        self.client.force_login(self.admin)
        response = self.client.get(reverse("assessments"))

        categories = {item["device"].pk: item["category"] for item in response.context["portal_devices"]}
        self.assertEqual(categories[self.device.pk], "pending")
        self.assertEqual(categories[incomplete.pk], "incomplete")
        self.assertEqual(categories[completed.pk], "completed")
        self.assertContains(response, 'data-filter="pending"')
        self.assertContains(response, 'data-filter="incomplete"')
        self.assertContains(response, "View Detailed Report")

    def test_completed_assessment_report_button_has_working_detail_route(self):
        ITAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            completed_at=datetime.datetime.now(datetime.timezone.utc),
            notes="Verified report",
        )
        self.device.status = Device.Status.IT_VERIFIED
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)

        portal_response = self.client.get(reverse("assessments"))
        self.assertContains(portal_response, reverse("assessment_report", args=[self.device.pk]))

        report_response = self.client.get(reverse("assessment_report", args=[self.device.pk]))
        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, "Detailed report")
        self.assertContains(report_response, "Verified report")

    def test_it_assessment_rejects_devices_that_are_no_longer_active(self):
        self.device.status = Device.Status.FAILED
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)

        response = self.client.get(reverse("it_assessment", args=[self.device.pk]))

        self.assertRedirects(response, reverse("it_assessments_portal"))

    def test_it_assessment_handles_deleted_or_outdated_device_ids(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("it_assessment", args=[999999]))

        self.assertRedirects(response, reverse("it_assessments_portal"))

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
            "acquisition_month": "03",
            "acquisition_year": "2024",
        }
        form = DeviceRegistrationForm(data=form_data, owner=self.manager)
        self.assertFalse(form.is_valid())
        self.assertIn("device_name", form.errors)
        self.assertIn("serial_number", form.errors)

    def test_current_valuation_handles_missing_purchase_date(self):
        self.device.purchase_date = None

        valuation = self.device.current_net_valuation
        expected_age = max(0, datetime.date.today().year - 2024)
        expected = round(max(5000.0, float(self.device.initial_value_kes) * ((1 - 0.15) ** expected_age)), 2)
        self.assertEqual(valuation, expected)

    def test_acquisition_period_form_accepts_month_and_year(self):
        form = DeviceRegistrationForm(data={
            "device_name": "Work laptop",
            "make": "Dell",
            "model_name": "Latitude",
            "serial_number": "SERIAL-MONTH-YEAR",
            "processor": "Intel",
            "ram_gb": 8,
            "storage_info": "256GB SSD",
            "initial_value_kes": "50000.00",
            "acquisition_month": "03",
            "acquisition_year": "2024",
        }, owner=self.manager)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["acquisition_period"], "2024-03")

    def test_donated_device_is_hidden_from_manager_disposition_choices(self):
        DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("100000"),
            cashback_kes=Decimal("3000"),
            payout_status=DonationLedger.Status.PENDING,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("disposition"))
        self.assertNotIn(self.device, response.context["devices"])

    def test_assessment_lookup_excludes_it_verified_and_donated_devices(self):
        self.device.status = Device.Status.PENDING_PROCESSING
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)
        response = self.client.get(reverse("assessments"), {"device": self.device.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.device, response.context["devices"])
        for status in (Device.Status.IT_VERIFIED, Device.Status.DONATED, Device.Status.SANITIZED, Device.Status.COMPLETED):
            with self.subTest(status=status):
                self.device.status = status
                self.device.save(update_fields=["status"])
                response = self.client.get(reverse("assessments"))
                self.assertNotIn(self.device, response.context["devices"])
                response = self.client.get(reverse("assessments"), {"device": self.device.pk})
                self.assertRedirects(response, reverse("assessments"))

    def test_inventory_details_include_manager_assessment(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="REPAIR", screen_status="PARTIALLY_WORKING")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("devices"))
        self.assertContains(response, "View details")
        self.assertContains(response, "Device manager physical assessment")
        self.assertContains(response, "Partially Working")

    def test_device_inventory_filters_by_selected_component_condition(self):
        partially_working = self.make_device(device_name="Partially working laptop")
        not_working = self.make_device(device_name="Not working laptop")
        TechnicalAssessment.objects.create(device=self.device, assessed_by=self.admin, screen_status="WORKING")
        TechnicalAssessment.objects.create(device=partially_working, assessed_by=self.admin, screen_status="PARTIALLY_WORKING")
        TechnicalAssessment.objects.create(device=not_working, assessed_by=self.admin, screen_status="NOT_WORKING")
        self.client.force_login(self.admin)

        for condition, expected_device in (
            ("WORKING", self.device),
            ("PARTIALLY_WORKING", partially_working),
            ("NOT_WORKING", not_working),
        ):
            with self.subTest(condition=condition):
                response = self.client.get(reverse("devices"), {"component": "screen", "condition": condition})
                self.assertEqual(list(response.context["devices"]), [expected_device])

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
        fixed = RepairEvaluationForm(data={"component_name": "Screen", "repair_result": "FIXED", "comments": "   "})
        self.assertTrue(fixed.is_valid(), fixed.errors)

    def test_repair_evaluation_removes_device_from_queue_and_allows_blank_fixed_comment(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="REPAIR", screen_status="PARTIALLY_WORKING")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("repairs"), {
            "device": self.device.pk,
            "repair_result": "FIXED",
            "comments": "",
        })
        self.assertRedirects(response, reverse("repairs"))
        self.assertEqual(RepairEvaluation.objects.get(device=self.device).comments, "")
        self.assertEqual(RepairEvaluation.objects.get(device=self.device).component_name, "Screen")
        response = self.client.get(reverse("repairs"))
        self.assertNotIn(self.device, response.context["devices"])

    def test_repair_queue_only_offers_components_flagged_in_technical_assessment(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="REPAIR")
        TechnicalAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            screen_status="PARTIALLY_WORKING",
            keyboard_status="WORKING",
            battery_status="MISSING",
            chassis_status="NOT_WORKING",
            storage_status="NOT_ASSESSED",
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("repairs"))
        self.assertContains(response, "Screen · Partially Working")
        self.assertContains(response, "Battery · Missing")
        self.assertContains(response, "Chassis · Not Working")
        self.assertNotContains(response, "Keyboard")
        self.assertNotContains(response, "Storage ·")
        self.assertEqual(response.context["devices"][0].repairable_components, [
            ("Screen", "Partially Working"),
            ("Battery", "Missing"),
            ("Chassis", "Not Working"),
        ])

    def test_repair_evaluation_marks_every_flagged_component(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="REPAIR")
        TechnicalAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            screen_status="PARTIALLY_WORKING",
            battery_status="MISSING",
        )
        self.client.force_login(self.admin)
        response = self.client.post(reverse("repairs"), {
            "device": self.device.pk,
            "repair_result": "NOT_FIXED",
            "comments": "Parts unavailable",
        })
        self.assertRedirects(response, reverse("repairs"))
        self.assertEqual(
            set(RepairEvaluation.objects.filter(device=self.device).values_list("component_name", flat=True)),
            {"Screen", "Battery"},
        )

    def test_repair_queue_includes_devices_sent_by_manager_without_it_assessment(self):
        OwnerSelfAssessment.objects.create(
            device=self.device,
            chosen_pathway="REPAIR",
            battery_status="PARTIALLY_WORKING",
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("repairs"))
        self.assertIn(self.device, response.context["devices"])
        self.assertContains(response, "Battery · Partially Working")

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

    def test_sanitization_verification_reuses_existing_certificate(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.client.force_login(self.admin)
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "pass"})
        certificate = SanitizationCertificate.objects.create(device=self.device, verified_by=self.admin)
        response = self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "verify"})
        self.assertRedirects(response, reverse("sanitization"))
        self.assertEqual(SanitizationCertificate.objects.filter(device=self.device).count(), 1)
        self.assertEqual(SanitizationCertificate.objects.get(device=self.device).pk, certificate.pk)
        self.assertEqual(SanitizationJob.objects.get(device=self.device).status, "VERIFIED")
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.SANITIZED)

    def test_overview_keeps_sanitization_summary_without_it_verify_summary(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("overview"))
        self.assertContains(response, "SANITIZATION COMPLETED")
        self.assertNotContains(response, "IT VERIFIED")

    def test_mock_sanitization_fail_records_failure_note(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.client.force_login(self.admin)
        self.client.post(reverse("sanitization"), {"device": self.device.pk, "action": "fail", "failure_notes": "Storage check failed"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.SANITIZATION_FAILED)
        self.assertEqual(self.device.failure_notes, "Storage check failed")
        self.assertEqual(SanitizationJob.objects.get(device=self.device).status, SanitizationJob.FAIL)

    def test_ewaste_metric_is_donation_value_not_cashback(self):
        DonationLedger.objects.create(device=self.device, donated_by=self.manager, donation_value_kes=Decimal("100000"), cashback_kes=Decimal("3000"), payout_status=DonationLedger.Status.PAID)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("overview"))
        self.assertEqual(response.context["ewaste_recovered"], Decimal("100000"))
