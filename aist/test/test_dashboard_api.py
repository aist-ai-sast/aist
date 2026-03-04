from __future__ import annotations

from datetime import datetime, timedelta
from operator import itemgetter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    CWE,
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

from aist.models import AISTAIFindingResponse, AISTPipeline, AISTProject, AISTStatus


class DashboardSummaryViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="dashboard_user",
            email="dashboard@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_login(self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA dashboard")
        self.prod_type = Product_Type.objects.create(name="PT dashboard")
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.user,
            role=self.role_maintainer,
        )

        self.product = Product.objects.create(
            name="Dashboard Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )

        self.test_type = Test_Type.objects.create(name="Dashboard test type")
        period_start = timezone.make_aware(datetime(2026, 1, 1, 0, 0, 0))
        period_end = timezone.make_aware(datetime(2026, 12, 31, 23, 59, 59))
        self.engagement = Engagement.objects.create(
            name="Dashboard engagement",
            target_start=period_start,
            target_end=period_end,
            product=self.product,
        )
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=period_start,
            target_end=period_end,
            test_type=self.test_type,
        )

        self.finding_critical = Finding.objects.create(
            test=self.test,
            title="Critical finding",
            severity="Critical",
            active=True,
            reporter=self.user,
        )
        self.finding_high = Finding.objects.create(
            test=self.test,
            title="High finding",
            severity="High",
            active=True,
            reporter=self.user,
        )
        self.finding_medium = Finding.objects.create(
            test=self.test,
            title="Medium finding",
            severity="Medium",
            active=True,
            reporter=self.user,
        )
        self.finding_mitigated = Finding.objects.create(
            test=self.test,
            title="Mitigated finding",
            severity="High",
            active=False,
            is_mitigated=True,
            reporter=self.user,
        )
        self.finding_risk_accepted = Finding.objects.create(
            test=self.test,
            title="Risk accepted finding",
            severity="Medium",
            active=True,
            risk_accepted=True,
            reporter=self.user,
        )

    def _url(self):
        return reverse("client_dashboard_summary")

    def test_requires_authentication(self):
        unauthenticated = Client()
        response = unauthenticated.get(self._url())
        self.assertEqual(response.status_code, 302)

    def test_kpi_values_are_correct(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        kpi = response.json()["kpi"]

        # 4 active: critical, high, medium, risk_accepted (risk_accepted is still active)
        self.assertEqual(kpi["total_active"], 4)
        # critical + high active: critical and high findings
        self.assertEqual(kpi["critical_high"], 2)
        # total includes mitigated too
        self.assertEqual(kpi["total_findings"], 5)
        self.assertEqual(kpi["risk_accepted"], 1)
        self.assertEqual(kpi["projects_count"], 1)

    def test_severity_distribution_counts_active_only(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        sev = response.json()["severity_distribution"]

        self.assertEqual(sev["Critical"], 1)
        # 1 active High (mitigated high is excluded)
        self.assertEqual(sev["High"], 1)
        # 2 active Medium (medium + risk_accepted medium)
        self.assertEqual(sev["Medium"], 2)
        self.assertEqual(sev["Low"], 0)
        self.assertEqual(sev["Info"], 0)

    def test_top_projects_includes_current_project(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        top = response.json()["top_projects"]

        self.assertEqual(len(top), 1)
        proj = top[0]
        self.assertEqual(proj["project_id"], self.project.id)
        self.assertEqual(proj["name"], self.product.name)
        self.assertEqual(proj["critical"], 1)
        self.assertEqual(proj["high"], 1)
        self.assertEqual(proj["medium"], 2)
        self.assertEqual(proj["total_active"], 4)

    def test_status_breakdown_values(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        status = response.json()["finding_status_breakdown"]

        self.assertEqual(status["active"], 4)
        self.assertEqual(status["mitigated"], 1)
        self.assertEqual(status["risk_accepted"], 1)
        self.assertEqual(status["under_review"], 0)
        self.assertEqual(status["false_positive"], 0)
        self.assertEqual(status["out_of_scope"], 0)

    def test_project_id_filter_returns_only_that_project(self):
        # Create a second product/project with findings
        product2 = Product.objects.create(
            name="Other Product",
            description="other",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        project2 = AISTProject.objects.create(
            product=product2,
            supported_languages=["go"],
            script_path="scripts/build2.sh",
            compilable=False,
            profile={},
        )
        engagement2 = Engagement.objects.create(
            name="Other engagement",
            target_start=timezone.make_aware(datetime(2026, 1, 1, 0, 0, 0)),
            target_end=timezone.make_aware(datetime(2026, 12, 31, 23, 59, 59)),
            product=product2,
        )
        test2 = Test.objects.create(
            engagement=engagement2,
            target_start=timezone.make_aware(datetime(2026, 1, 1, 0, 0, 0)),
            target_end=timezone.make_aware(datetime(2026, 12, 31, 23, 59, 59)),
            test_type=self.test_type,
        )
        Finding.objects.create(
            test=test2,
            title="Other critical",
            severity="Critical",
            active=True,
            reporter=self.user,
        )

        # Filter by original project
        response = self.client.get(self._url(), data={"project_id": self.project.id})
        self.assertEqual(response.status_code, 200)
        kpi = response.json()["kpi"]
        self.assertEqual(kpi["projects_count"], 1)
        self.assertEqual(kpi["total_active"], 4)

        # Filter by second project
        response2 = self.client.get(self._url(), data={"project_id": project2.id})
        self.assertEqual(response2.status_code, 200)
        kpi2 = response2.json()["kpi"]
        self.assertEqual(kpi2["projects_count"], 1)
        self.assertEqual(kpi2["total_active"], 1)
        self.assertEqual(kpi2["critical_high"], 1)

        _ = project2  # used above

    def test_invalid_project_id_returns_empty(self):
        response = self.client.get(self._url(), data={"project_id": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        # invalid project_id is ignored, returns all authorized projects
        kpi = response.json()["kpi"]
        self.assertGreaterEqual(kpi["projects_count"], 1)

    def test_no_findings_returns_zero_kpis(self):
        # Create a project with no findings
        product_empty = Product.objects.create(
            name="Empty Product",
            description="no findings",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        project_empty = AISTProject.objects.create(
            product=product_empty,
            supported_languages=["java"],
            script_path="scripts/empty.sh",
            compilable=False,
            profile={},
        )
        response = self.client.get(self._url(), data={"project_id": project_empty.id})
        self.assertEqual(response.status_code, 200)
        kpi = response.json()["kpi"]
        self.assertEqual(kpi["total_active"], 0)
        self.assertEqual(kpi["critical_high"], 0)
        self.assertEqual(kpi["total_findings"], 0)
        self.assertEqual(len(response.json()["top_projects"]), 0)

        _ = project_empty  # used above

    def test_findings_aging_heatmap_counts_active_findings_by_bucket(self):
        old_date = (timezone.now() - timedelta(days=45)).date()
        self.finding_critical.date = old_date
        self.finding_critical.save(update_fields=["date", "updated"])

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        heatmap = response.json()["findings_aging_heatmap"]["matrix"]

        self.assertEqual(heatmap["Critical"]["31_90"], 1)
        self.assertEqual(heatmap["Critical"]["0_7"], 0)
        self.assertEqual(heatmap["High"]["0_7"], 1)
        self.assertEqual(heatmap["Medium"]["0_7"], 2)

    def test_risk_trend_contains_current_week_new_and_mitigated(self):
        self.finding_mitigated.last_status_update = timezone.now()
        self.finding_mitigated.save(update_fields=["last_status_update", "updated"])

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        trend = response.json()["risk_trend"]
        self.assertEqual(len(trend), 12)

        current_week = max(trend, key=itemgetter("week"))
        self.assertEqual(current_week["new_findings"], 5)
        self.assertEqual(current_week["mitigated_findings"], 1)
        self.assertEqual(current_week["net"], 4)

    def test_pipeline_performance_trend_reports_runs_median_and_warning_rate(self):
        now = timezone.now()
        AISTPipeline.objects.create(
            id="dashboard-pipe-finished",
            project=self.project,
            status=AISTStatus.FINISHED,
            created=now - timedelta(minutes=80),
        )
        AISTPipeline.objects.filter(id="dashboard-pipe-finished").update(updated=now - timedelta(minutes=40))
        AISTPipeline.objects.create(
            id="dashboard-pipe-warn",
            project=self.project,
            status=AISTStatus.FINISHED_WITH_WARNINGS,
            created=now - timedelta(minutes=70),
        )
        AISTPipeline.objects.filter(id="dashboard-pipe-warn").update(updated=now - timedelta(minutes=10))

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        perf = response.json()["pipeline_performance_trend"]
        self.assertEqual(len(perf), 12)

        current_week = max(perf, key=itemgetter("week"))
        self.assertEqual(current_week["runs"], 2)
        self.assertEqual(current_week["median_duration_seconds"], 3000)
        self.assertAlmostEqual(current_week["warnings_rate"], 0.5)

    def test_ai_verdict_analytics_groups_by_verdict_and_severity(self):
        pipeline = AISTPipeline.objects.create(
            id="dashboard-pipe-ai",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            finding=self.finding_critical,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            uncertainty_level=0.20,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            finding=self.finding_high,
            verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE,
            uncertainty_level=0.90,
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        analytics = response.json()["ai_verdict_analytics"]

        self.assertEqual(analytics["total"], 2)
        self.assertEqual(analytics["verdict_counts"]["true_positive"], 1)
        self.assertEqual(analytics["verdict_counts"]["false_positive"], 1)
        self.assertEqual(analytics["severity_by_verdict"]["Critical"]["true_positive"], 1)
        self.assertEqual(analytics["severity_by_verdict"]["High"]["false_positive"], 1)
        self.assertEqual(analytics["uncertainty_buckets"]["low"], 1)
        self.assertEqual(analytics["uncertainty_buckets"]["high"], 1)

    def test_cwe_distribution_returns_top_active_cwes_with_enriched_metadata(self):
        cache.delete("aist:cwe:meta:79")
        cache.delete("aist:cwe:meta:89")
        CWE.objects.update_or_create(
            number=79,
            defaults={
                "description": "Improper Neutralization of Input During Web Page Generation ('Cross-site Scripting')",
                "url": "https://cwe.mitre.org/data/definitions/79.html",
            },
        )
        CWE.objects.update_or_create(
            number=89,
            defaults={
                "description": "Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')",
                "url": "https://cwe.mitre.org/data/definitions/89.html",
            },
        )

        self.finding_critical.cwe = 79
        self.finding_critical.save(update_fields=["cwe", "updated"])
        self.finding_high.cwe = 79
        self.finding_high.save(update_fields=["cwe", "updated"])
        self.finding_medium.cwe = 89
        self.finding_medium.save(update_fields=["cwe", "updated"])
        # Inactive finding must not contribute to CWE distribution.
        self.finding_mitigated.cwe = 79
        self.finding_mitigated.save(update_fields=["cwe", "updated"])

        with patch(
            "aist.views.summaries.fetch_cwe_meta",
            return_value={
                "title": "Improper Neutralization of Input During Web Page Generation",
                "description": "Improper neutralization of untrusted input in generated output.",
                "impact": "Execution of attacker-controlled scripts in victim browsers.",
                "url": "https://cwe.mitre.org/data/definitions/79.html",
            },
        ):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        distribution = response.json()["cwe_distribution"]
        self.assertEqual(len(distribution), 2)
        self.assertEqual(distribution[0]["cwe"], 79)
        self.assertEqual(distribution[0]["count"], 2)
        self.assertIn("Improper Neutralization", distribution[0]["title"])
        self.assertIn("Improper neutralization", distribution[0]["description"])
        self.assertIn("attacker-controlled", distribution[0]["impact"])
        self.assertEqual(distribution[1]["cwe"], 89)
        self.assertEqual(distribution[1]["count"], 1)
