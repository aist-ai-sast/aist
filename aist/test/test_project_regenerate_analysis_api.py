from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.test.test_api import AISTApiBase


class AISTProjectRegenerateAnalysisAPITests(AISTApiBase):

    """Tests for POST /projects/<id>/regenerate-analysis/."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Regen Org", product_type=self.prod_type)
        self.project.repository = RepositoryInfo.objects.create(
            type=ScmType.GITEA,
            repo_owner="myorg",
            repo_name="myrepo",
            base_url="http://gitea.internal:3000",
        )
        self.project.save(update_fields=["repository"])
        self.main = AISTProjectVersion.objects.create(
            project=self.project, version="main", version_type=VersionType.GIT_BRANCH,
        )

    def _post(self, project_id: int, data=None, client=None):
        body = {"project_version_id": self.main.id} if data is None else data
        return (client or self.client).post(self._url(project_id), data=body, format="json")

    def _url(self, project_id: int) -> str:
        return reverse("aist_api:project_regenerate_analysis", kwargs={"project_id": project_id})

    def _make_active_claude_integration(self):
        return OrgIntegration.objects.create(
            organization=self.org,
            integration_type="CLAUDE_CODE",
            name="Claude",
            is_active=True,
            secret="sk-ant-oat01-" + "a" * 24,
        )

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_queues_task_when_preconditions_met(self, mock_delay):
        self._make_active_claude_integration()

        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.data["queued"])
        mock_delay.assert_called_once_with(self.project.id, self.main.id)

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_uses_the_chosen_branch_not_the_newest(self, mock_delay):
        self._make_active_claude_integration()
        AISTProjectVersion.objects.create(
            project=self.project, version="develop", version_type=VersionType.GIT_BRANCH,
        )

        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 202)
        mock_delay.assert_called_once_with(self.project.id, self.main.id)

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_requires_project_version_id(self, mock_delay):
        self._make_active_claude_integration()

        for body in ({}, {"project_version_id": "main"}, {"project_version_id": 0}):
            with self.subTest(body=body):
                resp = self._post(self.project.id, data=body)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("project_version_id", resp.data)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_rejects_non_branch_version_of_this_project(self, mock_delay):
        self._make_active_claude_integration()
        dast_target = AISTProjectVersion.objects.create(
            project=self.project, version="https://app.example", version_type=VersionType.DAST_TARGET,
        )

        # self.pv is a GIT_HASH version of this project.
        for version in (self.pv, dast_target):
            with self.subTest(version_type=version.version_type):
                resp = self._post(self.project.id, data={"project_version_id": version.id})
                self.assertEqual(resp.status_code, 400)
                self.assertIn("project_version_id", resp.data)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_hides_versions_of_other_projects(self, mock_delay):
        """A branch of another project — same org or another org — is simply not found."""
        self._make_active_claude_integration()
        sibling_product = Product.objects.create(
            name="Sibling Product", prod_type=self.prod_type, description="d", sla_configuration_id=self.sla.id,
        )
        sibling = AISTProject.objects.create(
            product=sibling_product, supported_languages=[], compilable=False, profile={},
        )
        sibling_main = AISTProjectVersion.objects.create(
            project=sibling, version="main", version_type=VersionType.GIT_BRANCH,
        )
        foreign_main = AISTProjectVersion.objects.create(
            project=self.other_project, version="main", version_type=VersionType.GIT_BRANCH,
        )

        for version in (sibling_main, foreign_main):
            with self.subTest(project=version.project_id):
                resp = self._post(self.project.id, data={"project_version_id": version.id})
                self.assertEqual(resp.status_code, 404)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_without_repository(self, mock_delay):
        self._make_active_claude_integration()
        self.project.repository = None
        self.project.save(update_fields=["repository"])

        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_without_active_claude_integration(self, mock_delay):
        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_when_claude_integration_inactive(self, mock_delay):
        integration = self._make_active_claude_integration()
        integration.is_active = False
        integration.save(update_fields=["is_active"])

        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_denies_other_product(self, mock_delay):
        self._make_active_claude_integration()

        resp = self._post(self.other_project.id, data={"project_version_id": self.other_pv.id})

        self.assertEqual(resp.status_code, 404)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_requires_edit_permission(self, mock_delay):
        """
        A reader-only role can view the project but must not be able to trigger regeneration.

        Mirrors AISTProjectDetailAPI.delete/post: the authorized queryset is
        already scoped to Product_Edit, so an insufficiently-privileged user
        gets 404 (object not found in their authorized queryset), same as
        cross-org access — not a separate 403 branch.
        """
        self._make_active_claude_integration()
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).update(role=role_reader)

        resp = self._post(self.project.id)

        self.assertEqual(resp.status_code, 404)
        mock_delay.assert_not_called()

    def test_unauthenticated_returns_401_or_403(self):
        anon = APIClient()

        resp = self._post(self.project.id, client=anon)

        self.assertIn(resp.status_code, [401, 403])
