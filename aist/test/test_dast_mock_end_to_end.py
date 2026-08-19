# ruff: noqa: E402
from __future__ import annotations

import stat
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.contract_snapshot import DastContractSnapshot
from pipeline.dast.contracts import (
    DastConnectorOutcome,
    DastConnectorOutcomeState,
    DastRunState,
    DastTerminalResult,
)
from pipeline.dast.executor import DastExecutionResult, DastExecutionTelemetry

from aist.execution.claiming import claim_next_launch_request, revalidate_claimed_authority
from aist.execution.dast import DastPipelineLaunchAdapter
from aist.execution.dast_deadlines import dast_final_pass_taken, dast_unreachable_grace
from aist.execution.dispatching import (
    LaunchPlanningStatus,
    accept_published_launch,
    plan_claimed_launch,
    prepare_launch_publish,
)
from aist.integrations.dast_capability_sync import prepare_dast_capability_sync, run_dast_capability_sync
from aist.integrations.dast_config import DastTargetSnapshot
from aist.integrations.dast_gateway_client import DastGatewayPing, DastTargetCatalog
from aist.integrations.dast_validation import prepare_dast_validation, run_dast_validation
from aist.launch_data import PipelineLaunchData
from aist.models import (
    AISTPipeline,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    DastProjectBinding,
    DastRunMetadata,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    OrgIntegrationVPNSecret,
    PipelineExecutionLease,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.services.dast_outcomes import public_dast_outcome_code
from aist.tasks import pipeline as pipeline_tasks
from aist.tasks import pipeline_dispatcher
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import finish_pipeline

ACTUAL_SHA = "a" * 40
TRIGGER_BRANCH = "release/2026-07"
PERIMETER_TARGET = "edge-perimeter"


class ContractFaithfulDastGateway:

    """Scripted provider double whose surfaces are locked to the pinned OpenAPI snapshot."""

    def __init__(self, test_case):
        self.test_case = test_case
        self.contract = DastContractSnapshot.load()
        self.events: list[tuple[str, str]] = []
        self.targets = (
            self._target("payments-api", capability_character="a", schema_character="c"),
            self._target("admin-api", capability_character="b", schema_character="d"),
            # A perimeter target: the provider scans a running system and knows no repository,
            # so this run carries no trigger and reports no source commit.
            self._target(
                PERIMETER_TARGET,
                capability_character="e",
                schema_character="f",
                requires_repository=False,
            ),
        )
        self.findings_by_target: dict[str, list[dict]] = {
            "payments-api": [],
            "admin-api": [
                {
                    "title": "Cross-tenant object access",
                    "severity": "High",
                    "description": "A tenant can read another tenant's object.",
                    "unique_id_from_tool": "dast-bola-1",
                    "vuln_id_from_tool": "dast-bola-1",
                    "cwe": 639,
                    "dynamic_finding": True,
                    "endpoints": ["https://admin.example.test/v2/objects/42"],
                },
            ],
            PERIMETER_TARGET: [
                {
                    "title": "Exposed management interface",
                    "severity": "High",
                    "description": "An administrative interface answers on the public perimeter.",
                    "unique_id_from_tool": "dast-perimeter-1",
                    "vuln_id_from_tool": "dast-perimeter-1",
                    "cwe": 284,
                    "dynamic_finding": True,
                    "endpoints": ["https://edge.example.test:8443/manage"],
                },
            ],
        }

    @staticmethod
    def _target(
        target_id: str,
        *,
        capability_character: str,
        schema_character: str,
        requires_repository: bool = True,
    ) -> DastTargetSnapshot:
        return DastTargetSnapshot.from_snapshot(
            {
                "id": target_id,
                "display_name": target_id.replace("-", " ").title(),
                "contract_revision": "2.0",
                "capability_revision": f"sha256:{capability_character * 64}",
                "schema_digest": f"sha256:{schema_character * 64}",
                "parameter_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"depth": {"enum": ["light", "deep"]}},
                    "required": ["depth"],
                },
                "defaults": {"depth": "light"},
                "repository_keys": ["backend"] if requires_repository else [],
                "launch_requirements": ["repository-trigger"] if requires_repository else [],
                "autonomous_ready": True,
            },
        )

    @contextmanager
    def context(self, integration, *, execution_id):
        self.events.append(("context", execution_id))
        self.integration = integration
        yield self

    def ping(self) -> DastGatewayPing:
        operation = self.contract["paths"]["/integrations/v2/ping"]["get"]
        self.test_case.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.events.append(("GET", "/integrations/v2/ping"))
        return DastGatewayPing(contract_version="2.0", gateway_version="2026.7", status="ok")

    def catalog(self, *, etag: str = "") -> DastTargetCatalog:
        del etag
        operation = self.contract["paths"]["/integrations/v2/targets"]["get"]
        self.test_case.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.events.append(("GET", "/integrations/v2/targets"))
        return DastTargetCatalog(contract_version="2.0", etag="catalog-e2e", targets=self.targets)

    def execute(self, _execution_type, execution) -> DastExecutionResult:
        self.events.extend(
            [
                ("POST", "/integrations/v2/runs"),
                ("GET", "/integrations/v2/runs/{run_id}/logs"),
                ("GET", "/integrations/v2/runs/{run_id}/results"),
            ],
        )
        self.test_case.assertEqual(execution.vpn_container_name, "vpn-dast-e2e")
        sourceless = execution.command.target_id == PERIMETER_TARGET
        if sourceless:
            self.test_case.assertIsNone(execution.command.trigger)
        else:
            self.test_case.assertEqual(execution.command.trigger.type.value, "GIT_BRANCH")
            self.test_case.assertEqual(execution.command.trigger.ref, TRIGGER_BRANCH)
        self.test_case.assertEqual(stat.S_IMODE(execution.token_file.stat().st_mode), 0o600)
        self.test_case.assertEqual(
            execution.token_file.read_text(encoding="utf-8"),
            "pub_e2e.secret-value",
        )

        run_id = f"run-{execution.command.target_id}"
        recovery = execution.recovery.for_run(run_id).with_cursor(2)
        status = DastRunState.STOPPED if execution.stop_requested else DastRunState.SUCCEEDED
        findings = self.findings_by_target.get(execution.command.target_id, [])
        source_commits = {} if sourceless else {"backend": ACTUAL_SHA}
        terminal = DastTerminalResult.from_wire(
            {
                "contract_version": "2.0",
                "run_id": run_id,
                "status": status.value,
                "selection": {"stand_id": "qa-shared", "relation": "ancestor", "distance": 2},
                "trigger_resolution": None if sourceless else {
                    "type": "GIT_BRANCH",
                    "ref": TRIGGER_BRANCH,
                    "resolved_commit": "b" * 40,
                    "resolved_at": "2026-07-26T10:00:00Z",
                },
                "dast_run_metadata": {"source_commits": source_commits},
                "report": {
                    "name": "DAST",
                    "type": "DAST Autonomous Scan",
                    "version": PERIMETER_TARGET if sourceless else "backend@aaaaaaaaaaaa",
                    "findings": findings,
                    "dast_run_metadata": {
                        "run_id": run_id,
                        "target": execution.command.target_id,
                        "stand": "qa-shared",
                        "source_commits": source_commits,
                    },
                },
                "audit": {
                    "correlation_id": execution.command.correlation_id,
                    "source_verified": not execution.stop_requested,
                },
            },
        )
        outcome = DastConnectorOutcome(
            state=DastConnectorOutcomeState.TERMINAL,
            recovery=recovery,
            reason_code="CANCEL_REQUESTED" if execution.stop_requested else None,
        )
        return DastExecutionResult(
            outcome=outcome,
            terminal_result=terminal,
            recovery=recovery,
            telemetry=DastExecutionTelemetry(logs_delivered=2, max_log_lag_seconds=0.5),
        )


