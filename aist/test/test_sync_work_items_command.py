from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import WorkItemLink, WorkItemProviderType
from aist.test.test_work_item_provider_api import WorkItemProviderAPIBase
from aist.work_items.sync import ProviderSyncResult


class SyncWorkItemsCommandTests(WorkItemProviderAPIBase):
    def setUp(self):
        super().setUp()
        engagement = Engagement.objects.create(
            name="Sync command engagement",
            product=self.product,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        test_type = Test_Type.objects.create(name="Sync command test type")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=test,
            title="Sync command finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

    def test_dry_run_single_provider_reports_link_count_without_syncing(self):
        provider = self._create_provider(name="Dry Run Jira")
        WorkItemLink.objects.create(
            finding=self.finding,
            provider=provider,
            external_id="123",
            external_key="SEC-123",
            external_url="https://jira.example.com/browse/SEC-123",
        )
        stdout = StringIO()

        with patch("aist.management.commands.sync_work_items.sync_provider") as mock_sync_provider:
            call_command("sync_work_items", "--provider-id", str(provider.pk), "--dry-run", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn(f"Would sync 1 link(s) for provider[{provider.pk}]", output)
        mock_sync_provider.assert_not_called()

    def test_single_provider_sync_calls_sync_provider_and_prints_summary(self):
        provider = self._create_provider(
            name="Prod Jira",
            provider_type=WorkItemProviderType.JIRA,
            sync_enabled=True,
            is_active=True,
        )
        stdout = StringIO()
        result = ProviderSyncResult(provider_id=provider.pk, synced=2, failed=1, skipped=3, errors=["boom"])

        with patch("aist.management.commands.sync_work_items.sync_provider", return_value=result) as mock_sync_provider:
            call_command("sync_work_items", "--provider-id", str(provider.pk), stdout=stdout)

        self.assertIn(
            f"Provider[{provider.pk}]: synced=2 failed=1 skipped=3",
            stdout.getvalue(),
        )
        mock_sync_provider.assert_called_once_with(provider)

    def test_inactive_provider_id_fails_fast(self):
        provider = self._create_provider(name="Inactive Jira", is_active=False)

        with self.assertRaises(CommandError) as ctx:
            call_command("sync_work_items", "--provider-id", str(provider.pk))

        self.assertIn(f"Provider[{provider.pk}] is inactive", str(ctx.exception))
