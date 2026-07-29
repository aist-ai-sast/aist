from __future__ import annotations

from unittest.mock import patch

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
from rest_framework.test import APIClient

from aist.models import (
    AISTProject,
    Organization,
    WorkItemLink,
    WorkItemProvider,
    WorkItemProviderType,
    WorkItemStatusCategory,
)


def _make_finding(test, title="Vuln", severity="High"):
    return Finding.objects.create(
        test=test,
        title=title,
        severity=severity,
        date=timezone.now(),
        reporter=test.engagement.product.prod_type.authorized_users.first()
        if hasattr(test.engagement.product.prod_type, "authorized_users")
        else Finding._meta.get_field("reporter").default
        and None,
    )


class WorkItemBaseTestCase(TestCase):

    """Shared setup: org, product, project, finding, authorized user."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="wi_user",
            email="wi@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA wi")
        self.prod_type = Product_Type.objects.create(name="PT wi")
        self.role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=self.role)

        self.org = Organization.objects.create(name="Org WI", product_type=self.prod_type)

        self.product = Product.objects.create(
            name="WI Product",
            description="",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )

        test_type = Test_Type.objects.create(name="WI test type")
        self.engagement = Engagement.objects.create(
            name="WI engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=self.test,
            title="SQL Injection",
            severity="Critical",
            date=timezone.now(),
            reporter=self.user,
        )

    # URL helpers
    def _providers_url(self, org_id=None):
        return reverse("aist_api:work_item_provider_list_create", kwargs={"org_id": org_id or self.org.pk})

    def _provider_detail_url(self, provider_id):
        return reverse("aist_api:work_item_provider_detail", kwargs={"provider_id": provider_id})

    def _links_url(self, finding_id=None):
        return reverse("aist_api:finding_work_item_list_create", kwargs={"finding_id": finding_id or self.finding.pk})

    def _link_detail_url(self, link_id, finding_id=None):
        return reverse(
            "aist_api:finding_work_item_detail",
            kwargs={"finding_id": finding_id or self.finding.pk, "link_id": link_id},
        )


# ---------------------------------------------------------------------------
# Provider tests
# ---------------------------------------------------------------------------


class WorkItemProviderCRUDTests(WorkItemBaseTestCase):

    def test_list_providers_empty(self):
        response = self.client.get(self._providers_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_create_provider_generic(self):
        payload = {
            "provider_type": WorkItemProviderType.GENERIC,
            "name": "Company Tracker",
            "base_url": "https://tracker.example.com",
            "sync_enabled": False,
        }
        response = self.client.post(self._providers_url(), payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Company Tracker")
        self.assertNotIn("api_token", response.data)  # write_only
        self.assertFalse(response.data["has_token"])

    def test_create_provider_with_token(self):
        payload = {
            "provider_type": WorkItemProviderType.JIRA,
            "name": "Company Jira",
            "base_url": "https://jira.example.com",
            "api_token": "secret-jira-token",
            "sync_enabled": True,
        }
        response = self.client.post(self._providers_url(), payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["has_token"])
        provider = WorkItemProvider.objects.get(pk=response.data["id"])
        self.assertEqual(provider.api_token, "secret-jira-token")

    def test_list_providers_returns_created(self):
        WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.GITHUB,
            name="GitHub Issues",
        )
        response = self.client.get(self._providers_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "GitHub Issues")

    def test_retrieve_provider(self):
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.GITLAB,
            name="GitLab Issues",
        )
        response = self.client.get(self._provider_detail_url(provider.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], provider.pk)

    def test_patch_provider_name(self):
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.LINEAR,
            name="Linear Old",
        )
        response = self.client.patch(self._provider_detail_url(provider.pk), {"name": "Linear New"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Linear New")

    def test_patch_provider_preserves_token_when_omitted(self):
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Jira",
            api_token="original-token",  # noqa: S106
        )
        response = self.client.patch(self._provider_detail_url(provider.pk), {"name": "Jira Updated"}, format="json")
        self.assertEqual(response.status_code, 200)
        provider.refresh_from_db()
        self.assertEqual(provider.api_token, "original-token")

    def test_delete_provider(self):
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.GITHUB,
            name="To delete",
        )
        response = self.client.delete(self._provider_detail_url(provider.pk))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(WorkItemProvider.objects.filter(pk=provider.pk).exists())

    def test_delete_provider_preserves_links(self):
        """Deleting a provider keeps WorkItemLinks but sets provider=None."""
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Jira to delete",
        )
        link = WorkItemLink.objects.create(
            finding=self.finding,
            provider=provider,
            external_key="SEC-1",
            external_url="https://jira.example.com/browse/SEC-1",
        )
        self.client.delete(self._provider_detail_url(provider.pk))
        link.refresh_from_db()
        self.assertIsNone(link.provider)

    def test_unauthorized_user_cannot_access_providers(self):
        other_user = get_user_model().objects.create_user(
            username="wi_stranger", email="stranger@example.com", password="x",  # noqa: S106
        )
        self.client.force_authenticate(user=other_user)
        WorkItemProvider.objects.create(
            organization=self.org, provider_type=WorkItemProviderType.GENERIC, name="secret",
        )
        response = self.client.get(self._providers_url())
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Link tests
# ---------------------------------------------------------------------------


class WorkItemLinkCRUDTests(WorkItemBaseTestCase):

    def _set_role(self, role_id, name):
        role, _ = Role.objects.get_or_create(id=role_id, defaults={"name": name})
        Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).update(role=role)

    def test_reader_can_list_but_cannot_create_patch_or_delete_links(self):
        self._set_role(Roles.Reader, "Reader")
        self.assertEqual(self.client.get(self._links_url()).status_code, 200)
        create = self.client.post(
            self._links_url(),
            {"external_url": "https://example.com/reader"},
            format="json",
        )
        self.assertEqual(create.status_code, 404)
        link = WorkItemLink.objects.create(finding=self.finding, external_url="https://example.com/existing")
        self.assertEqual(
            self.client.patch(
                self._link_detail_url(link.pk),
                {"status_category": WorkItemStatusCategory.DONE},
                format="json",
            ).status_code,
            404,
        )
        self.assertEqual(self.client.delete(self._link_detail_url(link.pk)).status_code, 404)

    def test_writer_can_create_patch_and_delete_manual_link(self):
        self._set_role(Roles.Writer, "Writer")
        created = self.client.post(
            self._links_url(),
            {"external_url": "https://example.com/writer"},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        link_id = created.data["id"]
        patched = self.client.patch(
            self._link_detail_url(link_id),
            {"status_category": WorkItemStatusCategory.DONE},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(self.client.delete(self._link_detail_url(link_id)).status_code, 204)

    @patch("aist.api.work_items.sync_work_item_link.delay")
    def test_writer_can_create_link_with_visible_provider(self, mock_sync):
        self._set_role(Roles.Writer, "Writer")
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Writer-visible Jira",
        )

        created = self.client.post(
            self._links_url(),
            {
                "provider": provider.pk,
                "external_key": "SEC-7",
                "external_url": "https://jira.example.com/browse/SEC-7",
            },
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["provider"], provider.pk)
        mock_sync.assert_called_once()

    def test_writer_cannot_resolve_provider_from_another_organization(self):
        self._set_role(Roles.Writer, "Writer")
        other_product_type = Product_Type.objects.create(name="Other WI PT")
        other_organization = Organization.objects.create(
            name="Other WI Org",
            product_type=other_product_type,
        )
        other_provider = WorkItemProvider.objects.create(
            organization=other_organization,
            provider_type=WorkItemProviderType.JIRA,
            name="Other Jira",
        )

        response = self.client.post(
            self._links_url(),
            {
                "provider": other_provider.pk,
                "external_key": "OTHER-1",
                "external_url": "https://jira.example.com/browse/OTHER-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("provider", response.data)
        self.assertFalse(WorkItemLink.objects.filter(finding=self.finding).exists())

    def test_list_links_empty(self):
        response = self.client.get(self._links_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_create_manual_link(self):
        payload = {
            "external_key": "SEC-42",
            "external_url": "https://jira.example.com/browse/SEC-42",
            "title": "Fix SQL injection",
        }
        response = self.client.post(self._links_url(), payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["external_key"], "SEC-42")
        self.assertIsNone(response.data["provider"])
        link = WorkItemLink.objects.get(pk=response.data["id"])
        self.assertEqual(link.finding_id, self.finding.pk)
        self.assertEqual(link.created_by, self.user)

    def test_create_link_without_url_fails(self):
        payload = {"external_key": "NO-URL"}
        response = self.client.post(self._links_url(), payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("external_url", response.data)

    @patch("aist.api.work_items.sync_work_item_link.delay")
    def test_create_link_with_provider(self, mock_sync):
        provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Jira provider",
        )
        payload = {
            "provider": provider.pk,
            "external_id": "10042",
            "external_key": "PROJ-42",
            "external_url": "https://jira.example.com/browse/PROJ-42",
            "title": "Fix vuln",
        }
        response = self.client.post(self._links_url(), payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["provider"], provider.pk)
        self.assertEqual(response.data["provider_name"], "Jira provider")
        mock_sync.assert_called_once()

    def test_duplicate_manual_link_same_key_is_rejected(self):
        WorkItemLink.objects.create(
            finding=self.finding,
            provider=None,
            external_key="DUP-1",
            external_url="https://example.com/DUP-1",
        )
        payload = {
            "external_key": "DUP-1",
            "external_url": "https://example.com/DUP-1",
        }
        response = self.client.post(self._links_url(), payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("external_key", response.data)

    def test_finding_can_have_links_in_multiple_providers(self):
        """One finding → Jira link + GitHub link is allowed."""
        provider_jira = WorkItemProvider.objects.create(
            organization=self.org, provider_type=WorkItemProviderType.JIRA, name="Jira",
        )
        provider_gh = WorkItemProvider.objects.create(
            organization=self.org, provider_type=WorkItemProviderType.GITHUB, name="GitHub",
        )
        WorkItemLink.objects.create(
            finding=self.finding, provider=provider_jira,
            external_key="SEC-1", external_url="https://jira.example.com/browse/SEC-1",
        )
        WorkItemLink.objects.create(
            finding=self.finding, provider=provider_gh,
            external_key="42", external_url="https://github.com/org/repo/issues/42",
        )
        response = self.client.get(self._links_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_patch_link_updates_status(self):
        link = WorkItemLink.objects.create(
            finding=self.finding,
            external_key="UP-1",
            external_url="https://example.com/UP-1",
        )
        payload = {
            "raw_status": "In Review",
            "status_category": WorkItemStatusCategory.IN_PROGRESS,
        }
        response = self.client.patch(self._link_detail_url(link.pk), payload, format="json")
        self.assertEqual(response.status_code, 200)
        link.refresh_from_db()
        self.assertEqual(link.status_category, WorkItemStatusCategory.IN_PROGRESS)
        self.assertEqual(link.raw_status, "In Review")

    def test_delete_link(self):
        link = WorkItemLink.objects.create(
            finding=self.finding,
            external_url="https://example.com/del",
        )
        response = self.client.delete(self._link_detail_url(link.pk))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(WorkItemLink.objects.filter(pk=link.pk).exists())

    def test_unauthorized_user_cannot_access_finding_links(self):
        other_user = get_user_model().objects.create_user(
            username="wi_stranger2", email="stranger2@example.com", password="x",  # noqa: S106
        )
        self.client.force_authenticate(user=other_user)
        response = self.client.get(self._links_url())
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# has_work_item filter tests
# ---------------------------------------------------------------------------


class HasWorkItemFilterTests(WorkItemBaseTestCase):

    def _findings_url(self):
        return reverse("aist_api:finding_list")

    def setUp(self):
        super().setUp()
        # Second finding without a link
        self.finding_no_link = Finding.objects.create(
            test=self.test,
            title="XSS",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

    def test_filter_work_item_status_any_returns_linked_findings(self):
        WorkItemLink.objects.create(
            finding=self.finding,
            external_url="https://example.com/f1",
        )
        response = self.client.get(self._findings_url(), {"work_item_status": "any"})
        self.assertEqual(response.status_code, 200)
        ids = [f["id"] for f in response.data["results"]]
        self.assertIn(self.finding.pk, ids)
        self.assertNotIn(self.finding_no_link.pk, ids)

    def test_filter_work_item_status_none_returns_unlinked_findings(self):
        WorkItemLink.objects.create(
            finding=self.finding,
            external_url="https://example.com/f1",
        )
        response = self.client.get(self._findings_url(), {"work_item_status": "none"})
        self.assertEqual(response.status_code, 200)
        ids = [f["id"] for f in response.data["results"]]
        self.assertNotIn(self.finding.pk, ids)
        self.assertIn(self.finding_no_link.pk, ids)

    def test_finding_with_link_has_work_items_in_response(self):
        WorkItemLink.objects.create(
            finding=self.finding,
            external_key="INLINE-1",
            external_url="https://example.com/INLINE-1",
            title="Inline ticket",
            status_category=WorkItemStatusCategory.OPEN,
        )
        response = self.client.get(self._findings_url(), {"id": self.finding.pk})
        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        finding_data = next(f for f in results if f["id"] == self.finding.pk)
        self.assertEqual(len(finding_data["work_items"]), 1)
        self.assertEqual(finding_data["work_items"][0]["external_key"], "INLINE-1")
        self.assertEqual(finding_data["work_items"][0]["status_category"], WorkItemStatusCategory.OPEN)

    def test_finding_without_link_has_empty_work_items(self):
        response = self.client.get(self._findings_url(), {"id": self.finding_no_link.pk})
        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        finding_data = next(f for f in results if f["id"] == self.finding_no_link.pk)
        self.assertEqual(finding_data["work_items"], [])

    def test_filter_work_item_status_by_category_returns_matching_findings(self):
        # finding has an OPEN work item; finding_no_link has a DONE work item
        WorkItemLink.objects.create(
            finding=self.finding,
            external_url="https://example.com/f-open",
            status_category=WorkItemStatusCategory.OPEN,
        )
        WorkItemLink.objects.create(
            finding=self.finding_no_link,
            external_url="https://example.com/f-done",
            status_category=WorkItemStatusCategory.DONE,
        )

        response = self.client.get(self._findings_url(), {"work_item_status": "OPEN"})
        self.assertEqual(response.status_code, 200)
        ids = [f["id"] for f in response.data["results"]]
        self.assertIn(self.finding.pk, ids)
        self.assertNotIn(self.finding_no_link.pk, ids)

    def test_filter_work_item_status_all_returns_all_findings(self):
        WorkItemLink.objects.create(
            finding=self.finding,
            external_url="https://example.com/f-open",
            status_category=WorkItemStatusCategory.OPEN,
        )

        response = self.client.get(self._findings_url(), {"work_item_status": "all"})
        self.assertEqual(response.status_code, 200)
        ids = [f["id"] for f in response.data["results"]]
        self.assertIn(self.finding.pk, ids)
        self.assertIn(self.finding_no_link.pk, ids)
