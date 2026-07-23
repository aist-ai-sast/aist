from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.models import OrgIntegration, OrgIntegrationType, ProjectIntegrationOverride
from aist.test.test_api import AISTApiBase


class OrgIntegrationListCreateAPITests(AISTApiBase):

    """GET/POST /organizations/<org_id>/integrations/"""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(name="Test Org", product_type=self.org_prod_type)
        self.project.refresh_from_db()
        self.url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})

    def test_list_empty(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_create_gitlab_integration(self):
        resp = self.client.post(self.url, {
            "integration_type": "GITLAB",
            "name": "Production GitLab",
            "config": {"base_url": "https://gitlab.example.com"},
            "secret": "glpat-test-token",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["integration_type"], "GITLAB")
        self.assertEqual(resp.data["name"], "Production GitLab")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("secret", resp.data)  # write-only
        self.assertEqual(OrgIntegration.objects.count(), 1)

    def test_create_gerrit_integration(self):
        resp = self.client.post(self.url, {
            "integration_type": "GERRIT",
            "name": "Production Gerrit",
            "config": {"base_url": "https://gerrit.example.com", "username": "svc-user"},
            "secret": "http-password",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["integration_type"], "GERRIT")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("secret", resp.data)  # write-only

    def test_create_gerrit_integration_requires_username(self):
        resp = self.client.post(self.url, {
            "integration_type": "GERRIT",
            "name": "Bad Gerrit",
            "config": {"base_url": "https://gerrit.example.com"},
            "secret": "http-password",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_create_gitea_integration(self):
        resp = self.client.post(self.url, {
            "integration_type": "GITEA",
            "name": "Production Gitea",
            "config": {"base_url": "https://gitea.example.com"},
            "secret": "pat-token",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["integration_type"], "GITEA")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("secret", resp.data)  # write-only

    def test_create_gitea_integration_requires_base_url(self):
        resp = self.client.post(self.url, {
            "integration_type": "GITEA",
            "name": "Bad Gitea",
            "config": {},
            "secret": "pat-token",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_create_dast_integration(self):
        resp = self.client.post(self.url, {
            "integration_type": "DAST",
            "name": "Production DAST",
            "config": {"gateway_url": "https://dast-gateway.internal"},
            "secret": "pub_abc123.secretvaluevaluevalue",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["integration_type"], "DAST")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("secret", resp.data)  # write-only

    def test_create_dast_integration_requires_gateway_url(self):
        resp = self.client.post(self.url, {
            "integration_type": "DAST",
            "name": "Bad DAST",
            "config": {},
            "secret": "pub_abc123.secretvaluevaluevalue",
            "is_active": True,
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_create_slack_integration(self):
        resp = self.client.post(self.url, {
            "integration_type": "SLACK",
            "name": "Slack",
            "config": {"default_channel": "#appsec"},
            "secret": "xoxb-test-token",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["has_secret"])

    def test_create_github_integration_no_secret(self):
        resp = self.client.post(self.url, {
            "integration_type": "GITHUB",
            "name": "GitHub",
            "config": {"base_api_url": ""},
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data["has_secret"])

    def test_create_duplicate_name_fails(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type="GITLAB",
            name="Production GitLab",
        )
        resp = self.client.post(self.url, {
            "integration_type": "GITLAB",
            "name": "Production GitLab",
            "config": {},
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_list_returns_only_own_org(self):
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        other_prod_type = Product_Type.objects.create(name="Other Org PT")
        other_org = Organization.objects.create(name="Other Org", product_type=other_prod_type)
        OrgIntegration.objects.create(organization=other_org, integration_type="SLACK", name="Other Slack")
        OrgIntegration.objects.create(organization=self.org, integration_type="SLACK", name="My Slack")

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["name"], "My Slack")

    def test_unauthenticated_rejected(self):
        from rest_framework.test import APIClient  # noqa: PLC0415

        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 403)


class OrgIntegrationDetailAPITests(AISTApiBase):

    """GET/PATCH/DELETE /integrations/<id>/"""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(name="Detail Org", product_type=self.org_prod_type)
        self.project.refresh_from_db()
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="GITLAB",
            name="My GitLab",
            config={"base_url": "https://gitlab.example.com"},
            secret="glpat-initial",  # noqa: S106
            is_active=True,
        )
        self.url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": self.integration.pk})

    def test_get_returns_integration(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "My GitLab")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("glpat-initial", str(resp.data))

    def test_patch_updates_config(self):
        resp = self.client.patch(self.url, {"config": {"base_url": "https://gitlab2.example.com"}}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.config["base_url"], "https://gitlab2.example.com")

    def test_patch_without_secret_preserves_existing_secret(self):
        resp = self.client.patch(self.url, {"name": "Updated Name"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.secret, "glpat-initial")
        self.assertEqual(self.integration.name, "Updated Name")

    def test_patch_with_empty_secret_clears_it(self):
        resp = self.client.patch(self.url, {"secret": ""}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.secret, "")

    def test_delete(self):
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(OrgIntegration.objects.filter(pk=self.integration.pk).exists())

    def test_cannot_access_other_org_integration(self):
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        other_org = Organization.objects.create(
            name="Alien Org",
            product_type=Product_Type.objects.create(name="Alien PT"),
        )
        alien = OrgIntegration.objects.create(
            organization=other_org, integration_type="SLACK", name="Alien Slack",
        )
        url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": alien.pk})
        self.assertEqual(self.client.get(url).status_code, 404)


class OrgIntegrationValidateAPITests(AISTApiBase):

    """POST /integrations/<id>/validate/ and GET /…/<task_id>/"""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Validate Org",
            product_type=self.prod_type,
        )
        self.project.refresh_from_db()

    def _post_validate(self, integration):
        url = reverse("aist_api:org_integration_validate", kwargs={"integration_id": integration.pk})
        fake_result = MagicMock()
        fake_result.id = "fake-task-id"
        with patch("aist.tasks.validate.validate_integration.delay", return_value=fake_result):
            return self.client.post(url)

    def test_validate_returns_202_and_task_id(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="GitHub", config={},
        )
        resp = self._post_validate(integration)
        self.assertEqual(resp.status_code, 202)
        self.assertIn("task_id", resp.data)

    def test_validate_status_pending(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="GitHub", config={},
        )
        url = reverse(
            "aist_api:org_integration_validate_status",
            kwargs={"integration_id": integration.pk, "task_id": "some-task"},
        )
        fake_ar = MagicMock()
        fake_ar.state = "PENDING"
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])

    def test_validate_status_success(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="GitHub", config={},
        )
        url = reverse(
            "aist_api:org_integration_validate_status",
            kwargs={"integration_id": integration.pk, "task_id": "some-task"},
        )
        fake_ar = MagicMock()
        fake_ar.state = "SUCCESS"
        fake_ar.result = {"valid": True, "detail": "", "_integration_id": integration.pk}
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["state"], "SUCCESS")
        self.assertTrue(resp.data["valid"])

    def test_validate_status_failure(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="GitHub", config={},
        )
        url = reverse(
            "aist_api:org_integration_validate_status",
            kwargs={"integration_id": integration.pk, "task_id": "some-task"},
        )
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = {"_integration_id": integration.pk}
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["state"], "FAILURE")
        self.assertFalse(resp.data["valid"])

    def test_validate_status_cross_org_rejected(self):
        """Cannot poll status for an integration from another org."""
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        other_org = Organization.objects.create(
            name="Other Org V",
            product_type=Product_Type.objects.create(name="Other PT V"),
        )
        foreign = OrgIntegration.objects.create(
            organization=other_org, integration_type="GITHUB", name="Foreign GitHub",
        )
        url = reverse(
            "aist_api:org_integration_validate_status",
            kwargs={"integration_id": foreign.pk, "task_id": "task-x"},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


class ProjectIntegrationOverrideAPITests(AISTApiBase):

    """GET /projects/<id>/integration-overrides/ and PUT/DELETE /…/<type>/"""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Override Org",
            product_type=self.prod_type,
        )
        self.project.refresh_from_db()
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="SLACK",
            name="Slack",
            config={"default_channel": "#appsec"},
            secret="xoxb-org-token",  # noqa: S106
        )
        self.list_url = reverse("aist_api:project_integration_overrides", kwargs={"project_id": self.project.pk})
        self.detail_url = reverse(
            "aist_api:project_integration_override_detail",
            kwargs={"project_id": self.project.pk, "integration_type": "SLACK"},
        )

    def test_list_empty_initially(self):
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_writer_can_read_but_cannot_put_or_delete_override(self):
        role_writer, _ = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        Product_Type_Member.objects.filter(product_type=self.project.product.prod_type, user=self.user).update(
            role=role_writer,
        )
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        put = self.client.put(
            self.detail_url,
            {"org_integration": self.integration.pk, "config_override": {}},
            format="json",
        )
        self.assertEqual(put.status_code, 404)
        override = ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="SLACK",
            org_integration=self.integration,
        )
        self.assertEqual(self.client.delete(self.detail_url).status_code, 404)
        self.assertTrue(ProjectIntegrationOverride.objects.filter(pk=override.pk).exists())

    def test_put_creates_override(self):
        resp = self.client.put(self.detail_url, {
            "org_integration": self.integration.pk,
            "config_override": {"channels": ["#my-project"]},
        }, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ProjectIntegrationOverride.objects.count(), 1)
        override = ProjectIntegrationOverride.objects.get()
        self.assertEqual(override.config_override["channels"], ["#my-project"])

    def test_put_upserts_override(self):
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="SLACK",
            org_integration=self.integration,
            config_override={"channels": ["#old"]},
        )
        resp = self.client.put(self.detail_url, {
            "config_override": {"channels": ["#new"]},
        }, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ProjectIntegrationOverride.objects.count(), 1)

    def test_delete_override(self):
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="SLACK",
            org_integration=self.integration,
        )
        resp = self.client.delete(self.detail_url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectIntegrationOverride.objects.exists())

    def test_invalid_type_rejected(self):
        url = reverse(
            "aist_api:project_integration_override_detail",
            kwargs={"project_id": self.project.pk, "integration_type": "UNKNOWN_TYPE"},
        )
        resp = self.client.put(url, {"config_override": {}}, format="json")
        self.assertEqual(resp.status_code, 400)


class CrossOrgDataLeakTests(AISTApiBase):

    """
    User from Org A must never see integrations of a valid Org B project.

    A project whose explicit organization disagrees with its Product_Type is
    now rejected by the database and covered in test_tenant_model_integrity.py.
    """

    def setUp(self):
        super().setUp()
        from dojo.models import Product, Product_Type  # noqa: PLC0415

        from aist.models import AISTProject, Organization  # noqa: PLC0415

        # Org A — user has Maintainer membership (via AISTApiBase.prod_type)
        self.org_a = Organization.objects.create(
            name="Org A", product_type=self.prod_type,
        )

        # Org B — completely separate product_type, user has NO membership
        self.prod_type_b = Product_Type.objects.create(name="PT B")
        self.org_b = Organization.objects.create(
            name="Org B", product_type=self.prod_type_b,
        )

        # Create an integration in Org B
        self.org_b_integration = OrgIntegration.objects.create(
            organization=self.org_b,
            integration_type="SLACK",
            name="Org B Slack",
            config={"default_channel": "#b"},
        )

        product_in_b = Product.objects.create(
            name="Isolated Product",
            description="",
            prod_type=self.prod_type_b,
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=product_in_b,
            supported_languages=[],
            compilable=False,
            profile={},
        )

    def test_user_cannot_list_org_b_integrations(self):
        """User from Org A must not see Org B's integrations via the org B URL."""
        url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org_b.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_user_cannot_access_org_b_integration_detail(self):
        url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": self.org_b_integration.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_user_cannot_create_integration_in_org_b(self):
        url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org_b.pk})
        resp = self.client.post(url, {"integration_type": "GITHUB", "name": "Injected", "config": {}}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_manageable_orgs_endpoint_excludes_org_b(self):
        """GET /organizations/?manage=true must not return Org B."""
        url = reverse("aist_api:organization_create") + "?manage=true"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        results = payload.get("results", payload)
        ids = [o["id"] for o in results]
        self.assertNotIn(self.org_b.pk, ids)
        self.assertIn(self.org_a.pk, ids)


class OrgIntegrationReaderAccessTests(AISTApiBase):

    """Reader-role user must not be able to write integrations (POST/PATCH/DELETE)."""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(name="Reader Test Org", product_type=self.org_prod_type)
        self.project.refresh_from_db()

        # Give the base (Maintainer) user access to this org too so integrations exist
        Product_Type_Member.objects.get_or_create(
            product_type=self.org_prod_type,
            user=self.user,
            defaults={"role": self.role_maintainer},
        )

        # Create a separate Reader user with only Reader role on this org
        role_reader, _ = Role.objects.get_or_create(
            id=Roles.Reader,
            defaults={"name": "Reader"},
        )
        self.reader_user = get_user_model().objects.create_user(
            username="reader_tester",
            email="reader@example.com",
            password="pass",  # noqa: S106
        )
        Product_Type_Member.objects.create(
            product_type=self.org_prod_type,
            user=self.reader_user,
            role=role_reader,
        )
        self.reader_client = APIClient()
        self.reader_client.force_authenticate(user=self.reader_user)

        self.list_url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="SLACK",
            name="Shared Slack",
            config={"default_channel": "#all"},
        )
        self.detail_url = reverse(
            "aist_api:org_integration_detail",
            kwargs={"integration_id": self.integration.pk},
        )

    def test_reader_cannot_list_integrations(self):
        """Reader must not be able to open org integrations even for their own org."""
        resp = self.reader_client.get(self.list_url)
        self.assertEqual(resp.status_code, 404)

    def test_reader_cannot_get_integration_detail(self):
        resp = self.reader_client.get(self.detail_url)
        self.assertEqual(resp.status_code, 404)

    def test_reader_cannot_create_integration(self):
        resp = self.reader_client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "Smuggled GitLab",
            "config": {"base_url": "https://evil.example.com"},
        }, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(OrgIntegration.objects.filter(name="Smuggled GitLab").exists())

    def test_reader_cannot_patch_integration(self):
        resp = self.reader_client.patch(self.detail_url, {"name": "Hijacked"}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.name, "Shared Slack")

    def test_reader_cannot_delete_integration(self):
        resp = self.reader_client.delete(self.detail_url)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(OrgIntegration.objects.filter(pk=self.integration.pk).exists())

    def test_reader_cannot_validate_integration(self):
        url = reverse("aist_api:org_integration_validate", kwargs={"integration_id": self.integration.pk})
        resp = self.reader_client.post(url)
        self.assertEqual(resp.status_code, 404)

    def test_maintainer_can_still_create_integration(self):
        """Regression: existing Maintainer user must not lose write access."""
        resp = self.client.post(self.list_url, {
            "integration_type": "GITHUB",
            "name": "Maintainer GitHub",
            "config": {},
        }, format="json")
        self.assertEqual(resp.status_code, 201)


class IntegrationResolverTests(AISTApiBase):

    """Unit tests for aist.integrations.resolver.resolve_integration"""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Resolver Org",
            product_type=self.prod_type,
        )
        self.project.refresh_from_db()

    def test_returns_none_when_no_integration(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        result = resolve_integration(self.project, OrgIntegrationType.SLACK)
        self.assertIsNone(result)

    def test_returns_org_default(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="SLACK",
            name="Slack",
            secret="xoxb-default",  # noqa: S106
        )
        result = resolve_integration(self.project, OrgIntegrationType.SLACK)
        self.assertIsNotNone(result)
        self.assertEqual(result.integration.pk, integration.pk)
        self.assertEqual(result.integration.secret, "xoxb-default")

    def test_project_override_takes_precedence(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        OrgIntegration.objects.create(
            organization=self.org,
            integration_type="SLACK",
            name="Slack Org",
            config={"default_channel": "#org"},
            secret="xoxb-org",  # noqa: S106
        )
        override_integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="SLACK",
            name="Slack Project",
            config={"default_channel": "#project"},
            secret="xoxb-project",  # noqa: S106
        )
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="SLACK",
            org_integration=override_integration,
            config_override={"channels": ["#my-override"]},
        )
        result = resolve_integration(self.project, OrgIntegrationType.SLACK)
        self.assertEqual(result.integration.pk, override_integration.pk)
        # Config is merged: override_integration.config + config_override
        self.assertEqual(result.config["channels"], ["#my-override"])
        self.assertEqual(result.config["default_channel"], "#project")

    def test_inactive_integration_skipped_in_org_default(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        OrgIntegration.objects.create(
            organization=self.org, integration_type="SLACK", name="Inactive Slack",
            secret="xoxb-inactive", is_active=False,  # noqa: S106
        )
        result = resolve_integration(self.project, OrgIntegrationType.SLACK)
        self.assertIsNone(result)

    def test_returns_none_when_project_has_no_org(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        # An unowned Product_Type has no derivable AIST organization and
        # therefore fails closed. A project under an owned Product_Type cannot
        # be detached from that organization anymore.
        result = resolve_integration(self.other_project, OrgIntegrationType.GITLAB)
        self.assertIsNone(result)


class OrgIntegrationVPNLinkTests(AISTApiBase):

    """Tests for the vpn_integration FK on OrgIntegration."""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(name="VPN Link Org", product_type=self.org_prod_type)
        self.project.refresh_from_db()

        self.vpn = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="VPN",
            name="Corp VPN",
            is_active=True,
        )
        self.list_url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})

    def test_create_gitlab_with_vpn_link(self):
        resp = self.client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "Corp GitLab",
            "config": {"base_url": "https://gitlab.corp.com"},
            "vpn_integration": self.vpn.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["vpn_integration"], self.vpn.pk)
        integration = OrgIntegration.objects.get(name="Corp GitLab")
        self.assertEqual(integration.vpn_integration_id, self.vpn.pk)

    def test_create_gitlab_without_vpn_link(self):
        resp = self.client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "Direct GitLab",
            "config": {},
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.data["vpn_integration"])

    def test_patch_adds_vpn_link(self):
        integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="GITLAB",
            name="GitLab No VPN",
        )
        url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": integration.pk})
        resp = self.client.patch(url, {"vpn_integration": self.vpn.pk}, format="json")
        self.assertEqual(resp.status_code, 200)
        integration.refresh_from_db()
        self.assertEqual(integration.vpn_integration_id, self.vpn.pk)

    def test_patch_removes_vpn_link(self):
        integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="GITLAB",
            name="GitLab With VPN",
            vpn_integration=self.vpn,
        )
        url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": integration.pk})
        resp = self.client.patch(url, {"vpn_integration": None}, format="json")
        self.assertEqual(resp.status_code, 200)
        integration.refresh_from_db()
        self.assertIsNone(integration.vpn_integration_id)

    def test_vpn_integration_not_shown_for_vpn_type(self):
        """VPN integrations must not expose vpn_integration field (they are the VPN)."""
        url = reverse("aist_api:org_integration_detail", kwargs={"integration_id": self.vpn.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("vpn_integration", resp.data)

    def test_cross_org_vpn_rejected(self):
        """VPN integration from a different org must be rejected."""
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        other_org = Organization.objects.create(
            name="Other Org",
            product_type=Product_Type.objects.create(name="Other PT"),
        )
        foreign_vpn = OrgIntegration.objects.create(
            organization=other_org,
            integration_type="VPN",
            name="Foreign VPN",
        )
        resp = self.client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "Smuggled GitLab",
            "config": {},
            "vpn_integration": foreign_vpn.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 400)


class VpnProjectOverrideTests(AISTApiBase):

    """Tests for per-project VPN disable via ProjectIntegrationOverride.is_disabled."""

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(name="VPN Override Org", product_type=self.org_prod_type)
        self.project.refresh_from_db()

        self.vpn = OrgIntegration.objects.create(
            organization=self.org,
            integration_type="VPN",
            name="Corp VPN",
            is_active=True,
        )
        self.detail_url = reverse(
            "aist_api:project_integration_override_detail",
            kwargs={"project_id": self.project.pk, "integration_type": "VPN"},
        )

    # ------------------------------------------------------------------
    # Resolver behaviour
    # ------------------------------------------------------------------

    def test_resolver_returns_org_vpn_when_no_override(self):
        """With no override, resolver should return the org-level VPN."""
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        result = resolve_integration(self.project, OrgIntegrationType.VPN)
        self.assertIsNotNone(result)
        self.assertEqual(result.integration.pk, self.vpn.pk)

    def test_resolver_returns_none_when_is_disabled_true(self):
        """Override with is_disabled=True must suppress org-level VPN."""
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="VPN",
            is_disabled=True,
        )
        result = resolve_integration(self.project, OrgIntegrationType.VPN)
        self.assertIsNone(result)

    def test_resolver_falls_back_to_org_default_when_is_disabled_false(self):
        """Override with is_disabled=False and no org_integration → use org default."""
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="VPN",
            is_disabled=False,
        )
        result = resolve_integration(self.project, OrgIntegrationType.VPN)
        self.assertIsNotNone(result)
        self.assertEqual(result.integration.pk, self.vpn.pk)

    # ------------------------------------------------------------------
    # API behaviour
    # ------------------------------------------------------------------

    def test_put_vpn_override_is_disabled(self):
        """PUT /projects/<id>/integration-overrides/VPN/ with is_disabled=true must persist."""
        resp = self.client.put(self.detail_url, {"is_disabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["is_disabled"])
        override = ProjectIntegrationOverride.objects.get(project=self.project, integration_type="VPN")
        self.assertTrue(override.is_disabled)

    def test_put_vpn_override_re_enable(self):
        """Setting is_disabled=false on an existing disabled override re-enables org VPN."""
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="VPN",
            is_disabled=True,
        )
        resp = self.client.put(self.detail_url, {"is_disabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["is_disabled"])

    def test_delete_vpn_override_restores_org_default(self):
        """DELETE removes the override — resolver falls back to org-level VPN."""
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="VPN",
            is_disabled=True,
        )
        resp = self.client.delete(self.detail_url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectIntegrationOverride.objects.filter(project=self.project, integration_type="VPN").exists())
        result = resolve_integration(self.project, OrgIntegrationType.VPN)
        self.assertIsNotNone(result)
        self.assertEqual(result.integration.pk, self.vpn.pk)

    def test_other_project_in_same_org_unaffected(self):
        """Disabling VPN for one project must not affect other projects in the same org."""
        from dojo.models import Product  # noqa: PLC0415

        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415
        from aist.models import AISTProject  # noqa: PLC0415

        other_product = Product.objects.create(
            name=f"Other Product {self.project.pk}",
            description="",
            prod_type=self.org_prod_type,
            sla_configuration_id=self.sla.id,
        )
        other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type="VPN",
            is_disabled=True,
        )
        result = resolve_integration(other_project, OrgIntegrationType.VPN)
        self.assertIsNotNone(result)
        self.assertEqual(result.integration.pk, self.vpn.pk)


class VpnOrgIsolationAPITests(AISTApiBase):

    """
    H-3: vpn_integration FK must always be validated against the target integration's org.
    The validator must reject cross-org VPN links unconditionally.
    """

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_pt = self.prod_type
        self.org = Organization.objects.create(name="VPN Iso Org", product_type=self.org_pt)
        self.project.refresh_from_db()

        # Own-org VPN
        self.own_vpn = OrgIntegration.objects.create(
            organization=self.org, integration_type="VPN", name="Own VPN",
        )
        # Own-org GitLab
        self.gitlab = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITLAB", name="Own GitLab",
        )
        # Cross-org VPN (user has NO membership in this org)
        other_pt = Product_Type.objects.create(name="Cross Org PT")
        self.other_org = Organization.objects.create(name="Cross Org", product_type=other_pt)
        self.cross_vpn = OrgIntegration.objects.create(
            organization=self.other_org, integration_type="VPN", name="Cross VPN",
        )

        self.list_url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})
        self.gitlab_url = reverse("aist_api:org_integration_detail",
                                  kwargs={"integration_id": self.gitlab.pk})

    def test_cross_org_vpn_link_rejected_on_create(self):
        """Cannot link a VPN integration from another org at creation time."""
        resp = self.client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "Smuggled GitLab",
            "config": {},
            "vpn_integration": self.cross_vpn.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(OrgIntegration.objects.filter(name="Smuggled GitLab").exists())

    def test_cross_org_vpn_link_rejected_on_patch(self):
        """Cannot link a VPN integration from another org via PATCH."""
        resp = self.client.patch(self.gitlab_url, {"vpn_integration": self.cross_vpn.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.gitlab.refresh_from_db()
        self.assertIsNone(self.gitlab.vpn_integration_id)

    def test_same_org_vpn_link_accepted_on_create(self):
        """Linking an own-org VPN at creation must succeed."""
        resp = self.client.post(self.list_url, {
            "integration_type": "SLACK",
            "name": "Slack With VPN",
            "config": {},
            "vpn_integration": self.own_vpn.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["vpn_integration"], self.own_vpn.pk)

    def test_same_org_vpn_link_accepted_on_patch(self):
        """Linking an own-org VPN via PATCH must succeed."""
        resp = self.client.patch(self.gitlab_url, {"vpn_integration": self.own_vpn.pk}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["vpn_integration"], self.own_vpn.pk)

    def test_null_vpn_integration_always_accepted(self):
        """Setting vpn_integration=null must always be allowed (removes the link)."""
        self.gitlab.vpn_integration = self.own_vpn
        self.gitlab.save(update_fields=["vpn_integration"])
        resp = self.client.patch(self.gitlab_url, {"vpn_integration": None}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["vpn_integration"])


class ResolverCrossOrgDefenseTests(AISTApiBase):

    """
    M-2: resolve_integration must log and ignore cross-org overrides even if they
    somehow exist in the DB, and fall back to the org default.
    """

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_pt = self.prod_type
        self.org = Organization.objects.create(name="Resolver Sec Org", product_type=self.org_pt)
        self.project.refresh_from_db()

        # Legitimate org-level VPN
        self.org_vpn = OrgIntegration.objects.create(
            organization=self.org, integration_type="VPN", name="Org VPN", is_active=True,
        )
        # Cross-org VPN (different org)
        other_pt = Product_Type.objects.create(name="CrossOrg PT Res")
        self.other_org = Organization.objects.create(name="CrossOrg Res", product_type=other_pt)
        self.cross_vpn = OrgIntegration.objects.create(
            organization=self.other_org, integration_type="VPN", name="Cross VPN",
        )

    def test_cross_org_override_is_rejected_and_default_remains_effective(self):
        from aist.integrations.resolver import resolve_integration  # noqa: PLC0415

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectIntegrationOverride.objects.create(
                project=self.project,
                integration_type="VPN",
                org_integration=self.cross_vpn,
            )

        result = resolve_integration(self.project, OrgIntegrationType.VPN)
        # Must NOT return the cross-org integration
        self.assertIsNotNone(result)
        self.assertNotEqual(result.integration.pk, self.cross_vpn.pk)
        self.assertEqual(result.integration.pk, self.org_vpn.pk)


class ValidateStatusTaskBindingTests(AISTApiBase):

    """
    M-4: validate status endpoint must verify task_id → integration_id binding
    in FAILURE state, just as it does in SUCCESS state.
    """

    def setUp(self):
        super().setUp()

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Binding Test Org",
            product_type=self.prod_type,
        )
        self.project.refresh_from_db()

        self.my_integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="My GitHub",
        )

    def _status_url(self, integration_id, task_id):
        return reverse(
            "aist_api:org_integration_validate_status",
            kwargs={"integration_id": integration_id, "task_id": task_id},
        )

    def test_failure_with_matching_integration_id_returned(self):
        """FAILURE result for the correct integration must be returned."""
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = {"_integration_id": self.my_integration.pk}
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.my_integration.pk, "task-mine"))
        self.assertEqual(resp.data["state"], "FAILURE")
        self.assertFalse(resp.data["valid"])

    def test_failure_with_wrong_integration_id_returns_pending(self):
        """FAILURE of task_id belonging to integration 999 must not be exposed for another."""
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = {"_integration_id": 999999}  # different integration
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.my_integration.pk, "task-other"))
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])

    def test_failure_with_non_dict_result_returns_pending(self):
        """If task meta is a plain exception (old task format), treat as PENDING."""
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = Exception("something went wrong")
        with patch("celery.result.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.my_integration.pk, "task-exc"))
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])


