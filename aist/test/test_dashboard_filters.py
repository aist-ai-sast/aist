"""
Dashboard summary under the shared Findings filter.

The Security Dashboard accepts the same query string as the Findings page.
The scenarios here are the ones a user relies on: every number equals what the
Findings list shows for the same filter, the filter only ever narrows what the
user may already see, and widgets that are not finding-based stay put.
"""
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
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)
from rest_framework.test import APIClient

from aist.models import (
    AISTAIFindingResponse,
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    Organization,
    OrgMemberAccessScope,
    ProjectAccessDenial,
    VersionType,
    WorkItemLink,
    WorkItemStatusCategory,
)

User = get_user_model()

PERIOD_START = datetime(2026, 1, 1, 0, 0, 0)
PERIOD_END = datetime(2026, 12, 31, 23, 59, 59)


class DashboardFilterBase(TestCase):

    """
    Org A: projects P1 (four findings) and P2 (one finding). Org B: one project.

    P1 findings are tagged so that several of them carry more than one tag:
    a filter that joins tags into the aggregate would count them twice.
    """

    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA dashboard filters")
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        self.test_type = Test_Type.objects.create(name="Dashboard filter test type")

        self.pt_a = Product_Type.objects.create(name="Dashboard Org A")
        self.org_a = Organization.objects.create(name="Dashboard Org A", product_type=self.pt_a)
        self.pt_b = Product_Type.objects.create(name="Dashboard Org B")
        self.org_b = Organization.objects.create(name="Dashboard Org B", product_type=self.pt_b)

        self.maintainer = self._member("dash_maintainer", self.pt_a, self.role_maintainer)
        self.reporter = self.maintainer

        self.p1, self.pv1, test1 = self._project("Dash P1", self.pt_a)
        self.p2, self.pv2, test2 = self._project("Dash P2", self.pt_a)
        self.pb, self.pvb, test_b = self._project("Dash B1", self.pt_b)

        self.f_critical = self._finding(test1, self.pv1, "Critical SQL", "Critical", tags="auth,api", cwe=89)
        self.f_high = self._finding(test1, self.pv1, "High XSS", "High", tags="auth", cwe=79)
        self.f_mitigated = self._finding(
            test1, self.pv1, "High mitigated", "High", tags="test", active=False, is_mitigated=True,
        )
        self.f_medium = self._finding(test1, self.pv1, "Medium leak", "Medium", tags="test,api")
        self.f_p2 = self._finding(test2, self.pv2, "P2 high", "High", tags="p2only")
        self.f_org_b = self._finding(test_b, self.pvb, "Org B critical", "Critical", tags="orgb")

        self.pipe1 = self._pipeline("dash-filter-p1", self.p1, self.pv1)
        self.pipe2 = self._pipeline("dash-filter-p2", self.p2, self.pv2)
        self.pipe_b = self._pipeline("dash-filter-b", self.pb, self.pvb)
        self._ai(self.pipe1, self.f_critical, AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self._ai(self.pipe1, self.f_high, AISTAIFindingResponse.Verdict.FALSE_POSITIVE)
        self._ai(self.pipe2, self.f_p2, AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self._ai(self.pipe_b, self.f_org_b, AISTAIFindingResponse.Verdict.TRUE_POSITIVE)

        WorkItemLink.objects.create(
            finding=self.f_critical,
            external_url="https://jira.example.com/DASH-1",
            external_key="DASH-1",
            status_category=WorkItemStatusCategory.OPEN,
        )

    # -- fixtures --------------------------------------------------------

    def _member(self, username, prod_type, role):
        user = User.objects.create_user(username, f"{username}@example.com", "pass")
        Product_Type_Member.objects.create(product_type=prod_type, user=user, role=role)
        return user

    def _project(self, name, prod_type):
        product = Product.objects.create(
            name=name, description="d", prod_type=prod_type, sla_configuration_id=self.sla.id,
        )
        project = AISTProject.objects.create(
            product=product, supported_languages=["python"], compilable=False, profile={},
        )
        version = AISTProjectVersion.objects.create(
            project=project, version_type=VersionType.GIT_HASH, version=f"{name}-main",
        )
        engagement = Engagement.objects.create(
            name=f"{name} engagement",
            target_start=timezone.make_aware(PERIOD_START),
            target_end=timezone.make_aware(PERIOD_END),
            product=product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.make_aware(PERIOD_START),
            target_end=timezone.make_aware(PERIOD_END),
            test_type=self.test_type,
        )
        return project, version, test

    def _finding(self, test, version, title, severity, *, tags="", cwe=None, active=True, is_mitigated=False):
        finding = Finding.objects.create(
            test=test,
            title=title,
            severity=severity,
            active=active,
            is_mitigated=is_mitigated,
            cwe=cwe,
            file_path=f"src/{title.lower().replace(' ', '_')}.py",
            reporter=self.reporter,
        )
        if tags:
            finding.tags = tags
            finding.save()
        version.findings.add(finding)
        return finding

    def _pipeline(self, pipeline_id, project, version):
        return AISTPipeline.objects.create(
            id=pipeline_id, project=project, project_version=version, status=AISTStatus.FINISHED,
        )

    def _ai(self, pipeline, finding, verdict):
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline, finding=finding, verdict=verdict, uncertainty_level=0.2,
        )

    # -- helpers ---------------------------------------------------------

    def _client(self, user) -> APIClient:
        client = APIClient()
        client.force_login(user)
        return client

    def _dashboard(self, user, query=None):
        return self._client(user).get(reverse("client_dashboard_summary"), data=query or {})

    def _dashboard_ok(self, user, query=None) -> dict:
        response = self._dashboard(user, query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _findings_count(self, user, query) -> int:
        response = self._client(user).get(reverse("aist_api:finding_list"), data={**query, "limit": 1})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["count"]


class DashboardFindingsParityTests(DashboardFilterBase):

    """Scenario: the numbers on the dashboard equal the Findings list for the same filter."""

    QUERIES = (
        {},
        {"severity": "High"},
        {"severity": "Critical,High"},
        {"tags": "auth"},
        {"tags__and": "auth,api"},
        {"not_tags": "test"},
        {"cwe": "89"},
        {"active": "false"},
        {"active": "true"},
        {"work_item_status": "OPEN"},
        {"work_item_status": "none"},
        {"ai_status": "ai_tp"},
        {"ai_status": "no_ai"},
        {"title": "High"},
        {"file": "leak"},
        {"created_gte": timezone.now().date().isoformat()},
        {"project_version": "Dash P1-main"},
        {"pipeline_id": "dash-filter-p1"},
        {"project_id": "PLACEHOLDER_P1", "severity": "High"},
        {"project_id": "PLACEHOLDER_P1", "tags": "auth", "not_tags": "test", "active": "true"},
    )

    def _resolve(self, query: dict) -> dict:
        return {key: (str(self.p1.id) if value == "PLACEHOLDER_P1" else value) for key, value in query.items()}

    def test_total_findings_matches_findings_list_for_every_filter(self):
        for raw_query in self.QUERIES:
            query = self._resolve(raw_query)
            with self.subTest(query=query):
                payload = self._dashboard_ok(self.maintainer, query)
                self.assertEqual(payload["kpi"]["total_findings"], self._findings_count(self.maintainer, query))

    def test_active_kpis_match_findings_list_with_active_filter(self):
        for raw_query in self.QUERIES:
            query = self._resolve(raw_query)
            if "active" in query:
                continue
            with self.subTest(query=query):
                payload = self._dashboard_ok(self.maintainer, query)
                self.assertEqual(
                    payload["kpi"]["total_active"],
                    self._findings_count(self.maintainer, {**query, "active": "true"}),
                )

    def test_multi_tag_findings_are_counted_once(self):
        payload = self._dashboard_ok(self.maintainer, {"tags": "auth,api"})
        # f_critical carries both tags; it must not be counted twice.
        self.assertEqual(payload["kpi"]["total_findings"], 3)
        self.assertEqual(payload["kpi"]["total_active"], 3)
        self.assertEqual(payload["severity_distribution"]["Critical"], 1)
        self.assertEqual(payload["severity_distribution"]["High"], 1)
        self.assertEqual(payload["severity_distribution"]["Medium"], 1)
        self.assertEqual(payload["top_projects"][0]["total_active"], 3)

    def test_filter_reaches_every_finding_widget(self):
        payload = self._dashboard_ok(self.maintainer, {"severity": "High"})
        self.assertEqual(payload["severity_distribution"]["Critical"], 0)
        self.assertEqual(payload["severity_distribution"]["High"], 2)
        self.assertEqual(payload["finding_status_breakdown"]["active"], 2)
        self.assertEqual(payload["finding_status_breakdown"]["mitigated"], 1)
        self.assertEqual(payload["findings_aging_heatmap"]["matrix"]["Critical"]["0_7"], 0)
        self.assertEqual(payload["findings_aging_heatmap"]["matrix"]["High"]["0_7"], 2)
        self.assertEqual([row["cwe"] for row in payload["cwe_distribution"]], [79])
        self.assertEqual(payload["work_item_coverage"]["total_linked"], 0)
        self.assertEqual(sum(row["new_findings"] for row in payload["risk_trend"]), 3)


class DashboardFilterValidationTests(DashboardFilterBase):
    def test_invalid_filter_value_returns_400_with_field_error(self):
        for query, field in (
            ({"ai_status": "bogus"}, "ai_status"),
            ({"created_gte": "not-a-date"}, "created_gte"),
        ):
            with self.subTest(query=query):
                response = self._dashboard(self.maintainer, query)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json())

    def test_invalid_project_id_is_still_ignored(self):
        payload = self._dashboard_ok(self.maintainer, {"project_id": "not-a-number"})
        self.assertEqual(payload["kpi"]["projects_count"], 2)


class DashboardFilterIsolationTests(DashboardFilterBase):

    """Scenario: a filter can only narrow what the user may already see."""

    def _assert_empty(self, payload):
        self.assertEqual(payload["kpi"]["total_findings"], 0)
        self.assertEqual(payload["kpi"]["total_active"], 0)
        self.assertEqual(payload["top_projects"], [])
        self.assertEqual(payload["ai_verdict_analytics"]["total"], 0)
        self.assertEqual(payload["work_item_coverage"]["total_linked"], 0)

    def test_other_org_pipeline_tag_and_project_yield_nothing(self):
        self._assert_empty(self._dashboard_ok(self.maintainer, {"pipeline_id": self.pipe_b.id}))
        self._assert_empty(self._dashboard_ok(self.maintainer, {"tags": "orgb"}))
        payload = self._dashboard_ok(self.maintainer, {"project_id": self.pb.id})
        self._assert_empty(payload)
        self.assertEqual(payload["kpi"]["projects_count"], 0)

    def test_restricted_member_sees_only_granted_project(self):
        reader = self._member("dash_restricted", self.pt_a, self.role_reader)
        OrgMemberAccessScope.objects.create(organization=self.org_a, user=reader, restricted=True)
        Product_Member.objects.create(user=reader, product=self.p1.product, role=self.role_reader)

        payload = self._dashboard_ok(reader)
        self.assertEqual(payload["kpi"]["total_findings"], 4)
        self.assertEqual(payload["kpi"]["projects_count"], 1)
        self.assertEqual(payload["ai_verdict_analytics"]["total"], 2)
        self.assertEqual(payload["work_item_coverage"]["total_linked"], 1)

        self._assert_empty(self._dashboard_ok(reader, {"project_id": self.p2.id}))
        self._assert_empty(self._dashboard_ok(reader, {"tags": "p2only"}))
        self._assert_empty(self._dashboard_ok(reader, {"pipeline_id": self.pipe2.id}))
        ai_tp = self._dashboard_ok(reader, {"ai_status": "ai_tp"})
        self.assertEqual(ai_tp["kpi"]["total_findings"], 1)
        self.assertEqual(ai_tp["ai_verdict_analytics"]["total"], 1)

    def test_denied_project_stays_hidden_under_any_filter(self):
        reader = self._member("dash_denied", self.pt_a, self.role_reader)
        ProjectAccessDenial.objects.create(user=reader, project=self.p2)

        payload = self._dashboard_ok(reader)
        self.assertNotIn(self.p2.id, [row["project_id"] for row in payload["top_projects"]])
        self.assertEqual(payload["kpi"]["total_findings"], 4)

        self._assert_empty(self._dashboard_ok(reader, {"project_id": self.p2.id}))
        self._assert_empty(self._dashboard_ok(reader, {"tags": "p2only"}))
        self._assert_empty(self._dashboard_ok(reader, {"pipeline_id": self.pipe2.id}))


class DashboardWidgetScopeTests(DashboardFilterBase):
    def test_ai_verdict_analytics_follows_the_finding_filter(self):
        payload = self._dashboard_ok(self.maintainer, {"tags__and": "auth,api"})
        analytics = payload["ai_verdict_analytics"]
        self.assertEqual(analytics["total"], 1)
        self.assertEqual(analytics["verdict_counts"]["true_positive"], 1)
        self.assertEqual(analytics["verdict_counts"]["false_positive"], 0)
        self.assertEqual(analytics["severity_by_verdict"]["Critical"]["true_positive"], 1)

    def test_pipeline_performance_follows_project_only(self):
        baseline = self._dashboard_ok(self.maintainer)["pipeline_performance_trend"]
        filtered = self._dashboard_ok(
            self.maintainer, {"severity": "Critical", "tags": "auth", "ai_status": "no_ai"},
        )["pipeline_performance_trend"]
        self.assertEqual(filtered, baseline)
        self.assertEqual(sum(week["runs"] for week in baseline), 2)

        by_project = self._dashboard_ok(self.maintainer, {"project_id": self.p1.id})["pipeline_performance_trend"]
        self.assertEqual(sum(week["runs"] for week in by_project), 1)
