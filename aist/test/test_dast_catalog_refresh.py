from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from dojo.models import Product_Type

from aist.integrations.dast_readiness import DAST_CATALOG_MAX_AGE
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.tasks import validate as validate_tasks
from aist.tasks.validate import refresh_dast_capability_catalogs
from aist.test.test_api import AISTApiBase

_CONFIG = {
    "gateway_url": "https://dast.example.internal",
    "ca_bundle": "",
    "contract_major": 2,
    "integrator_public_id": "pub_catalog",
    "server_fingerprint": "sha256:catalog",
}


class DastCatalogRefreshTests(AISTApiBase):

    """
    Launch readiness rejects a binding once its catalog passes DAST_CATALOG_MAX_AGE. Nothing in
    the product refreshed it before, so an installation stopped launching a day after onboarding
    with no visible cause. These tests pin who is due for a refresh and who is left alone.
    """

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Catalog refresh org",
            product_type=self.prod_type,
        )

    def _organization(self, name):
        """
        A second tenant, with its own Product_Type.

        Two constraints meet here: one active DAST integration per organization, and one
        organization per Product_Type. A second DAST integration therefore needs a whole second
        tenant, not just another row.
        """
        return Organization.objects.create(
            name=name,
            product_type=Product_Type.objects.create(name=f"{name} PT"),
        )

    def _integration(self, *, name, state, synced_at, claimed_at=None, is_active=True, organization=None):
        integration = OrgIntegration.objects.create(
            organization=organization or self.organization,
            integration_type=OrgIntegrationType.DAST,
            name=name,
            config=dict(_CONFIG),
            secret="pub_catalog.secret",  # noqa: S106
            is_active=is_active,
        )
        DastIntegrationState.objects.update_or_create(
            integration=integration,
            defaults={
                "validation_state": state,
                "capabilities_synced_at": synced_at,
                "sync_claimed_at": claimed_at,
            },
        )
        return integration

    def test_a_catalog_older_than_the_refresh_window_is_scheduled(self):
        now = timezone.now()
        integration = self._integration(
            name="stale",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=13),
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result, {"scheduled": 1, "skipped": 0})
        send_task.assert_called_once()
        self.assertEqual(send_task.call_args.args[0], "aist.tasks.validate.sync_dast_capabilities")
        self.assertEqual(send_task.call_args.kwargs["args"][0], integration.pk)

    def test_the_refresh_window_leaves_room_for_a_failed_attempt(self):
        """Refreshing at half the maximum age means one missed pass cannot expire the catalog."""
        self.assertLess(timedelta(hours=12) * 2, DAST_CATALOG_MAX_AGE + timedelta(hours=1))

        now = timezone.now()
        self._integration(
            name="fresh",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=2),
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result, {"scheduled": 0, "skipped": 0})
        send_task.assert_not_called()

    def test_a_never_synchronized_catalog_is_scheduled(self):
        self._integration(
            name="never",
            state=DastIntegrationValidationState.READY,
            synced_at=None,
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result["scheduled"], 1)
        send_task.assert_called_once()

    def test_an_in_flight_synchronization_is_not_restarted(self):
        now = timezone.now()
        self._integration(
            name="in-flight",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=20),
            claimed_at=now - timedelta(minutes=5),
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result, {"scheduled": 0, "skipped": 0})
        send_task.assert_not_called()

    def test_an_abandoned_claim_is_picked_up_again(self):
        now = timezone.now()
        self._integration(
            name="abandoned",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=20),
            claimed_at=now - timedelta(hours=3),
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result["scheduled"], 1)
        send_task.assert_called_once()

    def test_unvalidated_and_disabled_integrations_are_left_alone(self):
        now = timezone.now()
        self._integration(
            name="unvalidated",
            state=DastIntegrationValidationState.PENDING_VALIDATION,
            synced_at=now - timedelta(days=3),
        )
        self._integration(
            name="disabled",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(days=3),
            is_active=False,
            organization=self._organization("Disabled DAST org"),
        )

        with patch("aist.integrations.dast_capability_sync.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result, {"scheduled": 0, "skipped": 0})
        send_task.assert_not_called()

    def test_one_integration_losing_readiness_does_not_fail_the_whole_pass(self):
        now = timezone.now()
        self._integration(
            name="first",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=20),
        )
        self._integration(
            name="second",
            state=DastIntegrationValidationState.READY,
            synced_at=now - timedelta(hours=20),
            organization=self._organization("Second DAST org"),
        )
        real_schedule = validate_tasks.schedule_dast_capability_sync
        calls = {"count": 0}

        def flaky(integration):
            calls["count"] += 1
            if calls["count"] == 1:
                msg = "DAST capabilities can only be synchronized for a validated integration."
                raise ValueError(msg)
            return real_schedule(integration)

        with patch("aist.tasks.validate.schedule_dast_capability_sync", side_effect=flaky), \
                patch("aist.integrations.dast_capability_sync.current_app.send_task"), \
                self.captureOnCommitCallbacks(execute=True):
            result = refresh_dast_capability_catalogs()

        self.assertEqual(result, {"scheduled": 1, "skipped": 1})
