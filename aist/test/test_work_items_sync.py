from __future__ import annotations

from unittest.mock import MagicMock, patch

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
from aist.work_items.backends.base import RemoteIssueInfo, WorkItemSyncError
from aist.work_items.backends.registry import get_backend, has_backend, register_backend
from aist.work_items.sync import ProviderSyncResult, sync_link, sync_provider

# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class BackendRegistryTests(TestCase):

    def test_has_backend_for_known_types(self):
        # Importing the package triggers registration of all backends
        import aist.work_items.backends  # noqa: F401,PLC0415

        self.assertTrue(has_backend("JIRA"))
        self.assertTrue(has_backend("GITHUB"))
        self.assertTrue(has_backend("GITLAB"))

    def test_has_no_backend_for_generic(self):
        import aist.work_items.backends  # noqa: F401,PLC0415

        self.assertFalse(has_backend("GENERIC"))

    def test_get_backend_raises_for_unknown(self):
        provider = MagicMock()
        provider.provider_type = "NONEXISTENT"
        with self.assertRaises(NotImplementedError):
            get_backend(provider)

    def test_register_backend_adds_to_registry(self):
        @register_backend("_TEST_ONLY_")
        class _FakeBackend:
            pass

        self.assertTrue(has_backend("_TEST_ONLY_"))


# ---------------------------------------------------------------------------
# sync_link unit tests (backend mocked)
# ---------------------------------------------------------------------------


