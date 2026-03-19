from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Product_Type, Test, Test_Type

from aist.models import Organization, WorkItemProvider, WorkItemProviderType
from aist.test.test_api import AISTApiBase


class WorkItemProviderAPIBase(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.org_prod_type = Product_Type.objects.create(name="WIP Org PT")
        self.org = Organization.objects.create(name="WIP Org", product_type=self.org_prod_type)
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
        self.list_url = reverse("aist_api:work_item_provider_list_create", kwargs={"org_id": self.org.pk})

    def _detail_url(self, provider_id: int) -> str:
        return reverse("aist_api:work_item_provider_detail", kwargs={"provider_id": provider_id})

    def _validate_url(self, provider_id: int) -> str:
        return reverse("aist_api:work_item_provider_validate", kwargs={"provider_id": provider_id})

    def _sync_url(self, provider_id: int) -> str:
        return reverse("aist_api:work_item_provider_sync", kwargs={"provider_id": provider_id})

    def _create_provider(self, **kwargs) -> WorkItemProvider:
        defaults = {
            "organization": self.org,
            "provider_type": WorkItemProviderType.JIRA,
            "name": "Test Jira",
            "base_url": "https://jira.example.com",
            "sync_enabled": True,
        }
        defaults.update(kwargs)
        return WorkItemProvider.objects.create(**defaults)


# ---------------------------------------------------------------------------
# List / Create
# ---------------------------------------------------------------------------


class WorkItemProviderListCreateAPITests(WorkItemProviderAPIBase):

    def test_list_empty(self):
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_list_returns_org_providers(self):
        self._create_provider(name="Jira Cloud")
        self._create_provider(name="YouTrack", provider_type=WorkItemProviderType.YOUTRACK)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_list_does_not_leak_other_org_providers(self):
        other_prod_type = Product_Type.objects.create(name="Other PT")
        other_org = Organization.objects.create(name="Other Org", product_type=other_prod_type)
        WorkItemProvider.objects.create(
            organization=other_org,
            provider_type=WorkItemProviderType.JIRA,
            name="Other Jira",
        )
        self._create_provider(name="Our Jira")
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["name"], "Our Jira")

    def test_create_provider(self):
        payload = {
            "provider_type": "JIRA",
            "name": "My Jira",
            "base_url": "https://jira.example.com",
            "api_token": "secret-token",
            "sync_enabled": True,
        }
        resp = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["name"], "My Jira")
        self.assertEqual(resp.data["provider_type"], "JIRA")
        self.assertTrue(resp.data["has_token"])
        self.assertNotIn("api_token", resp.data)

    def test_create_requires_provider_type(self):
        resp = self.client.post(self.list_url, data={"name": "No Type"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("provider_type", resp.data)

    def test_create_requires_name(self):
        resp = self.client.post(self.list_url, data={"provider_type": "JIRA"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.data)

    def test_unauthenticated_list_rejected(self):
        from rest_framework.test import APIClient  # noqa: PLC0415

        resp = APIClient().get(self.list_url)
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Detail (GET / PATCH / DELETE)
# ---------------------------------------------------------------------------


class WorkItemProviderDetailAPITests(WorkItemProviderAPIBase):

    def test_get_provider(self):
        provider = self._create_provider()
        resp = self.client.get(self._detail_url(provider.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], provider.pk)
        self.assertEqual(resp.data["provider_type"], "JIRA")

    def test_api_token_not_returned(self):
        provider = self._create_provider()
        provider.api_token = "xoxb-secret"  # noqa: S105
        provider.save(update_fields=["api_token"])
        resp = self.client.get(self._detail_url(provider.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("api_token", resp.data)
        self.assertTrue(resp.data["has_token"])

    def test_patch_name(self):
        provider = self._create_provider()
        resp = self.client.patch(self._detail_url(provider.pk), data={"name": "Renamed"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "Renamed")

    def test_patch_token_updates_stored_token(self):
        provider = self._create_provider()
        resp = self.client.patch(
            self._detail_url(provider.pk),
            data={"api_token": "new-token"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        provider.refresh_from_db()
        self.assertEqual(provider.api_token, "new-token")

    def test_patch_without_token_preserves_existing(self):
        provider = self._create_provider()
        provider.api_token = "keep-me"  # noqa: S105
        provider.save(update_fields=["api_token"])
        self.client.patch(self._detail_url(provider.pk), data={"name": "Patched"}, format="json")
        provider.refresh_from_db()
        self.assertEqual(provider.api_token, "keep-me")

    def test_delete_provider(self):
        provider = self._create_provider()
        resp = self.client.delete(self._detail_url(provider.pk))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(WorkItemProvider.objects.filter(pk=provider.pk).exists())

    def test_detail_for_other_org_returns_404(self):
        other_prod_type = Product_Type.objects.create(name="Isolated PT")
        other_org = Organization.objects.create(name="Isolated Org", product_type=other_prod_type)
        other_provider = WorkItemProvider.objects.create(
            organization=other_org,
            provider_type=WorkItemProviderType.JIRA,
            name="Isolated Jira",
        )
        resp = self.client.get(self._detail_url(other_provider.pk))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class WorkItemProviderValidateAPITests(WorkItemProviderAPIBase):

    def test_validate_returns_true_when_backend_succeeds(self):
        provider = self._create_provider()
        with patch("aist.api.work_items.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.validate_credentials.return_value = True
            mock_get_backend.return_value = mock_backend
            resp = self.client.post(self._validate_url(provider.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["valid"])
        self.assertIn("detail", resp.data)

    def test_validate_returns_false_with_detail_when_backend_fails(self):
        provider = self._create_provider()
        with patch("aist.api.work_items.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.validate_credentials.return_value = False
            mock_get_backend.return_value = mock_backend
            resp = self.client.post(self._validate_url(provider.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["valid"])
        self.assertIn("detail", resp.data)
        self.assertTrue(resp.data["detail"])

    def test_validate_no_backend_returns_false_with_detail(self):
        provider = self._create_provider()
        with patch("aist.api.work_items.get_backend", side_effect=NotImplementedError):
            resp = self.client.post(self._validate_url(provider.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["valid"])
        self.assertIn("detail", resp.data)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class WorkItemProviderSyncAPITests(WorkItemProviderAPIBase):

    def test_sync_queues_task_and_returns_202(self):
        provider = self._create_provider()
        with patch("aist.tasks.work_items.sync_work_item_provider.delay") as mock_delay:
            resp = self.client.post(self._sync_url(provider.pk))
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.data["queued"])
        mock_delay.assert_called_once_with(provider.pk)

    def test_sync_for_other_org_returns_404(self):
        other_prod_type = Product_Type.objects.create(name="Isolated PT 2")
        other_org = Organization.objects.create(name="Isolated Org 2", product_type=other_prod_type)
        other_provider = WorkItemProvider.objects.create(
            organization=other_org,
            provider_type=WorkItemProviderType.JIRA,
            name="Isolated Jira 2",
        )
        resp = self.client.post(self._sync_url(other_provider.pk))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Work item auto-detection
# ---------------------------------------------------------------------------


class WorkItemAutoProviderDetectionTests(WorkItemProviderAPIBase):

    """Creating a work item link auto-detects provider from URL hostname."""

    def setUp(self):
        super().setUp()
        test_type = Test_Type.objects.create(name="Auto-detect Test Type")
        engagement = Engagement.objects.create(
            name="Auto-detect Engagement",
            product=self.product,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=test,
            title="Auto-detect finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

    def _work_items_url(self) -> str:
        return reverse(
            "aist_api:finding_work_item_list_create",
            kwargs={"finding_id": self.finding.pk},
        )

    def test_provider_auto_detected_from_url(self):
        """When external_url hostname matches provider base_url, provider is auto-linked."""
        provider = self._create_provider(base_url="https://jira.example.com")
        resp = self.client.post(
            self._work_items_url(),
            data={"external_url": "https://jira.example.com/browse/PROJ-1"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["provider"], provider.pk)

    def test_no_auto_detection_when_no_matching_provider(self):
        """When hostname doesn't match any provider, link is created without provider."""
        self._create_provider(base_url="https://other.example.com")
        resp = self.client.post(
            self._work_items_url(),
            data={"external_url": "https://different-host.example.com/browse/PROJ-1"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(resp.data["provider"])


# ---------------------------------------------------------------------------
# Work item manual status update
# ---------------------------------------------------------------------------


class WorkItemManualStatusUpdateTests(WorkItemAutoProviderDetectionTests):

    """PATCH status_category on a provider-less link updates the stored value."""

    def _detail_url_for_link(self, link_id: int) -> str:
        return reverse(
            "aist_api:finding_work_item_detail",
            kwargs={"finding_id": self.finding.pk, "link_id": link_id},
        )

    def _create_link(self, **kwargs):
        from aist.models import WorkItemLink  # noqa: PLC0415

        defaults = {
            "finding": self.finding,
            "external_url": "https://tracker.example.com/ISSUE-1",
            "created_by": self.user,
        }
        defaults.update(kwargs)
        return WorkItemLink.objects.create(**defaults)

    def test_patch_status_category_updates_value(self):
        link = self._create_link()
        resp = self.client.patch(
            self._detail_url_for_link(link.pk),
            data={"status_category": "DONE"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        link.refresh_from_db()
        self.assertEqual(link.status_category, "DONE")

    def test_patch_status_category_invalid_value_rejected(self):
        link = self._create_link()
        resp = self.client.patch(
            self._detail_url_for_link(link.pk),
            data={"status_category": "NOT_A_STATUS"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_status_category_nonexistent_link_returns_404(self):
        resp = self.client.patch(
            self._detail_url_for_link(999999),
            data={"status_category": "DONE"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