# ---------------------------------------------------------------------------
# WorkItemProvider validate — async + VPN routing
# ---------------------------------------------------------------------------

class WorkItemProviderValidateAPITests(AISTApiBase):

    """
    POST /work-item-providers/<id>/validate/ must return 202 + task_id.
    GET  /work-item-providers/<id>/validate/<task_id>/ must poll result with
    provider_id binding to prevent cross-task disclosure.
    """

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization, WorkItemProvider  # noqa: PLC0415

        self.org_pt = Product_Type.objects.create(name="WIP Validate PT")
        self.org = Organization.objects.create(name="WIP Validate Org", product_type=self.org_pt)
        from dojo.models import Product_Type_Member  # noqa: PLC0415
        Product_Type_Member.objects.create(
            product_type=self.org_pt, user=self.user, role=self.role_maintainer,
        )
        self.provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type="JIRA",
            name="Test Jira",
            base_url="https://jira.example.com",
        )

    def _validate_url(self, provider_id: int) -> str:
        return reverse("aist_api:work_item_provider_validate", kwargs={"provider_id": provider_id})

    def _status_url(self, provider_id: int, task_id: str) -> str:
        return reverse(
            "aist_api:work_item_provider_validate_status",
            kwargs={"provider_id": provider_id, "task_id": task_id},
        )

    @patch("aist.tasks.validate.validate_work_item_provider.delay")
    def test_post_returns_202_with_task_id(self, mock_delay):
        mock_delay.return_value = MagicMock(id="test-task-id")
        resp = self.client.post(self._validate_url(self.provider.pk))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["task_id"], "test-task-id")
        mock_delay.assert_called_once_with(self.provider.pk)

    def test_status_success_matching_provider_id(self):
        fake_ar = MagicMock()
        fake_ar.state = "SUCCESS"
        fake_ar.result = {"valid": True, "detail": "", "_provider_id": self.provider.pk}
        with patch("aist.api.work_items.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.provider.pk, "task-ok"))
        self.assertEqual(resp.data["state"], "SUCCESS")
        self.assertTrue(resp.data["valid"])

    def test_status_success_wrong_provider_id_returns_pending(self):
        """A task_id belonging to a different provider must not reveal its result."""
        fake_ar = MagicMock()
        fake_ar.state = "SUCCESS"
        fake_ar.result = {"valid": False, "detail": "bad creds", "_provider_id": 999999}
        with patch("aist.api.work_items.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.provider.pk, "task-other"))
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])

    def test_status_failure_matching_provider_id(self):
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = {"_provider_id": self.provider.pk}
        with patch("aist.api.work_items.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.provider.pk, "task-fail"))
        self.assertEqual(resp.data["state"], "FAILURE")
        self.assertFalse(resp.data["valid"])

    def test_status_failure_wrong_provider_id_returns_pending(self):
        fake_ar = MagicMock()
        fake_ar.state = "FAILURE"
        fake_ar.result = {"_provider_id": 999999}
        with patch("aist.api.work_items.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.provider.pk, "task-other-fail"))
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])

    def test_status_pending(self):
        fake_ar = MagicMock()
        fake_ar.state = "PENDING"
        fake_ar.result = None
        with patch("aist.api.work_items.AsyncResult", return_value=fake_ar):
            resp = self.client.get(self._status_url(self.provider.pk, "task-pending"))
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertIsNone(resp.data["valid"])