class _DastContractGatewayFixture(AISTApiBase):

    """Onboarding, bindings and launch plumbing for a contract-faithful DAST gateway."""

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(name="DAST mock E2E", product_type=self.prod_type)
        self.repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="backend",
        )
        self.project.repository = self.repository
        self.project.save(update_fields=["repository", "updated"])
        self.branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version=TRIGGER_BRANCH,
        )
        self.vpn = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.VPN,
            name="DAST E2E VPN",
            is_active=True,
            config={"profile": "dast-e2e"},
        )
        OrgIntegrationVPNSecret.objects.create(integration=self.vpn, ovpn_content="client\ndev tun")
        self.gateway = ContractFaithfulDastGateway(self)
        self.integration = self._onboard_and_sync()
        self.bindings = self._create_bindings()

    def _onboard_and_sync(self):
        import_url = reverse(
            "aist_api:organization_dast_integration_import",
            kwargs={"org_id": self.organization.pk},
        )
        # Import schedules its own validation. This test drives validation and sync explicitly
        # against the contract-faithful fake below, so the broker publish is suppressed rather
        # than left to fire later inside an unrelated on-commit block.
        self.enterContext(patch("aist.integrations.dast_validation.current_app.send_task"))
        response = self.client.post(
            import_url,
            {
                "name": "Contract gateway",
                "vpn_integration_id": self.vpn.pk,
                "bundle": {
                    "bundle_version": 1,
                    "gateway_url": "https://10.20.30.40",
                    "ca_bundle": "",
                    "contract_major": 2,
                    "integrator_public_id": "pub_e2e",
                    "server_fingerprint": "sha256:e2e-fingerprint",
                    "token": "pub_e2e.secret-value",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("secret-value", str(response.data))
        integration = OrgIntegration.objects.get(pk=response.data["id"])

        validation = run_dast_validation(
            prepare_dast_validation(integration),
            client_context_factory=self.gateway.context,
        )
        self.assertTrue(validation["valid"])
        sync = run_dast_capability_sync(
            prepare_dast_capability_sync(integration),
            client_context_factory=self.gateway.context,
        )
        self.assertEqual(sync["etag"], "catalog-e2e")
        return integration

    def _create_bindings(self):
        bindings_url = reverse(
            "aist_api:project_dast_binding_list_create",
            kwargs={"project_id": self.project.pk},
        )
        bindings = {}
        for target in self.integration.dast_targets.order_by("provider_id"):
            requires_repository = target.get_snapshot().launch_requirements.requires_repository()
            response = self.client.post(
                bindings_url,
                {
                    "target_id": target.pk,
                    "capability_revision": target.capability_revision,
                    "schema_digest": target.schema_digest,
                    "source_repo_key": "backend" if requires_repository else "",
                    "enabled": True,
                    "parameter_snapshot": {"depth": "light"},
                },
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["readiness"]["ready"], response.data)
            bindings[target.provider_id] = DastProjectBinding.objects.get(pk=response.data["id"])
        return bindings

    def _enqueue(self, binding, name):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name=name,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=binding,
            trigger_project_version=self.branch if binding.requires_source_repository else None,
            params={"depth": "light"},
        )
        start_url = reverse(
            "aist_api:project_launch_config_start",
            kwargs={"project_id": self.project.pk, "config_id": config.pk},
        )
        response = self.client.post(start_url, {}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        return PipelineLaunchRequest.objects.get(pk=response.data["id"])

    def _plan_and_publish(self, request):
        claim = claim_next_launch_request(claim_owner=f"e2e-{request.pk}")
        self.assertEqual(claim.request_id, request.pk)
        self.assertTrue(revalidate_claimed_authority(request_id=request.pk, claim_owner=claim.claim_owner))
        result = plan_claimed_launch(
            request_id=request.pk,
            claim_owner=claim.claim_owner,
            adapter_registry=pipeline_dispatcher.launch_adapter_registry,
        )
        if result.status is LaunchPlanningStatus.READY:
            command = prepare_launch_publish(request_id=request.pk)
            self.assertEqual(
                accept_published_launch(pipeline_id=command.pipeline_id, task_id=command.task_id).value,
                "ACCEPTED",
            )
        request.refresh_from_db()
        return result

    @contextmanager
    def _runtime_patches(self):
        @contextmanager
        def vpn_context(resolved, *, execution_id):
            self.assertEqual(resolved.integration.pk, self.vpn.pk)
            self.gateway.events.append(("vpn", execution_id))
            yield "vpn-dast-e2e", None

        with (
            patch("aist.tasks.pipeline.vpn_sidecar_context", vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=self.gateway.execute),
        ):
            yield


class DastMockEndToEndTests(_DastContractGatewayFixture):
    def test_onboarding_capacity_clean_findings_and_cancel_follow_one_generic_execution_path(self):
        clean_request = self._enqueue(self.bindings["payments-api"], "Clean target")
        finding_request = self._enqueue(self.bindings["admin-api"], "Finding target")

        self.assertEqual(self._plan_and_publish(clean_request).status, LaunchPlanningStatus.READY)
        busy_result = self._plan_and_publish(finding_request)
        self.assertEqual(busy_result.status, LaunchPlanningStatus.BUSY)
        finding_request.refresh_from_db()
        self.assertEqual(finding_request.state, PipelineLaunchRequestState.PENDING)
        self.assertIsNone(finding_request.pipeline_id)

        with self._runtime_patches(), self.captureOnCommitCallbacks(execute=True):
            pipeline_tasks._execute_dast_pipeline(clean_request.pipeline_id)
        clean_pipeline = AISTPipeline.objects.get(pk=clean_request.pipeline_id)
        self.assertEqual(clean_pipeline.project_version.version, ACTUAL_SHA)
        self.assertEqual(clean_pipeline.tests.first().finding_set.count(), 0)
        self.assertEqual(clean_pipeline.status, AISTStatus.FINISHED)

        PipelineLaunchRequest.objects.filter(pk=finding_request.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self._plan_and_publish(finding_request).status, LaunchPlanningStatus.READY)
        with self._runtime_patches(), self.captureOnCommitCallbacks(execute=False):
            pipeline_tasks._execute_dast_pipeline(finding_request.pipeline_id)
        finding_pipeline = AISTPipeline.objects.get(pk=finding_request.pipeline_id)
        self.assertEqual(finding_pipeline.project_version.version, ACTUAL_SHA)
        self.assertEqual(finding_pipeline.tests.first().finding_set.count(), 1)
        finish_pipeline(finding_pipeline.id)

        cancel_request = self._enqueue(self.bindings["payments-api"], "Cancelled target")
        self.assertEqual(self._plan_and_publish(cancel_request).status, LaunchPlanningStatus.READY)
        # Model the accepted worker reaching the execution boundary before the operator
        # requests cancellation. The second preparation below is the normal recovery path
        # and must carry the persisted stop intent into the connector input.
        pipeline_tasks._prepare_dast_runtime(cancel_request.pipeline_id)
        stop_url = reverse("aist_api:pipeline_stop", kwargs={"pipeline_id": cancel_request.pipeline_id})
        with patch("aist.utils.pipeline.cleanup_pipeline_containers"):
            stopped = self.client.post(stop_url, format="json")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.data["state"], "STOP_PENDING")
        with self._runtime_patches():
            result = pipeline_tasks._execute_dast_pipeline(cancel_request.pipeline_id)
        pipeline_tasks._handle_dast_execution_result(None, cancel_request.pipeline_id, result)
        cancel_request.refresh_from_db()
        cancelled_pipeline = AISTPipeline.objects.get(pk=cancel_request.pipeline_id)
        self.assertEqual(cancel_request.state, PipelineLaunchRequestState.CANCELLED)
        self.assertEqual(cancelled_pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertFalse(PipelineExecutionLease.objects.filter(released_at__isnull=True).exists())
        self.assertIn(("POST", "/integrations/v2/runs"), self.gateway.events)
        self.assertIn(("GET", "/integrations/v2/runs/{run_id}/logs"), self.gateway.events)

    def test_perimeter_target_finishes_and_its_findings_are_reachable_by_project(self):
        """
        The operator scenario this covers: a perimeter target has no repository, so nothing in the
        launch names a source version — and the findings it produces must still be the findings the
        operator sees when filtering by project.
        """
        request = self._enqueue(self.bindings[PERIMETER_TARGET], "Perimeter target")

        self.assertIsNone(request.trigger_project_version_id)
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)

        with self._runtime_patches(), self.captureOnCommitCallbacks(execute=True):
            pipeline_tasks._execute_dast_pipeline(request.pipeline_id)

        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        self.assertIsNone(pipeline.trigger_project_version_id)
        # The result belongs to the target itself, which is what makes it reachable below.
        self.assertEqual(pipeline.project_version.version, PERIMETER_TARGET)
        self.assertEqual(pipeline.project_version.version_type, VersionType.DAST_TARGET)
        self.assertEqual(pipeline.tests.first().finding_set.count(), 1)
        # Findings imported, so the pipeline waits for provider-side deduplication; the tail it
        # takes from here is covered by WatchDeduplicationTests, which drives the watcher directly.
        self.assertEqual(pipeline.status, AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        self.assertFalse(PipelineLaunchData(pipeline.launch_data).finding_postprocessing)

        findings = self.client.get(
            reverse("aist_api:finding_list"),
            {"project_id": self.project.pk, "pipeline_id": pipeline.id},
        )
        self.assertEqual(findings.status_code, 200, findings.data)
        # The importer title-cases finding titles; what matters here is that the perimeter
        # finding is reachable at all through a project filter that resolves via versions.
        self.assertEqual(
            [item["title"].lower() for item in findings.data["results"]],
            ["exposed management interface"],
        )

    def test_a_source_based_dast_result_also_opts_out_of_the_source_code_tail(self):
        """
        AIST cannot triage a finding it has no code for, and enrichment's path/severity exclusions
        describe a source tree — so a DAST result opts out for both shapes of target, not only the
        one that obviously has no repository.
        """
        request = self._enqueue(self.bindings["admin-api"], "Source-based tail")
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)

        with self._runtime_patches(), self.captureOnCommitCallbacks(execute=True):
            pipeline_tasks._execute_dast_pipeline(request.pipeline_id)

        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        self.assertEqual(pipeline.project_version.version, ACTUAL_SHA)
        self.assertEqual(pipeline.tests.first().finding_set.count(), 1)
        self.assertFalse(PipelineLaunchData(pipeline.launch_data).finding_postprocessing)

    def test_redelivered_perimeter_finalization_is_a_no_op(self):
        """A Celery redelivery of the finalization task must not turn into a permanent error."""
        request = self._enqueue(self.bindings[PERIMETER_TARGET], "Perimeter redelivery")
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)
        with self._runtime_patches(), self.captureOnCommitCallbacks(execute=True):
            result = pipeline_tasks._execute_dast_pipeline(request.pipeline_id)

        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        test_ids = list(pipeline.tests.values_list("id", flat=True))

        with self.captureOnCommitCallbacks(execute=True):
            pipeline_tasks._handle_dast_execution_result(None, request.pipeline_id, result)

        pipeline.refresh_from_db()
        self.assertEqual(list(pipeline.tests.values_list("id", flat=True)), test_ids)
        self.assertEqual(pipeline.tests.first().finding_set.count(), 1)


class _ExpiredDeadlineTask:

    """Stands in for the bound Celery task on a run whose ceiling has already passed."""

    def __init__(self):
        self.request = type("Request", (), {"retries": 7, "id": "task-harvest"})()
        self.retry_calls: list[dict] = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        detail = "a run past its ceiling must not be retried"
        raise AssertionError(detail)


class DastFinalHarvestTests(_DastContractGatewayFixture):

    """Regression: a scan that finished seconds after the platform stopped waiting was discarded."""

    def _expire_the_ceiling(self, pipeline_id):
        DastExecutionState.objects.filter(pipeline_id=pipeline_id).update(
            deadline=timezone.now() - dast_unreachable_grace() - timedelta(seconds=1),
            last_progress_at=timezone.now(),
        )

    def _pending_then(self, *, harvest_is_terminal: bool):
        """Answer every ordinary attempt with "not finished", and the harvest with the truth."""
        attempts = []

        def execute(execution_type, execution):
            attempts.append(execution.harvest_only)
            if execution.harvest_only and harvest_is_terminal:
                return self.gateway.execute(execution_type, execution)
            recovery = execution.recovery.for_run(f"run-{execution.command.target_id}").with_cursor(2)
            return DastExecutionResult(
                outcome=DastConnectorOutcome(
                    state=DastConnectorOutcomeState.STOP_PENDING,
                    recovery=recovery,
                    reason_code="EXECUTION_TIMEOUT",
                ),
                terminal_result=None,
                recovery=recovery,
                telemetry=DastExecutionTelemetry(logs_delivered=2, max_log_lag_seconds=0.5),
            )

        return execute, attempts

    def test_a_run_that_finished_after_its_ceiling_is_imported_instead_of_declared_lost(self):
        request = self._enqueue(self.bindings["admin-api"], "Overran target")
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)
        self._expire_the_ceiling(request.pipeline_id)
        execute, attempts = self._pending_then(harvest_is_terminal=True)

        @contextmanager
        def vpn_context(resolved, *, execution_id):
            del resolved, execution_id
            yield "vpn-dast-e2e", None

        with (
            patch("aist.tasks.pipeline.vpn_sidecar_context", vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=execute),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pipeline_tasks._run_dast_execution(
                _ExpiredDeadlineTask(),
                request.pipeline_id,
                operation="execute",
            )

        # One ordinary attempt, then exactly one harvest.
        self.assertEqual(attempts, [False, True])
        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        state = DastExecutionState.objects.get(pipeline_id=request.pipeline_id)
        self.assertEqual(state.outcome, DastExecutionOutcome.TERMINAL)
        self.assertEqual(public_dast_outcome_code(pipeline), "SUCCESS_WITH_FINDINGS")
        self.assertEqual(pipeline.tests.first().finding_set.count(), 1)
        self.assertTrue(DastRunMetadata.objects.filter(pipeline_id=request.pipeline_id).exists())
        self.assertNotEqual(pipeline.status, AISTStatus.EXECUTING)

    def test_a_run_that_really_is_gone_still_ends_as_a_timeout(self):
        request = self._enqueue(self.bindings["admin-api"], "Lost target")
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)
        self._expire_the_ceiling(request.pipeline_id)
        execute, attempts = self._pending_then(harvest_is_terminal=False)

        @contextmanager
        def vpn_context(resolved, *, execution_id):
            del resolved, execution_id
            yield "vpn-dast-e2e", None

        with (
            patch("aist.tasks.pipeline.vpn_sidecar_context", vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=execute),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pipeline_tasks._run_dast_execution(
                _ExpiredDeadlineTask(),
                request.pipeline_id,
                operation="execute",
            )

        self.assertEqual(attempts, [False, True])
        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        state = DastExecutionState.objects.get(pipeline_id=request.pipeline_id)
        self.assertEqual(state.outcome, DastExecutionOutcome.UNREACHABLE)
        self.assertEqual(public_dast_outcome_code(pipeline), "TIMEOUT")
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertFalse(PipelineExecutionLease.objects.filter(released_at__isnull=True).exists())
        # The closing pass recorded itself, so reconciliation will not grant a second one.
        self.assertTrue(dast_final_pass_taken(state.recovery_checkpoint))
        self.assertFalse(DastPipelineLaunchAdapter.should_recover(pipeline))

    def test_a_harvest_that_cannot_reach_the_provider_does_not_replace_the_outcome(self):
        request = self._enqueue(self.bindings["admin-api"], "Unreachable target")
        self.assertEqual(self._plan_and_publish(request).status, LaunchPlanningStatus.READY)
        self._expire_the_ceiling(request.pipeline_id)
        attempts = []

        def execute(_execution_type, execution):
            attempts.append(execution.harvest_only)
            if execution.harvest_only:
                detail = "gateway hostname could not be resolved safely"
                raise pipeline_tasks.DastExecutionIncomplete(detail)
            recovery = execution.recovery.for_run("run-admin-api").with_cursor(2)
            return DastExecutionResult(
                outcome=DastConnectorOutcome(
                    state=DastConnectorOutcomeState.UNREACHABLE,
                    recovery=recovery,
                ),
                terminal_result=None,
                recovery=recovery,
                telemetry=DastExecutionTelemetry(logs_delivered=2, max_log_lag_seconds=0.5),
            )

        @contextmanager
        def vpn_context(resolved, *, execution_id):
            del resolved, execution_id
            yield "vpn-dast-e2e", None

        with (
            patch("aist.tasks.pipeline.vpn_sidecar_context", vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=execute),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pipeline_tasks._run_dast_execution(
                _ExpiredDeadlineTask(),
                request.pipeline_id,
                operation="execute",
            )

        self.assertEqual(attempts, [False, True])
        pipeline = AISTPipeline.objects.get(pk=request.pipeline_id)
        self.assertEqual(public_dast_outcome_code(pipeline), "TIMEOUT")
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
