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

from aist.models import (
    AISTAIFindingResponse,
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    VersionType,
    WorkItemLink,
    WorkItemStatusCategory,
)


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
            compilable=False,
            profile={},
        )
        self.pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
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

    def test_status_breakdown_mitigated_excludes_false_positive_and_out_of_scope(self):
        # A finding closed as false positive has is_mitigated=True in DefectDojo, but must NOT
        # inflate the mitigated counter — it must appear only under false_positive.
        Finding.objects.create(
            test=self.test,
            title="False positive finding",
            severity="Low",
            active=False,
            is_mitigated=True,
            false_p=True,
            reporter=self.user,
        )
        # Same for out of scope.
        Finding.objects.create(
            test=self.test,
            title="Out of scope finding",
            severity="Low",
            active=False,
            is_mitigated=True,
            out_of_scope=True,
            reporter=self.user,
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        status = response.json()["finding_status_breakdown"]

        # Only the original finding_mitigated (no false_p/out_of_scope) counts as mitigated.
        self.assertEqual(status["mitigated"], 1)
        self.assertEqual(status["false_positive"], 1)
        self.assertEqual(status["out_of_scope"], 1)

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
            project_version=self.pv,
            status=AISTStatus.FINISHED,
            created=now - timedelta(minutes=80),
        )
        AISTPipeline.objects.filter(id="dashboard-pipe-finished").update(updated=now - timedelta(minutes=40))
        AISTPipeline.objects.create(
            id="dashboard-pipe-warn",
            project=self.project,
            project_version=self.pv,
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
            project_version=self.pv,
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

    # -- triage progress ---------------------------------------------------

    def _link(self, finding, key, status_category, *, created=None):
        link = WorkItemLink.objects.create(
            finding=finding,
            external_url=f"https://jira.example.com/{key}",
            external_key=key,
            status_category=status_category,
        )
        if created is not None:
            # ``created`` is auto_now_add: backdate it the way an old ticket looks.
            WorkItemLink.objects.filter(pk=link.pk).update(created=created)
        return link

    def _triage(self, **query):
        response = self.client.get(self._url(), data=query)
        self.assertEqual(response.status_code, 200)
        return response.json()["triage_progress"]

    @staticmethod
    def _states(triage):
        return {row["state"]: row["count"] for row in triage["by_state"]}

    def test_triage_progress_without_tickets_leaves_every_active_finding_untriaged(self):
        triage = self._triage()

        self.assertEqual(triage["total_active"], 4)
        self.assertEqual(triage["ticketed"], 0)
        self.assertAlmostEqual(triage["coverage_pct"], 0.0)
        self.assertEqual(triage["untriaged_critical_high"], 2)
        self.assertEqual(
            [row["state"] for row in triage["by_state"]],
            ["none", "OPEN", "IN_PROGRESS", "DONE", "CANCELLED", "UNKNOWN"],
        )
        self.assertEqual(self._states(triage)["none"], 4)
        severities = {row["severity"]: row for row in triage["by_severity"]}
        self.assertEqual([row["severity"] for row in triage["by_severity"]], ["Critical", "High", "Medium", "Low", "Info"])
        self.assertTrue(severities["Critical"]["below_target"])
        # No Info findings: an empty severity is never reported as behind its target.
        self.assertEqual(severities["Info"]["total"], 0)
        self.assertFalse(severities["Info"]["below_target"])

    def test_triage_progress_counts_each_finding_once_by_its_deciding_ticket(self):
        # Jira and GitLab tickets on one finding: the one still in progress decides.
        self._link(self.finding_critical, "SEC-1", WorkItemStatusCategory.OPEN)
        self._link(self.finding_critical, "SEC-2", WorkItemStatusCategory.IN_PROGRESS)
        # Done only because no ticket is left open.
        self._link(self.finding_high, "SEC-3", WorkItemStatusCategory.DONE)
        self._link(self.finding_high, "SEC-4", WorkItemStatusCategory.CANCELLED)
        # A fixed finding's ticket is not triage progress of active findings.
        self._link(self.finding_mitigated, "SEC-5", WorkItemStatusCategory.OPEN)

        triage = self._triage()
        states = self._states(triage)

        self.assertEqual(states, {"none": 2, "OPEN": 0, "IN_PROGRESS": 1, "DONE": 1, "CANCELLED": 0, "UNKNOWN": 0})
        self.assertEqual(sum(states.values()), triage["total_active"])
        self.assertEqual(triage["ticketed"], 2)
        self.assertAlmostEqual(triage["coverage_pct"], 50.0)
        self.assertEqual(triage["untriaged_critical_high"], 0)
        severities = {row["severity"]: row for row in triage["by_severity"]}
        self.assertEqual((severities["High"]["ticketed"], severities["High"]["total"]), (1, 1))
        self.assertFalse(severities["High"]["below_target"])
        self.assertEqual((severities["Medium"]["ticketed"], severities["Medium"]["total"]), (0, 2))
        self.assertAlmostEqual(severities["Medium"]["untriaged_pct"], 100.0)
        self.assertTrue(severities["Medium"]["below_target"])

    def test_ticketed_recently_counts_a_finding_by_its_first_ticket(self):
        now = timezone.now()
        self._link(self.finding_critical, "SEC-10", WorkItemStatusCategory.OPEN, created=now - timedelta(days=5))
        # Ticketed 40 days ago; a second ticket this week does not make it newly ticketed.
        self._link(self.finding_high, "SEC-11", WorkItemStatusCategory.DONE, created=now - timedelta(days=40))
        self._link(self.finding_high, "SEC-12", WorkItemStatusCategory.OPEN, created=now - timedelta(days=2))
        self._link(self.finding_medium, "SEC-13", WorkItemStatusCategory.OPEN, created=now - timedelta(days=75))

        recent = self._triage()["ticketed_recently"]

        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["previous_count"], 1)
        self.assertEqual(recent["delta_pct"], 0)
        self.assertEqual(recent["linked_from"], (timezone.localdate() - timedelta(days=30)).isoformat())

    def test_ticketed_recently_has_no_delta_without_a_previous_window(self):
        self._link(self.finding_critical, "SEC-20", WorkItemStatusCategory.OPEN)

        recent = self._triage()["ticketed_recently"]

        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["previous_count"], 0)
        self.assertIsNone(recent["delta_pct"])

    def test_stale_open_counts_open_tickets_nobody_picked_up(self):
        now = timezone.now()
        self._link(self.finding_critical, "SEC-30", WorkItemStatusCategory.OPEN, created=now - timedelta(days=45))
        self._link(self.finding_high, "SEC-31", WorkItemStatusCategory.OPEN, created=now - timedelta(days=3))
        self._link(self.finding_medium, "SEC-32", WorkItemStatusCategory.IN_PROGRESS, created=now - timedelta(days=45))

        stale = self._triage()["stale_open"]

        self.assertEqual(stale["count"], 1)
        self.assertEqual(stale["days"], 30)
        self.assertEqual(stale["linked_until"], (timezone.localdate() - timedelta(days=30)).isoformat())

    def test_weekly_series_tracks_new_ticketed_dismissed_and_backlog(self):
        self._link(self.finding_critical, "SEC-40", WorkItemStatusCategory.OPEN)
        Finding.objects.filter(pk=self.finding_high.pk).update(
            active=False, false_p=True, last_status_update=timezone.now(),
        )

        triage = self._triage()
        weekly = triage["weekly"]

        self.assertEqual(len(weekly), 12)
        current_week = max(weekly, key=itemgetter("week"))
        week_start = datetime.fromisoformat(current_week["week"]).date()
        self.assertEqual(current_week["week_end"], (week_start + timedelta(days=6)).isoformat())
        self.assertEqual(current_week["new_findings"], 5)
        self.assertEqual(current_week["ticketed"], 1)
        # The false positive marked above and the fixture's risk-accepted finding: both are
        # triage decisions that close a finding without a ticket.
        self.assertEqual(current_week["dismissed"], 2)
        # This week ends with the queue the progress bar shows today.
        self.assertEqual(current_week["untriaged_at_week_end"], 2)
        self.assertEqual(current_week["untriaged_at_week_end"], self._states(triage)["none"])
        # Every finding was found this week, so earlier weeks had nothing to triage.
        self.assertTrue(all(row["untriaged_at_week_end"] == 0 for row in weekly[:-1]))

    def test_weekly_backlog_shrinks_on_the_week_a_finding_got_its_ticket(self):
        now = timezone.now()
        Finding.objects.filter(pk=self.finding_critical.pk).update(date=(now - timedelta(days=21)).date())
        self._link(self.finding_critical, "SEC-50", WorkItemStatusCategory.OPEN, created=now - timedelta(days=7))

        weekly = self._triage()["weekly"]
        by_week = {row["week"]: row for row in weekly}
        found_week = (now - timedelta(days=21)).date()
        found_week -= timedelta(days=found_week.weekday())

        self.assertEqual(by_week[found_week.isoformat()]["untriaged_at_week_end"], 1)
        self.assertEqual(weekly[-1]["untriaged_at_week_end"], 3)

    def test_triage_progress_follows_the_project_filter(self):
        product2 = Product.objects.create(
            name="Coverage Product 2",
            description="p2",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=product2,
            supported_languages=["js"],
            compilable=False,
            profile={},
        )
        engagement2 = Engagement.objects.create(
            name="Coverage eng 2",
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
        finding_p2 = Finding.objects.create(
            test=test2,
            title="P2 finding",
            severity="High",
            active=True,
            reporter=self.user,
        )
        self._link(finding_p2, "P2-1", WorkItemStatusCategory.DONE)

        triage = self._triage(project_id=self.project.id)

        self.assertEqual(triage["total_active"], 4)
        self.assertEqual(triage["ticketed"], 0)
        self.assertEqual(self._states(triage)["DONE"], 0)
