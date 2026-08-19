from unittest.mock import patch

from django.test import SimpleTestCase

from aist.execution import observability
from aist.execution.observability import AuditContext


class ExecutionObservabilityTests(SimpleTestCase):
    def test_metric_labels_are_bounded_and_exclude_tenant_run_and_secret_dimensions(self):
        metrics = (
            observability.QUEUE_AGE_SECONDS,
            observability.QUEUE_EVENTS_TOTAL,
            observability.LEASE_UTILIZATION_RATIO,
            observability.PROVIDER_SECONDS,
            observability.PROVIDER_ERRORS_TOTAL,
            observability.DAST_OUTCOMES_TOTAL,
            observability.DAST_LOG_CURSOR,
            observability.DAST_LOG_LAG_SECONDS,
            observability.DAST_SELECTION_DISTANCE,
            observability.DAST_FINALIZE_TOTAL,
        )
        forbidden = {"organization", "project", "integration", "run_id", "pipeline_id", "token"}

        for metric in metrics:
            self.assertFalse(forbidden.intersection(metric._labelnames))

    def test_observers_normalize_untrusted_values_before_using_labels(self):
        with (
            patch.object(observability.PROVIDER_SECONDS, "labels") as provider,
            patch.object(observability.PROVIDER_ERRORS_TOTAL, "labels") as errors,
            patch.object(observability.DAST_OUTCOMES_TOTAL, "labels") as outcomes,
            patch.object(observability.DAST_SELECTION_DISTANCE, "labels") as selection,
        ):
            observability.observe_provider_call(
                operation="run-tenant-controlled",
                duration_seconds=-1,
                error_code="provider supplied arbitrary text with an id",
            )
            observability.observe_dast_outcome(
                outcome="tenant-controlled",
                logs_delivered=-4,
                log_lag_seconds=-2,
                relation="tenant-controlled",
                distance=-9,
            )

        # An unrecognised operation is labelled "unknown", not folded into the most common value:
        # that folding is what made a resume indistinguishable from a first attempt.
        provider.assert_called_once_with(operation="unknown", result="error")
        errors.assert_called_once_with(operation="unknown", error_code="UNKNOWN")
        outcomes.assert_called_once_with(outcome="error")
        selection.assert_called_once_with(relation="none")

    def test_audit_payload_has_only_allowlisted_identifiers_and_never_accepts_free_form_detail(self):
        context = AuditContext(
            organization_id=3,
            project_id=4,
            integration_id=5,
            binding_id=6,
            request_id=7,
            pipeline_id="pipe-8",
            actor_id=9,
        )
        with patch.object(observability._logger, "info") as info:
            observability.audit_event("dast_cancel_requested", context=context)

        payload = info.call_args.kwargs["extra"]["aist_audit_event"]
        self.assertEqual(payload["event"], "dast_cancel_requested")
        self.assertEqual(payload["pipeline_id"], "pipe-8")
        self.assertNotIn("detail", payload)
        self.assertNotIn("secret", payload)

    def test_unknown_audit_and_alert_codes_fail_closed(self):
        with self.assertRaises(ValueError):
            observability.audit_event("user-controlled", context=AuditContext())
        with self.assertRaises(ValueError):
            observability.operational_alert("user-controlled", execution_type="dast", count=1)
