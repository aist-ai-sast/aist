from __future__ import annotations

from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.api.common import compute_risk_score
from aist.models import AISTFindingAnnotation, AISTProject
from aist.tasks.regression import detect_regressions_for_pipeline


class RiskScoreUnitTests(TestCase):

    """Unit tests for compute_risk_score()."""

    def test_all_zeros_returns_score_zero_and_low(self):
        result = compute_risk_score({"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["label"], "low")

    def test_one_critical_gives_score_10(self):
        result = compute_risk_score({"Critical": 1, "High": 0, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 10)
        self.assertEqual(result["label"], "low")

    def test_medium_threshold_15_gives_medium_label(self):
        # 5 High x 5 = 25 -> medium (>=15, <40)
        result = compute_risk_score({"Critical": 0, "High": 5, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 25)
        self.assertEqual(result["label"], "medium")

    def test_high_threshold_gives_high_label(self):
        # 8 High x 5 = 40 -> high (>=40)
        result = compute_risk_score({"Critical": 0, "High": 8, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 40)
        self.assertEqual(result["label"], "high")

    def test_critical_threshold_gives_critical_label(self):
        # 7 Criticals x 10 = 70 -> critical (>=70)
        result = compute_risk_score({"Critical": 7, "High": 0, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 70)
        self.assertEqual(result["label"], "critical")

    def test_score_capped_at_100(self):
        result = compute_risk_score({"Critical": 20, "High": 0, "Medium": 0, "Low": 0, "Info": 0})
        self.assertEqual(result["score"], 100)

    def test_info_does_not_contribute(self):
        result = compute_risk_score({"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 100})
        self.assertEqual(result["score"], 0)


class ProductSummaryRiskScoreApiTests(TestCase):

    """Integration tests: risk_score appears in /api/v2/aist/product-summaries/ response."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rs_user", email="rs@example.com", password="pass",  # noqa: S106
        )
        self.client.force_login(self.user)

        sla = SLA_Configuration.objects.create(name="SLA rs")
        prod_type = Product_Type.objects.create(name="PT rs")
        role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=prod_type, user=self.user, role=role)

        self.product = Product.objects.create(
            name="RS Product", description="d", prod_type=prod_type, sla_configuration_id=sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product, supported_languages=["python"],
        )
        tt = Test_Type.objects.create(name="RS test type")
        start = timezone.make_aware(datetime(2026, 1, 1))
        end = timezone.make_aware(datetime(2026, 12, 31))
        eng = Engagement.objects.create(name="RS eng", target_start=start, target_end=end, product=self.product)
        test = Test.objects.create(engagement=eng, target_start=start, target_end=end, test_type=tt)

        # 2 Criticals (score = 20 → medium)
        for i in range(2):
            Finding.objects.create(
                test=test, title=f"Crit {i}", severity="Critical", active=True, reporter=self.user,
            )

    def _url(self):
        return reverse("client_product_summary")

    def test_risk_score_in_response(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        rs = results[0]["risk_score"]
        self.assertIsNotNone(rs)
        self.assertEqual(rs["score"], 20)   # 2 x 10
        self.assertEqual(rs["label"], "medium")

    def test_zero_findings_gives_low_risk(self):
        # Create a second product with no findings
        sla2 = SLA_Configuration.objects.create(name="SLA rs2")
        pt2 = Product_Type.objects.create(name="PT rs2")
        role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=pt2, user=self.user, role=role)
        prod2 = Product.objects.create(name="Empty RS", description="d", prod_type=pt2, sla_configuration_id=sla2.id)
        AISTProject.objects.create(product=prod2, supported_languages=[], compilable=False, profile={})

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        empty = next(r for r in response.json()["results"] if r["product_name"] == "Empty RS")
        self.assertEqual(empty["risk_score"]["score"], 0)
        self.assertEqual(empty["risk_score"]["label"], "low")


class RegressionDetectionTests(TestCase):

    """Tests for detect_regressions_for_pipeline()."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reg_user", email="reg@example.com", password="pass",  # noqa: S106
        )
        sla = SLA_Configuration.objects.create(name="SLA reg")
        prod_type = Product_Type.objects.create(name="PT reg")
        self.product = Product.objects.create(
            name="Reg Product", description="d", prod_type=prod_type, sla_configuration_id=sla.id,
        )
        tt = Test_Type.objects.create(name="Reg test type")
        start = timezone.make_aware(datetime(2026, 1, 1))
        end = timezone.make_aware(datetime(2026, 12, 31))
        eng = Engagement.objects.create(name="Reg eng", target_start=start, target_end=end, product=self.product)

        # Old test (pipeline 1): finding that was mitigated
        self.old_test = Test.objects.create(engagement=eng, target_start=start, target_end=end, test_type=tt)
        self.mitigated_finding = Finding.objects.create(
            test=self.old_test,
            title="SQL Injection",
            severity="High",
            active=False,
            is_mitigated=True,
            hash_code="abc123hash",
            reporter=self.user,
        )

        # New test (pipeline 2): same hash_code re-appears as active
        self.new_test = Test.objects.create(engagement=eng, target_start=start, target_end=end, test_type=tt)
        self.new_finding = Finding.objects.create(
            test=self.new_test,
            title="SQL Injection",
            severity="High",
            active=True,
            is_mitigated=False,
            hash_code="abc123hash",
            reporter=self.user,
        )

        # Unrelated active finding (different hash — should NOT be regression)
        self.unrelated_finding = Finding.objects.create(
            test=self.new_test,
            title="XSS",
            severity="Medium",
            active=True,
            is_mitigated=False,
            hash_code="xss_hash_999",
            reporter=self.user,
        )

    def test_regression_detected_for_reappeared_finding(self):
        count = detect_regressions_for_pipeline("pipe-001", [self.new_test.id])
        self.assertEqual(count, 1)
        annotation = AISTFindingAnnotation.objects.get(finding=self.new_finding)
        self.assertTrue(annotation.is_regression)
        self.assertIsNotNone(annotation.regression_detected_at)

    def test_unrelated_finding_not_marked_regression(self):
        detect_regressions_for_pipeline("pipe-001", [self.new_test.id])
        self.assertFalse(
            AISTFindingAnnotation.objects.filter(finding=self.unrelated_finding, is_regression=True).exists(),
        )

    def test_no_regressions_when_no_previously_mitigated(self):
        # Fresh product with only new active findings
        sla2 = SLA_Configuration.objects.create(name="SLA reg2")
        pt2 = Product_Type.objects.create(name="PT reg2")
        prod2 = Product.objects.create(name="Fresh", description="d", prod_type=pt2, sla_configuration_id=sla2.id)
        tt2 = Test_Type.objects.create(name="TT reg2")
        start = timezone.make_aware(datetime(2026, 1, 1))
        end = timezone.make_aware(datetime(2026, 12, 31))
        eng2 = Engagement.objects.create(name="Eng2", target_start=start, target_end=end, product=prod2)
        fresh_test = Test.objects.create(engagement=eng2, target_start=start, target_end=end, test_type=tt2)
        Finding.objects.create(
            test=fresh_test, title="New Bug", severity="Low",
            active=True, hash_code="fresh_hash", reporter=self.user,
        )

        count = detect_regressions_for_pipeline("pipe-002", [fresh_test.id])
        self.assertEqual(count, 0)

    def test_empty_test_ids_returns_zero(self):
        count = detect_regressions_for_pipeline("pipe-003", [])
        self.assertEqual(count, 0)

    def test_idempotent_second_run_does_not_duplicate(self):
        detect_regressions_for_pipeline("pipe-001", [self.new_test.id])
        detect_regressions_for_pipeline("pipe-001", [self.new_test.id])
        self.assertEqual(AISTFindingAnnotation.objects.filter(finding=self.new_finding).count(), 1)
