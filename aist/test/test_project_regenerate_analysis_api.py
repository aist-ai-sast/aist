from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.models import Organization, OrgIntegration, RepositoryInfo, ScmType
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

        resp = self.client.post(self._url(self.project.id))

        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.data["queued"])
        mock_delay.assert_called_once_with(self.project.id)

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_without_repository(self, mock_delay):
        self._make_active_claude_integration()
        self.project.repository = None
        self.project.save(update_fields=["repository"])

        resp = self.client.post(self._url(self.project.id))

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_without_active_claude_integration(self, mock_delay):
        resp = self.client.post(self._url(self.project.id))

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_returns_400_when_claude_integration_inactive(self, mock_delay):
        integration = self._make_active_claude_integration()
        integration.is_active = False
        integration.save(update_fields=["is_active"])

        resp = self.client.post(self._url(self.project.id))

        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @patch("aist.tasks.claude.analyze_project_after_import.delay")
    def test_regenerate_denies_other_product(self, mock_delay):
        self._make_active_claude_integration()

        resp = self.client.post(self._url(self.other_project.id))

        self.assertIn(resp.status_code, [403, 404])
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

        resp = self.client.post(self._url(self.project.id))

        self.assertIn(resp.status_code, [403, 404])
        mock_delay.assert_not_called()

    def test_unauthenticated_returns_401_or_403(self):
        anon = APIClient()

        resp = anon.post(self._url(self.project.id))

        self.assertIn(resp.status_code, [401, 403])