class ValidateWorkItemProviderHelperTests(AISTApiBase):

    """
    _validate_work_item_provider helper: VPN sidecar started when vpn_integration
    is configured; sanitized error on exception; NotImplementedError handled cleanly.
    """

    def _make_provider(self, vpn_integration=None):
        from types import SimpleNamespace  # noqa: PLC0415
        return SimpleNamespace(
            pk=42,
            provider_type="JIRA",
            vpn_integration=vpn_integration,
        )

    @patch("aist.api.work_items.get_backend")
    def test_no_vpn_backend_validate_credentials_called(self, mock_get_backend):
        """When no VPN is configured, validate_credentials() is called via scoped_context."""
        from contextlib import contextmanager  # noqa: PLC0415
        mock_backend = MagicMock()
        mock_backend.validate_credentials.return_value = True

        @contextmanager
        def _scoped_ctx(execution_id):
            mock_backend.proxy_url = None
            yield mock_backend

        mock_backend.scoped_context.side_effect = _scoped_ctx
        mock_get_backend.return_value = mock_backend

        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415
        valid, detail = _validate_work_item_provider(self._make_provider())
        self.assertTrue(valid)
        self.assertEqual(detail, "")
        mock_backend.validate_credentials.assert_called_once()
        # get_backend must NOT receive proxy_url — proxy is set via scoped_context
        _, kwargs = mock_get_backend.call_args
        self.assertNotIn("proxy_url", kwargs)

    @patch("aist.api.work_items.get_backend")
    def test_vpn_proxy_url_set_on_backend_via_scoped_context(self, mock_get_backend):
        """When VPN is configured, scoped_context sets proxy_url on the backend."""
        from contextlib import contextmanager  # noqa: PLC0415
        mock_backend = MagicMock()
        mock_backend.validate_credentials.return_value = True

        @contextmanager
        def _scoped_ctx(execution_id):
            mock_backend.proxy_url = "http://172.19.0.11:1080"
            yield mock_backend

        mock_backend.scoped_context.side_effect = _scoped_ctx
        mock_get_backend.return_value = mock_backend

        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415
        valid, _ = _validate_work_item_provider(self._make_provider())
        self.assertTrue(valid)
        mock_backend.validate_credentials.assert_called_once()

    @patch("aist.api.work_items.get_backend")
    def test_not_implemented_error_returns_false_with_message(self, mock_get_backend):
        mock_get_backend.side_effect = NotImplementedError

        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415
        valid, detail = _validate_work_item_provider(self._make_provider())
        self.assertFalse(valid)
        self.assertIn("not support", detail)

    @patch("aist.api.work_items.get_backend")
    def test_generic_exception_returns_sanitized_message(self, mock_get_backend):
        """Exceptions must not leak raw exception details to the caller."""
        from contextlib import contextmanager  # noqa: PLC0415
        mock_backend = MagicMock()
        mock_backend.validate_credentials.side_effect = RuntimeError("connection refused to 10.0.0.1")

        @contextmanager
        def _scoped_ctx(execution_id):
            mock_backend.proxy_url = None
            yield mock_backend

        mock_backend.scoped_context.side_effect = _scoped_ctx
        mock_get_backend.return_value = mock_backend

        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415
        valid, detail = _validate_work_item_provider(self._make_provider())
        self.assertFalse(valid)
        # Must NOT expose raw exception message with internal IP
        self.assertNotIn("10.0.0.1", detail)
        self.assertIn("see server logs", detail)
