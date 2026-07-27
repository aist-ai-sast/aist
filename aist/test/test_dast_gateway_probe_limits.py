"""
Bounds on how often one actor can make this installation call a tenant-supplied gateway.

Storing a DAST connection schedules a probe of whatever `gateway_url` the bundle names. Several
cheap writes each mean "the connection changed" — toggling `is_active` is enough — so without a
bound an organization admin could loop one and turn AIST into a traffic source aimed at that
host. Two layers cover it: scheduling debounces bursts, and the write endpoints are throttled.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse

from aist.integrations.dast_validation import (
    VALIDATION_DEBOUNCE_SECONDS,
    schedule_dast_validation,
)
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.test.test_api import AISTApiBase

_CONFIG = {
    "gateway_url": "https://dast.example.internal",
    "ca_bundle": "",
    "contract_major": 2,
    "integrator_public_id": "pub_probe",
    "server_fingerprint": "sha256:probe",
}


class DastGatewayProbeLimitTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        # The throttle cache is process-wide, so a prior test could otherwise push this one
        # straight to a spurious 429.
        cache.clear()
        self.organization = Organization.objects.create(
            name="Probe limit org",
            product_type=self.prod_type,
        )
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST",
            config=dict(_CONFIG),
            secret="pub_probe.secret",  # noqa: S106
            is_active=True,
        )
        DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
        )

    def test_a_scheduled_validation_is_delayed_so_a_burst_collapses(self):
        with patch("aist.integrations.dast_validation.current_app.send_task") as send_task, \
                self.captureOnCommitCallbacks(execute=True):
            schedule_dast_validation(self.integration)

        send_task.assert_called_once()
        self.assertEqual(send_task.call_args.kwargs["countdown"], VALIDATION_DEBOUNCE_SECONDS)
        self.assertGreater(
            VALIDATION_DEBOUNCE_SECONDS,
            0,
            "a zero delay means every write reaches the gateway individually",
        )

    def test_each_write_in_a_burst_supersedes_the_previous_generation(self):
        """
        Only the newest generation survives the debounce window, and `run_dast_validation`
        refuses a superseded ticket before opening a connection — so a burst is one probe.
        """
        with patch("aist.integrations.dast_validation.current_app.send_task"), \
                self.captureOnCommitCallbacks(execute=True):
            tickets = [schedule_dast_validation(self.integration) for _attempt in range(5)]

        generations = [ticket.generation for ticket in tickets]
        self.assertEqual(generations, sorted(generations))
        self.assertEqual(len(set(generations)), len(generations))
        state = DastIntegrationState.objects.get(integration=self.integration)
        self.assertEqual(state.validation_generation, generations[-1])

    def test_repeated_writes_are_throttled_before_they_can_become_traffic(self):
        """
        Exercised against the real configured rate rather than an overridden one: DRF caches its
        throttle settings, and a test that silently fails to apply an override would assert
        nothing. The default is 60/hour, so this loop has to cross it.
        """
        validate_url = reverse(
            "aist_api:org_integration_validate",
            kwargs={"integration_id": self.integration.pk},
        )

        with patch("aist.integrations.dast_validation.current_app.send_task"):
            statuses = [self.client.post(validate_url, format="json").status_code for _ in range(65)]

        self.assertIn(429, statuses, "the probe throttle never engaged")
        self.assertTrue(all(code in {202, 429} for code in statuses), set(statuses))

    def test_reading_an_integration_is_never_throttled(self):
        """The bound exists for outbound calls; listing must stay usable."""
        list_url = reverse(
            "aist_api:org_integration_list_create",
            kwargs={"org_id": self.organization.pk},
        )

        statuses = [self.client.get(list_url).status_code for _ in range(12)]

        self.assertNotIn(429, statuses)