class SyncLinkTests(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sync_user", email="sync@example.com", password="x",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(name="SLA sync")
        self.prod_type = Product_Type.objects.create(name="PT sync")
        self.role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=self.role)
        self.org = Organization.objects.create(name="Org sync", product_type=self.prod_type)
        self.product = Product.objects.create(
            name="Sync Product", description="", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product, organization=self.org, supported_languages=[], compilable=False, profile={},
        )
        test_type = Test_Type.objects.create(name="Sync test type")
        self.engagement = Engagement.objects.create(
            name="Sync engagement", target_start=timezone.now(), target_end=timezone.now(), product=self.product,
        )
        self.test = Test.objects.create(
            engagement=self.engagement, target_start=timezone.now(), target_end=timezone.now(), test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=self.test, title="Test vuln", severity="High", date=timezone.now(), reporter=self.user,
        )
        self.provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Test Jira",
            sync_enabled=True,
        )

    def _make_link(self, external_id="PROJ-42", external_key="PROJ-42", provider=None):
        return WorkItemLink.objects.create(
            finding=self.finding,
            provider=provider if provider is not None else self.provider,
            external_id=external_id,
            external_key=external_key,
            external_url="https://jira.example.com/browse/PROJ-42",
        )

    def test_sync_link_updates_status_on_success(self):
        link = self._make_link()
        remote = RemoteIssueInfo(
            raw_status="In Review",
            status_category=WorkItemStatusCategory.IN_PROGRESS,
            title="Fix SQL injection",
        )
        with patch("aist.work_items.sync.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.fetch_issue_status.return_value = remote
            mock_get_backend.return_value = mock_backend

            result = sync_link(link)

        self.assertTrue(result.success)
        link.refresh_from_db()
        self.assertEqual(link.raw_status, "In Review")
        self.assertEqual(link.status_category, WorkItemStatusCategory.IN_PROGRESS)
        self.assertEqual(link.title, "Fix SQL injection")
        self.assertEqual(link.sync_error, "")
        self.assertIsNotNone(link.last_synced_at)

    def test_sync_link_stores_error_on_failure(self):
        link = self._make_link()
        with patch("aist.work_items.sync.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.fetch_issue_status.side_effect = WorkItemSyncError("Connection timeout")
            mock_get_backend.return_value = mock_backend

            result = sync_link(link)

        self.assertFalse(result.success)
        self.assertIn("Connection timeout", result.error)
        link.refresh_from_db()
        self.assertIn("Connection timeout", link.sync_error)
        self.assertIsNotNone(link.last_synced_at)

    def test_sync_link_skips_manual_links(self):
        link = WorkItemLink.objects.create(
            finding=self.finding,
            provider=None,
            external_url="https://example.com/manual",
        )
        result = sync_link(link)
        self.assertFalse(result.success)
        self.assertIn("manual", result.error)

    def test_sync_link_skips_link_without_identifier(self):
        link = WorkItemLink.objects.create(
            finding=self.finding,
            provider=self.provider,
            external_id="",
            external_key="",
            external_url="https://example.com/x",
        )
        result = sync_link(link)
        self.assertFalse(result.success)
        link.refresh_from_db()
        self.assertIn("no external_id", link.sync_error)

    def test_sync_link_updates_external_url_from_remote(self):
        link = self._make_link()
        remote = RemoteIssueInfo(
            raw_status="Done",
            status_category=WorkItemStatusCategory.DONE,
            title="Fixed",
            external_url="https://jira.example.com/browse/PROJ-42",
        )
        with patch("aist.work_items.sync.get_backend") as mock_get_backend:
            mock_get_backend.return_value.fetch_issue_status.return_value = remote
            sync_link(link)

        link.refresh_from_db()
        self.assertEqual(link.external_url, "https://jira.example.com/browse/PROJ-42")


# ---------------------------------------------------------------------------
# sync_provider unit tests
# ---------------------------------------------------------------------------


class SyncProviderTests(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sprov_user", email="sprov@example.com", password="x",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(name="SLA sprov")
        self.prod_type = Product_Type.objects.create(name="PT sprov")
        self.role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=self.role)
        self.org = Organization.objects.create(name="Org sprov", product_type=self.prod_type)
        self.product = Product.objects.create(
            name="SProv Product", description="", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product, organization=self.org, supported_languages=[], compilable=False, profile={},
        )
        test_type = Test_Type.objects.create(name="SProv test type")
        self.engagement = Engagement.objects.create(
            name="SProv engagement", target_start=timezone.now(), target_end=timezone.now(), product=self.product,
        )
        self.test = Test.objects.create(
            engagement=self.engagement, target_start=timezone.now(), target_end=timezone.now(), test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=self.test, title="SProv vuln", severity="High", date=timezone.now(), reporter=self.user,
        )

    def _make_provider(self, *, sync_enabled=True, provider_type=WorkItemProviderType.JIRA):
        return WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=provider_type,
            name=f"Provider {provider_type} {sync_enabled}",
            sync_enabled=sync_enabled,
        )

    def test_sync_provider_skips_when_sync_disabled(self):
        provider = self._make_provider(sync_enabled=False)
        WorkItemLink.objects.create(
            finding=self.finding, provider=provider,
            external_key="X-1", external_url="https://x.com/1",
        )
        result = sync_provider(provider)
        self.assertIsInstance(result, ProviderSyncResult)
        self.assertEqual(result.synced, 0)
        self.assertEqual(result.skipped, 1)

    def test_sync_provider_skips_when_no_backend(self):
        provider = self._make_provider(provider_type=WorkItemProviderType.GENERIC)
        WorkItemLink.objects.create(
            finding=self.finding, provider=provider,
            external_url="https://example.com/1",
        )
        result = sync_provider(provider)
        self.assertEqual(result.synced, 0)
        self.assertEqual(result.skipped, 1)

    def test_sync_provider_aggregates_successes_and_failures(self):
        provider = self._make_provider(sync_enabled=True)
        # Two links
        finding2 = Finding.objects.create(
            test=self.test, title="SProv vuln2", severity="Medium", date=timezone.now(), reporter=self.user,
        )
        WorkItemLink.objects.create(
            finding=self.finding, provider=provider, external_key="P-1", external_url="https://x.com/1",
        )
        WorkItemLink.objects.create(
            finding=finding2, provider=provider, external_key="P-2", external_url="https://x.com/2",
        )

        remote_ok = RemoteIssueInfo(raw_status="Done", status_category=WorkItemStatusCategory.DONE, title="t")
        call_count = [0]

        def side_effect(link):
            call_count[0] += 1
            if call_count[0] == 1:
                return remote_ok
            msg = "timeout"
            raise WorkItemSyncError(msg)

        with patch("aist.work_items.sync.get_backend") as mock_get:
            mock_get.return_value.fetch_issue_status.side_effect = side_effect
            result = sync_provider(provider)

        self.assertEqual(result.synced, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len(result.errors), 1)


# ---------------------------------------------------------------------------
# Validate & sync API endpoint tests
# ---------------------------------------------------------------------------


class ValidateAndSyncAPITests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="vs_user", email="vs@example.com", password="x",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA vs")
        self.prod_type = Product_Type.objects.create(name="PT vs")
        self.role, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=self.role)
        self.org = Organization.objects.create(name="Org vs", product_type=self.prod_type)
        self.product = Product.objects.create(
            name="VS Product", description="", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=self.product, organization=self.org, supported_languages=[], compilable=False, profile={},
        )
        self.provider = WorkItemProvider.objects.create(
            organization=self.org,
            provider_type=WorkItemProviderType.JIRA,
            name="Jira VS",
            sync_enabled=True,
        )

    def _validate_url(self):
        return reverse("aist_api:work_item_provider_validate", kwargs={"provider_id": self.provider.pk})

    def _sync_url(self):
        return reverse("aist_api:work_item_provider_sync", kwargs={"provider_id": self.provider.pk})

    def test_validate_returns_202_and_task_id(self):
        fake_result = MagicMock(id="task-vs-ok")
        with patch("aist.tasks.validate.validate_work_item_provider.delay", return_value=fake_result) as mock_delay:
            response = self.client.post(self._validate_url())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["task_id"], "task-vs-ok")
        mock_delay.assert_called_once_with(self.provider.pk)

    def test_validate_queues_task_for_provider_with_backend(self):
        with patch("aist.tasks.validate.validate_work_item_provider.delay") as mock_delay:
            mock_delay.return_value = MagicMock(id="task-vs-backend")
            response = self.client.post(self._validate_url())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["task_id"], "task-vs-backend")

    def test_validate_queues_task_for_generic_provider(self):
        generic = WorkItemProvider.objects.create(
            organization=self.org, provider_type=WorkItemProviderType.GENERIC, name="Generic VS",
        )
        url = reverse("aist_api:work_item_provider_validate", kwargs={"provider_id": generic.pk})
        with patch("aist.tasks.validate.validate_work_item_provider.delay") as mock_delay:
            mock_delay.return_value = MagicMock(id="task-vs-generic")
            response = self.client.post(url)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["task_id"], "task-vs-generic")

    def test_sync_enqueues_task_and_returns_202(self):
        with patch("aist.api.work_items.sync_work_item_provider") as mock_task:
            response = self.client.post(self._sync_url())
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data["queued"])
        mock_task.delay.assert_called_once_with(self.provider.pk)
