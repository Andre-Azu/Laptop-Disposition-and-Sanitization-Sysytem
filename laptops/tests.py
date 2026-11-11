import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, RepairEvaluationForm
from .models import AuditLog, CollectionSlot, Device, DonationLedger, ITAssessment, ITAssessmentScoringEngine, OwnerSelfAssessment, RepairEvaluation, SanitizationCertificate, SanitizationJob, TechnicalAssessment, User
from .utils import calculate_device_valuation, format_kes


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
        for name in ("overview", "devices", "assessments", "sanitization", "repairs", "disposition", "profile"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_device_detail_shows_professional_lifecycle_record(self):
        self.device.owner = self.admin
        self.device.status = Device.Status.REPAIR_PROCESSING
        self.device.repair_tag = "in_progress"
        self.device.save(update_fields=["owner", "status", "repair_tag"])
        OwnerSelfAssessment.objects.create(
            device=self.device,
            chosen_pathway="REPAIR",
            screen_status="NOT_WORKING",
            keyboard_status="WORKING",
        )
        ITAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            completed_at=timezone.now(),
            q8_1="GOOD",
        )
        RepairEvaluation.objects.create(
            device=self.device,
            technician=self.admin,
            component_name="Screen",
            repair_result="FIXED",
            comments="Screen replacement completed.",
        )
        AuditLog.objects.create(
            device=self.device,
            actor=self.admin,
            action_stage="Repair execution finalized",
            description="Screen repair was recorded.",
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("device_detail", args=[self.device.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete lifecycle")
        self.assertContains(response, "Repair Processing")
        self.assertContains(response, "Assessment results")
        self.assertContains(response, "Repair record")
        self.assertContains(response, "Screen")
        self.assertContains(response, "Repaired")
        self.assertContains(response, "Complete activity history")

    def test_it_admin_profile_renders_processed_activity(self):
        self.device.owner = self.admin
        self.device.save(update_fields=["owner"])
        ITAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            completed_at=timezone.now(),
        )
        SanitizationCertificate.objects.create(
            device=self.device,
            verified_by=self.admin,
        )
        AuditLog.objects.create(
            device=self.device,
            actor=self.admin,
            action_stage="Repair execution finalized",
            description="Repair finalized by the IT administrator.",
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_admin"])
        self.assertEqual(response.context["assessment_count"], 1)
        self.assertEqual(response.context["sanitization_count"], 1)
        self.assertEqual(response.context["repair_count"], 1)
        self.assertContains(response, "My devices")
        self.assertContains(response, "Devices added by this IT administrator.")
        self.assertContains(response, "Campus laptop")

    def test_devices_page_has_add_device_button_for_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("devices"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "＋ Add device")
        self.assertContains(response, reverse("register_device"))

    def test_device_detail_updates_selected_sanitization_pathway_status(self):
        self.device.owner = self.admin
        self.device.status = Device.Status.SANITIZATION_PROCESSING
        self.device.save(update_fields=["owner", "status"])
        OwnerSelfAssessment.objects.create(
            device=self.device,
            chosen_pathway="SANITIZATION",
        )

        self.client.force_login(self.admin)
        pending_response = self.client.get(reverse("device_detail", args=[self.device.pk]))
        self.assertContains(pending_response, "Sanitization: Not complete")

        SanitizationCertificate.objects.create(
            device=self.device,
            verified_by=self.admin,
            cert_code="PATHWAY-COMPLETE",
        )
        self.device.sanitization_tag = "complete"
        self.device.status = Device.Status.DISPOSITION_PENDING
        self.device.save(update_fields=["sanitization_tag", "status"])

        complete_response = self.client.get(reverse("device_detail", args=[self.device.pk]))
        self.assertContains(complete_response, "Sanitization: Complete")

    def test_device_detail_updates_selected_repair_pathway_status(self):
        self.device.owner = self.admin
        self.device.status = Device.Status.REPAIR_PROCESSING
        self.device.save(update_fields=["owner", "status"])
        OwnerSelfAssessment.objects.create(
            device=self.device,
            chosen_pathway="REPAIR",
        )

        self.client.force_login(self.admin)
        pending_response = self.client.get(reverse("device_detail", args=[self.device.pk]))
        self.assertContains(pending_response, "Repair: Not complete")

        self.device.repair_tag = "complete"
        self.device.status = Device.Status.DISPOSITION_PENDING
        self.device.save(update_fields=["repair_tag", "status"])

        complete_response = self.client.get(reverse("device_detail", args=[self.device.pk]))
        self.assertContains(complete_response, "Repair: Complete")

    def test_audit_trails_and_detail_records_are_newest_first(self):
        self.device.owner = self.admin
        self.device.save(update_fields=["owner"])
        older = AuditLog.objects.create(
            device=self.device,
            actor=self.admin,
            action_stage="Older event",
            description="Created first.",
        )
        newer = AuditLog.objects.create(
            device=self.device,
            actor=self.admin,
            action_stage="Newer event",
            description="Created second.",
        )
        AuditLog.objects.filter(pk=older.pk).update(timestamp=timezone.now() - datetime.timedelta(hours=1))
        AuditLog.objects.filter(pk=newer.pk).update(timestamp=timezone.now())

        older_repair = RepairEvaluation.objects.create(
            device=self.device,
            technician=self.admin,
            component_name="Keyboard",
            repair_result="FIXED",
        )
        newer_repair = RepairEvaluation.objects.create(
            device=self.device,
            technician=self.admin,
            component_name="Screen",
            repair_result="FIXED",
        )
        RepairEvaluation.objects.filter(pk=older_repair.pk).update(created_at=timezone.now() - datetime.timedelta(hours=1))
        RepairEvaluation.objects.filter(pk=newer_repair.pk).update(created_at=timezone.now())

        self.client.force_login(self.admin)
        response = self.client.get(reverse("device_detail", args=[self.device.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["audit_logs"][0].action_stage, "Newer event")
        self.assertEqual(response.context["repairs"][0].component_name, "Screen")

    def test_device_inventory_contact_buttons_are_only_inside_more_info_dialog(self):
        self.manager.email = "owner@example.com"
        self.manager.phone_number = "+254700000000"
        self.manager.save(update_fields=["email", "phone_number"])
        self.client.force_login(self.admin)

        response = self.client.get(reverse("devices"))
        content = response.content.decode()

        self.assertEqual(content.count(">Email owner</a>"), 1)
        self.assertEqual(content.count(">Call owner</a>"), 1)
        self.assertIn('<dialog id="more-info-', content)
        self.assertNotContains(response, "Contact by email")

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

    def test_it_assessment_uses_exactly_twenty_questions_across_all_components(self):
        self.assertEqual(ITAssessmentScoringEngine.QUESTION_COUNT, 20)
        self.assertEqual(len(ITAssessmentScoringEngine.ALL_FIELDS), 20)
        self.assertEqual(
            set(ITAssessmentScoringEngine.MODULES),
            {
                "Motherboard", "CPU / Processor", "RAM / Memory", "Storage HDD / SSD",
                "Power System", "Cooling System", "GPU / Graphics Unit",
                "Display / Screen", "Keyboard", "Touchpad", "Network Adapter",
                "Audio System",
            },
        )

    def test_twenty_question_it_assessment_submission_preserves_scoring(self):
        self.device.status = Device.Status.PENDING_ADMIN_ASSESMENT
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)

        answers = {field: "GOOD" for field in ITAssessmentScoringEngine.ALL_FIELDS}
        response = self.client.post(
            reverse("it_assessment", args=[self.device.pk]),
            {**answers, "notes": "Twenty-question verification complete", "save_assessment": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        assessment = ITAssessment.objects.get(device=self.device)
        self.assertEqual(assessment.overall_score, 100.0)
        self.assertEqual(assessment.answered_count, 20)
        self.assertTrue(assessment.completed_at)

    def test_manager_pages_render_and_admin_pages_are_denied(self):
        self.client.force_login(self.manager)
        for name in ("home", "my_devices", "register_device", "self_assessment", "disposition", "profile"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("overview")).status_code, 403)
        self.assertEqual(self.client.get(reverse("devices")).status_code, 403)

    def test_my_devices_shows_processing_stage_and_owner_next_step(self):
        self.client.force_login(self.manager)

        registered_response = self.client.get(reverse("my_devices"))
        self.assertContains(registered_response, "Owner self-assessment required")
        self.assertContains(
            registered_response,
            reverse("self_assessment_device", args=[self.device.pk]),
        )
        self.assertContains(registered_response, "Complete self-assessment")

        self.device.status = Device.Status.DISPOSITION_PENDING
        self.device.save(update_fields=["status"])
        disposition_response = self.client.get(reverse("my_devices"))
        self.assertContains(disposition_response, "disposition decision required")
        self.assertContains(disposition_response, reverse("disposition"))
        self.assertContains(disposition_response, "Choose next step")

    def test_my_devices_separates_marketplace_listings(self):
        listed_device = self.make_device(device_name="Listed laptop", serial_number="SERIAL-LISTED", is_listed=True)
        self.client.force_login(self.manager)

        response = self.client.get(reverse("my_devices"))

        self.assertEqual(list(response.context["devices"]), [self.device])
        self.assertEqual(list(response.context["listed_devices"]), [listed_device])
        self.assertContains(response, "My marketplace listings")
        self.assertContains(response, "Remove listing")
        self.assertContains(response, reverse("remove_device_listing", args=[listed_device.pk]))

    def test_owner_can_remove_own_marketplace_listing(self):
        self.device.is_listed = True
        self.device.save(update_fields=["is_listed"])
        self.client.force_login(self.manager)

        response = self.client.post(reverse("remove_device_listing", args=[self.device.pk]))

        self.assertRedirects(response, reverse("my_devices"))
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_listed)
        self.assertTrue(AuditLog.objects.filter(
            device=self.device,
            actor=self.manager,
            action_stage="Marketplace listing removed",
        ).exists())

    def test_owner_cannot_remove_another_owners_marketplace_listing(self):
        other_owner = User.objects.create_user(username="another-manager", password="test-password")
        listed_device = self.make_device(owner=other_owner, serial_number="SERIAL-OTHER-LISTED", is_listed=True)
        self.client.force_login(self.manager)

        response = self.client.post(reverse("remove_device_listing", args=[listed_device.pk]))

        self.assertEqual(response.status_code, 404)
        listed_device.refresh_from_db()
        self.assertTrue(listed_device.is_listed)

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

    def test_owner_can_choose_keep_or_dispose_only_when_disposition_pending(self):
        self.device.status = Device.Status.DISPOSITION_PENDING
        self.device.save(update_fields=["status"])
        self.client.force_login(self.manager)

        response = self.client.get(reverse("disposition"))

        self.assertIn(self.device, response.context["devices"])
        self.assertContains(response, "Keep and schedule collection")
        self.assertContains(response, "Dispose device")

    def test_owner_keep_choice_schedules_collection_and_removes_disposition_decision(self):
        self.device.status = Device.Status.DISPOSITION_PENDING
        self.device.save(update_fields=["status"])
        scheduled_for = timezone.now() + datetime.timedelta(days=7)
        self.client.force_login(self.manager)

        response = self.client.post(reverse("disposition"), {
            "device": self.device.pk,
            "action": "schedule",
            "scheduled_for": scheduled_for.isoformat(),
            "location": "Strathmore University",
        })

        self.assertRedirects(response, reverse("disposition"))
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.SCHEDULED_FOR_COLLECTION)
        self.assertTrue(CollectionSlot.objects.filter(device=self.device, requested_by=self.manager).exists())

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
        self.assertContains(response, "More info")
        self.assertContains(response, "Owner contact")
        self.assertContains(response, "Component ratings")
        self.assertContains(response, "Final decision")

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

    def test_fixed_keyboard_is_working_in_inventory_filter(self):
        TechnicalAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            keyboard_status="NOT_WORKING",
        )
        RepairEvaluation.objects.create(
            device=self.device,
            technician=self.admin,
            component_name="Keyboard",
            repair_result="FIXED",
        )
        not_fixed = self.make_device(device_name="Keyboard still broken")
        TechnicalAssessment.objects.create(
            device=not_fixed,
            assessed_by=self.admin,
            keyboard_status="NOT_WORKING",
        )
        RepairEvaluation.objects.create(
            device=not_fixed,
            technician=self.admin,
            component_name="Keyboard",
            repair_result="NOT_FIXED",
        )

        self.client.force_login(self.admin)
        working = self.client.get(
            reverse("devices"),
            {"component": "keyboard", "condition": "WORKING"},
        )
        not_working = self.client.get(
            reverse("devices"),
            {"component": "keyboard", "condition": "NOT_WORKING"},
        )
        self.assertEqual(list(working.context["devices"]), [self.device])
        self.assertEqual(list(not_working.context["devices"]), [not_fixed])

    def test_search_is_scoped_to_the_current_page(self):
        matching = self.make_device(device_name="Page matching laptop")
        other = self.make_device(device_name="Other laptop")
        self.client.force_login(self.admin)

        response = self.client.get(reverse("devices"), {"q": "Page matching"})
        self.assertEqual(list(response.context["devices"]), [matching])
        self.assertNotIn(other, response.context["devices"])

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

    def test_both_pathway_is_available_to_sanitization_and_repair_workstations(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="BOTH")
        self.device.status = Device.Status.REPAIR_AND_SANITIZATION_PENDING
        self.device.save(update_fields=["status"])

        self.client.force_login(self.admin)
        sanitization_response = self.client.get(reverse("sanitization_execution", args=[self.device.pk]))
        repair_response = self.client.get(reverse("repair_execution", args=[self.device.pk]))
        overview_response = self.client.get(reverse("overview"), {"status": "awaiting_sanitization"})

        self.assertEqual(sanitization_response.status_code, 200)
        self.assertEqual(repair_response.status_code, 200)
        self.assertIn(self.device, overview_response.context["devices"])

    def test_completed_assessment_unlocks_repair_queue_despite_stale_status(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="REPAIR")
        ITAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            completed_at=datetime.datetime.now(datetime.timezone.utc),
        )
        self.device.status = Device.Status.PENDING_ADMIN_ASSESMENT
        self.device.save(update_fields=["status"])

        self.client.force_login(self.admin)
        queue_response = self.client.get(reverse("repairs"))
        execution_response = self.client.get(reverse("repair_execution", args=[self.device.pk]))

        self.assertEqual(queue_response.status_code, 200)
        self.assertContains(queue_response, "Start Repair")
        self.assertNotContains(queue_response, "Cannot be repaired before assessment")
        self.assertEqual(execution_response.status_code, 200)

    def test_sanitization_media_type_uses_supported_dropdown_options(self):
        self.device.status = Device.Status.SANITIZATION_PROCESSING
        self.device.save(update_fields=["status"])
        self.client.force_login(self.admin)

        response = self.client.get(reverse("sanitization_execution", args=[self.device.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="NVMe SSD">NVMe SSD</option>', html=True)
        self.assertContains(response, '<option value="Unknown">Unknown</option>', html=True)

    def test_device_manager_sees_sanitization_complete_and_can_view_certificate(self):
        certificate = SanitizationCertificate.objects.create(
            device=self.device,
            verified_by=self.admin,
            cert_code="TEST-SAN-CERT",
            crypto_hash="a" * 64,
        )
        self.device.status = Device.Status.SCHEDULED_FOR_COLLECTION
        self.device.save(update_fields=["status"])

        self.client.force_login(self.manager)
        my_devices_response = self.client.get(reverse("my_devices"))
        certificate_response = self.client.get(reverse("sanitization_certificate", args=[certificate.pk]))

        self.assertContains(my_devices_response, "Collection scheduled")
        self.assertNotContains(my_devices_response, "View Sanitization Certificate")
        self.assertEqual(certificate_response.status_code, 200)
        self.assertContains(certificate_response, ">Exit</a>", html=False)

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
        self.assertEqual(self.device.status, Device.Status.DISPOSITION_PENDING)

    def test_sanitization_tiles_filter_devices_and_default_to_pending(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.client.force_login(self.admin)

        response = self.client.get(reverse("sanitization"))

        self.assertEqual(response.context["sanitization_filter"], "pending")
        self.assertContains(response, "Pending Sanitization")
        self.assertContains(response, "Pending sanitization")
        self.assertEqual(response.context["pending_count"], 1)
        self.assertEqual(list(response.context["devices"]), [self.device])

    def test_sanitization_completed_filter_orders_certificates_newest_first(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        second_device = self.make_device(serial_number="SERIAL-NEW", device_name="Newer laptop")
        OwnerSelfAssessment.objects.create(device=second_device, chosen_pathway="SANITIZATION")
        first_certificate = SanitizationCertificate.objects.create(device=self.device, verified_by=self.admin)
        second_certificate = SanitizationCertificate.objects.create(device=second_device, verified_by=self.admin)
        now = timezone.now()
        SanitizationCertificate.objects.filter(pk=first_certificate.pk).update(issued_at=now - datetime.timedelta(days=1))
        SanitizationCertificate.objects.filter(pk=second_certificate.pk).update(issued_at=now)
        self.client.force_login(self.admin)

        response = self.client.get(reverse("sanitization"), {"sanitization_filter": "completed"})

        certificates = list(response.context["sanitized_certificates"])
        self.assertEqual(certificates[0].device, second_device)
        self.assertEqual(certificates[1].device, self.device)
        self.assertContains(response, "Completed Sanitizations")
        self.assertEqual(response.context["completed_count"], 2)

    def test_sanitization_failed_filter_shows_failed_devices(self):
        OwnerSelfAssessment.objects.create(device=self.device, chosen_pathway="SANITIZATION")
        self.device.status = Device.Status.SANITIZATION_FAILED
        self.device.failure_notes = "Storage media could not be overwritten"
        self.device.save(update_fields=["status", "failure_notes"])
        self.client.force_login(self.admin)

        response = self.client.get(reverse("sanitization"), {"sanitization_filter": "failed"})

        self.assertContains(response, "Failed Sanitizations")
        self.assertContains(response, "Storage media could not be overwritten")
        self.assertEqual(list(response.context["failed_devices"]), [self.device])
        self.assertEqual(response.context["failed_count"], 1)

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
        self.assertEqual(self.device.status, Device.Status.DISPOSITION_PENDING)

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

    def test_overview_orders_devices_by_most_recent_audit_activity(self):
        older_activity = self.make_device(device_name="Older activity laptop")
        newer_activity = self.make_device(device_name="Newer activity laptop")
        old_audit = AuditLog.objects.create(
            device=older_activity,
            actor=self.admin,
            action_stage="Assessment",
            description="Older audit event",
        )
        new_audit = AuditLog.objects.create(
            device=newer_activity,
            actor=self.admin,
            action_stage="Assessment",
            description="Newer audit event",
        )
        now = timezone.now()
        AuditLog.objects.filter(pk=old_audit.pk).update(timestamp=now - datetime.timedelta(days=2))
        AuditLog.objects.filter(pk=new_audit.pk).update(timestamp=now - datetime.timedelta(hours=2))

        self.client.force_login(self.admin)
        response = self.client.get(reverse("overview"))

        device_ids = [device.pk for device in response.context["devices"]]
        self.assertLess(device_ids.index(newer_activity.pk), device_ids.index(older_activity.pk))

    def test_overview_disbursement_view_info_marks_paid(self):
        ledger = DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("100000"),
            cashback_kes=Decimal("3000"),
            payout_status=DonationLedger.Status.APPROVED,
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("overview") + "?status=awaiting_disbursement")
        self.assertContains(response, "View info")
        self.assertContains(response, "Marked paid")
        self.assertContains(response, "Repair &amp; sanitization")

        response = self.client.post(reverse("overview"), {
            "action": "mark_disbursement_paid",
            "entry": ledger.pk,
        })
        self.assertEqual(response.status_code, 302)
        ledger.refresh_from_db()
        self.assertEqual(ledger.payout_status, DonationLedger.Status.PAID)
        self.assertTrue(AuditLog.objects.filter(device=self.device, action_stage="Disbursement paid").exists())

    def test_weighted_valuation_cap_and_cashback(self):
        RepairEvaluation.objects.create(
            device=self.device,
            technician=self.admin,
            component_name="Motherboard",
            repair_result="FIXED",
            comments="Restored",
        )
        valuation = calculate_device_valuation(self.device)
        self.assertEqual(valuation["acquired_value"], Decimal("100000.00"))
        self.assertEqual(valuation["current_value"], Decimal("75000.00"))
        self.assertEqual(valuation["cashback_amount"], Decimal("2250.00"))

    def test_disposition_approval_sets_device_flag_and_audit(self):
        ledger = DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("75000"),
            cashback_kes=Decimal("2250"),
            payout_status=DonationLedger.Status.PENDING,
        )
        self.client.force_login(self.admin)
        response = self.client.post(reverse("disposition"), {
            "action": "approve_donation",
            "entry": ledger.pk,
        })
        self.assertEqual(response.status_code, 302)
        ledger.refresh_from_db()
        self.device.refresh_from_db()
        self.assertEqual(ledger.payout_status, DonationLedger.Status.APPROVED)
        self.assertTrue(self.device.disbursement_approved)
        self.assertTrue(AuditLog.objects.filter(device=self.device, action_stage="Donation APPROVED").exists())

    def test_disposition_rejection_saves_required_reason(self):
        ledger = DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("75000"),
            cashback_kes=Decimal("2250"),
            payout_status=DonationLedger.Status.PENDING,
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("disposition"), {
            "action": "reject_donation",
            "entry": ledger.pk,
            "rejection_reason": "Valuation requires review",
        })
        ledger.refresh_from_db()
        self.assertEqual(ledger.payout_status, DonationLedger.Status.REJECTED)
        self.assertEqual(ledger.rejection_reason, "Valuation requires review")

    def test_disposition_rejection_requires_reason(self):
        ledger = DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("75000"),
            cashback_kes=Decimal("2250"),
            payout_status=DonationLedger.Status.PENDING,
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("disposition"), {
            "action": "reject_donation",
            "entry": ledger.pk,
            "rejection_reason": "",
        })
        ledger.refresh_from_db()
        self.assertEqual(ledger.payout_status, DonationLedger.Status.PENDING)
        self.assertEqual(ledger.rejection_reason, "")

    def test_device_manager_sees_rejected_disbursement(self):
        ledger = DonationLedger.objects.create(
            device=self.device,
            donated_by=self.manager,
            donation_value_kes=Decimal("75000"),
            cashback_kes=Decimal("2250"),
            payout_status=DonationLedger.Status.REJECTED,
            rejection_reason="Valuation requires review",
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("disposition"))
        self.assertContains(response, "Disbursement not accepted")
        self.assertContains(response, "Valuation requires review")

    def test_device_manager_accepts_offer_marks_donated_and_removes_collection(self):
        ITAssessment.objects.create(
            device=self.device,
            assessed_by=self.admin,
            completed_at=datetime.datetime.now(datetime.timezone.utc),
            notes="Verified",
        )
        self.device.status = Device.Status.IT_VERIFIED
        self.device.save(update_fields=["status"])
        CollectionSlot.objects.create(
            device=self.device,
            requested_by=self.manager,
            scheduled_for=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
            location="Strathmore University",
        )

        self.client.force_login(self.manager)
        response = self.client.post(reverse("user_assessments_tab"), {
            "action": "accept_cashback_offer",
            "device": self.device.pk,
            "donation_terms": "accepted",
        })

        self.assertEqual(response.status_code, 302)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, Device.Status.DONATED)
        self.assertTrue(DonationLedger.objects.filter(
            device=self.device,
            donated_by=self.manager,
            payout_status=DonationLedger.Status.PENDING,
        ).exists())
        self.assertFalse(CollectionSlot.objects.filter(device=self.device).exists())
