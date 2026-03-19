from __future__ import annotations

from django.contrib.auth import get_user_model
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
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = Product_Type.objects.create(name="Org PT")
        self.org = Organization.objects.create(name="Test Org", product_type=self.org_prod_type)
        Product_Type_Member.objects.create(
            product_type=self.org_prod_type,
            user=self.user,
            role=self.role_maintainer,
        )
        # Link project to org
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
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
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = Product_Type.objects.create(name="Org PT2")
        self.org = Organization.objects.create(name="Detail Org", product_type=self.org_prod_type)
        Product_Type_Member.objects.create(
            product_type=self.org_prod_type,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
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

    """POST /integrations/<id>/validate/"""

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Validate Org",
            product_type=Product_Type.objects.create(name="Validate PT"),
        )
        Product_Type_Member.objects.create(
            product_type=self.org.product_type,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])

    def test_github_always_valid(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="GITHUB", name="GitHub", config={},
        )
        url = reverse("aist_api:org_integration_validate", kwargs={"integration_id": integration.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["valid"])

    def test_email_always_valid(self):
        integration = OrgIntegration.objects.create(
            organization=self.org, integration_type="EMAIL", name="Email", config={},
        )
        url = reverse("aist_api:org_integration_validate", kwargs={"integration_id": integration.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["valid"])


class ProjectIntegrationOverrideAPITests(AISTApiBase):

    """GET /projects/<id>/integration-overrides/ and PUT/DELETE /…/<type>/"""

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Override Org",
            product_type=Product_Type.objects.create(name="Override PT"),
        )
        Product_Type_Member.objects.create(
            product_type=self.org.product_type,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
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
    User from Org A must never see integrations of Org B,
    even if a project in Org B happens to have a product in Org A's product_type.
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

        # Anomaly: a product in Prod_Type A is used by a project assigned to Org B
        product_in_a = Product.objects.create(
            name="Leaked Product",
            description="",
            prod_type=self.prod_type,  # Org A's product_type!
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=product_in_a,
            organization=self.org_b,  # but assigned to Org B
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
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = Product_Type.objects.create(name="Reader Test PT")
        self.org = Organization.objects.create(name="Reader Test Org", product_type=self.org_prod_type)
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])

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
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org = Organization.objects.create(
            name="Resolver Org",
            product_type=Product_Type.objects.create(name="Resolver PT"),
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])

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

        self.project.organization = None
        self.project.save(update_fields=["organization"])
        result = resolve_integration(self.project, OrgIntegrationType.GITLAB)
        self.assertIsNone(result)
