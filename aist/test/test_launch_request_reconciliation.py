import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.execution.leases import DEFAULT_EXECUTION_LEASE_POLICY
from aist.execution.reconciliation import (
    DEAD_EXECUTION_TASK,
    EXECUTION_LEASE_MISSING,
    EXECUTION_RECOVERY_REPUBLISHED,
    ORPHAN_PIPELINE,
    OUTBOX_LEASE_RENEWED,
    STALE_CLAIM_REQUEUED,
    TERMINAL_LEASE_RELEASED,
    reconcile_launch_requests,
)
from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionLease,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire


class LaunchRequestReconciliationTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        organization = Organization.objects.create(
            name="DAST reconciliation fixture organization",
            product_type=self.prod_type,
        )
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST reconciliation fixture",
            config=_integration_config("dast-reconciliation-public-id"),
            secret="runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        now = timezone.now()
        DastIntegrationState.objects.create(
            integration=integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=now,
            contract_version="2.0",
            capabilities_etag="reconciliation-catalog",
            capabilities_synced_at=now,
        )
        target = refresh_dast_targets(
            integration,
            (DastTargetSnapshot.from_snapshot(_target_wire("reconciliation-api")),),
            seen_at=now,
        )[0]
        self.dast_binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="reconciliation-api",
            enabled=True,
            parameter_snapshot={"depth": "deep"},
        )

    def _request(self, *, state, claimed_at=None):
        return PipelineLaunchRequest.objects.create(
            project=self.project,
            state=state,
            claim_owner="crashed-dispatcher" if claimed_at else None,
            claimed_at=claimed_at,
            task_name="aist.tasks.pipeline.run_pipeline_execution",
            task_args_snapshot=[],
        )

    def _attach_outbox_pipeline(self, request, *, status=AISTStatus.ADMITTED):
        pipeline = AISTPipeline.objects.create(
            id=request.task_id.hex,
            project=self.project,
            project_version=self.pv,
            status=status,
            run_task_id=str(request.task_id),
        )
        request.pipeline = pipeline
        request.save(update_fields=["pipeline", "updated"])
        return pipeline

    def _lease(self, request, *, pipeline=None, now, heartbeat_age=timedelta(minutes=10)):
        return PipelineExecutionLease.objects.create(
            request=request,
            pipeline=pipeline,
            resource_key=f"sast:worker:{request.pk}",
            slot=0,
            acquired_at=now - timedelta(minutes=20),
            heartbeat_at=now - heartbeat_age,
            expires_at=now - timedelta(minutes=1),
        )

    def test_stale_claim_without_committed_outbox_is_requeued_with_audit_reason(self):
        now = timezone.now()
        request = self._request(
            state=PipelineLaunchRequestState.CLAIMED,
            claimed_at=now - timedelta(minutes=10),
        )

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PENDING)
        self.assertIsNone(request.claim_owner)
        self.assertIsNone(request.claimed_at)
        self.assertEqual(request.failure_code, STALE_CLAIM_REQUEUED)
        self.assertEqual(stats["requeued_claims"], 1)

    def test_live_heartbeat_prevents_stale_claim_and_lease_from_being_stolen(self):
        now = timezone.now()
        request = self._request(
            state=PipelineLaunchRequestState.CLAIMED,
            claimed_at=now - timedelta(minutes=10),
        )
        lease = self._lease(request, now=now, heartbeat_age=timedelta(seconds=30))

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CLAIMED)
        self.assertIsNone(lease.released_at)
        self.assertEqual(stats["skipped_live_owners"], 1)

    def test_stale_claim_releases_expired_lease_before_requeue(self):
        now = timezone.now()
        request = self._request(
            state=PipelineLaunchRequestState.CLAIMED,
            claimed_at=now - timedelta(minutes=10),
        )
        lease = self._lease(request, now=now)

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PENDING)
        self.assertEqual(lease.released_at, now)
        self.assertEqual(stats["released_leases"], 1)

    def test_planned_outbox_renews_expired_lease_without_changing_identity(self):
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.PLANNED)
        pipeline = self._attach_outbox_pipeline(request)
        lease = self._lease(request, pipeline=pipeline, now=now)
        original_pipeline_id = request.pipeline_id
        original_task_id = request.task_id

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PLANNED)
        self.assertEqual(request.pipeline_id, original_pipeline_id)
        self.assertEqual(request.task_id, original_task_id)
        self.assertEqual(request.failure_code, OUTBOX_LEASE_RENEWED)
        self.assertEqual(lease.heartbeat_at, now)
        self.assertGreater(lease.expires_at, now)
        self.assertEqual(stats["recovered_outboxes"], 1)

    def test_outbox_without_pipeline_fails_and_releases_capacity(self):
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.PUBLISHED)
        lease = self._lease(request, now=now)

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.FAILED)
        self.assertEqual(request.failure_code, ORPHAN_PIPELINE)
        self.assertEqual(lease.released_at, now)
        self.assertEqual(stats["failed_orphans"], 1)

    def test_outbox_without_active_lease_fails_its_placeholder_pipeline(self):
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.PLANNED)
        pipeline = self._attach_outbox_pipeline(request)

        reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: None)

        request.refresh_from_db()
        pipeline.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.FAILED)
        self.assertEqual(request.failure_code, EXECUTION_LEASE_MISSING)
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertIsNone(pipeline.run_task_id)

    def test_dead_celery_task_finishes_pipeline_and_releases_lease_once(self):
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.DISPATCHED)
        pipeline = self._attach_outbox_pipeline(request, status=AISTStatus.EXECUTING)
        lease = self._lease(request, pipeline=pipeline, now=now)

        first = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "FAILURE")
        second = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "FAILURE")

        request.refresh_from_db()
        pipeline.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        self.assertEqual(request.failure_code, DEAD_EXECUTION_TASK)
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertIsNone(pipeline.run_task_id)
        self.assertEqual(pipeline.launch_data["execution_reconciliation"]["code"], DEAD_EXECUTION_TASK)
        self.assertEqual(lease.released_at, now)
        self.assertEqual(first["reconciled_dead_tasks"], 1)
        self.assertEqual(second["reconciled_dead_tasks"], 0)

    def test_live_execution_keeps_its_lease_renewed_beyond_the_ttl(self):
        """
        A DAST run can outlive the lease TTL by hours. While its Celery task is still alive the
        reconciler must push the lease forward, otherwise the row looks abandoned to anything
        reading the lease table and the stale-lease alert fires on healthy work.
        """
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.DISPATCHED)
        pipeline = self._attach_outbox_pipeline(request, status=AISTStatus.EXECUTING)
        lease = self._lease(request, pipeline=pipeline, now=now)

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "STARTED")

        lease.refresh_from_db()
        pipeline.refresh_from_db()
        self.assertEqual(stats["renewed_live_leases"], 1)
        self.assertIsNone(lease.released_at)
        self.assertEqual(lease.heartbeat_at, now)
        self.assertEqual(lease.expires_at, now + DEFAULT_EXECUTION_LEASE_POLICY.ttl)
        self.assertEqual(pipeline.status, AISTStatus.EXECUTING)
        self.assertEqual(request.failure_code, "")

    def test_dead_dast_task_resumes_known_provider_run_without_releasing_lease(self):
        now = timezone.now()
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.dast_binding,
            trigger_project_version=self.pv,
            state=PipelineLaunchRequestState.DISPATCHED,
            task_name="aist.tasks.pipeline.run_pipeline_execution",
            task_args_snapshot=[],
        )
        pipeline = AISTPipeline.objects.create(
            id=request.task_id.hex,
            project=self.project,
            trigger_project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
            run_task_id=str(request.task_id),
        )
        DastExecutionState.objects.create(
            pipeline=pipeline,
            run_id="provider-run-id",
            log_cursor=9,
            outcome=DastExecutionOutcome.STOP_PENDING,
        )
        request.pipeline = pipeline
        request.save(update_fields=["pipeline", "updated"])
        lease = self._lease(request, pipeline=pipeline, now=now)

        with (
            patch("aist.execution.reconciliation.current_app.send_task") as send_task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "FAILURE")

        request.refresh_from_db()
        pipeline.refresh_from_db()
        execution_state = pipeline.dast_execution_state
        lease.refresh_from_db()
        self.assertEqual(request.failure_code, EXECUTION_RECOVERY_REPUBLISHED)
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)
        self.assertEqual(str(request.task_id), pipeline.run_task_id)
        self.assertEqual(pipeline.status, AISTStatus.EXECUTING)
        self.assertEqual(execution_state.run_id, "provider-run-id")
        self.assertEqual(execution_state.log_cursor, 9)
        self.assertIsNone(lease.released_at)
        self.assertEqual(lease.heartbeat_at, now)
        self.assertEqual(stats["resumed_executions"], 1)
        send_task.assert_called_once_with(
            "aist.tasks.pipeline.run_pipeline_execution",
            args=[pipeline.id],
            task_id=pipeline.run_task_id,
        )

    def test_pipeline_waiting_for_deduplication_is_not_reconciled_as_dead(self):
        """
        A successful execution task is READY while the pipeline waits for dedup/enrich.
        That hand-off must not be reconciled as a dead execution, or the pipeline is forced
        terminal and the enrichment stage (path/severity exclusion) is cut short.
        """
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.DISPATCHED)
        pipeline = self._attach_outbox_pipeline(request, status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        pipeline.run_task_id = None
        pipeline.watch_dedup_task_id = "watch-task-id"
        pipeline.save(update_fields=["run_task_id", "watch_dedup_task_id", "updated"])
        lease = self._lease(request, pipeline=pipeline, now=now)

        stats = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "SUCCESS")

        request.refresh_from_db()
        pipeline.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        self.assertNotIn("execution_reconciliation", pipeline.launch_data or {})
        self.assertNotEqual(request.failure_code, DEAD_EXECUTION_TASK)
        self.assertIsNone(lease.released_at)
        self.assertEqual(lease.heartbeat_at, now)
        self.assertEqual(stats["handed_off_executions"], 1)
        self.assertEqual(stats["reconciled_dead_tasks"], 0)

    def test_terminal_pipeline_leftover_lease_is_released_idempotently(self):
        now = timezone.now()
        request = self._request(state=PipelineLaunchRequestState.DISPATCHED)
        pipeline = self._attach_outbox_pipeline(request, status=AISTStatus.FINISHED)
        pipeline.run_task_id = None
        pipeline.save(update_fields=["run_task_id", "updated"])
        lease = self._lease(request, pipeline=pipeline, now=now)

        first = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "SUCCESS")
        second = reconcile_launch_requests(now=now, task_state_getter=lambda _task_id: "SUCCESS")

        request.refresh_from_db()
        lease.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        self.assertEqual(request.failure_code, TERMINAL_LEASE_RELEASED)
        self.assertEqual(lease.released_at, now)
        self.assertEqual(first["released_leases"], 1)
        self.assertEqual(second["released_leases"], 0)


class LaunchRequestReconciliationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        product_type = Product_Type.objects.create(name=f"Reconciler PT {uuid.uuid4().hex}")
        sla = SLA_Configuration.objects.create(name=f"Reconciler SLA {uuid.uuid4().hex}")
        product = Product.objects.create(
            name=f"Reconciler product {uuid.uuid4().hex}",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
        self.now = timezone.now()
        self.request = PipelineLaunchRequest.objects.create(
            project=project,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner="dead-owner",
            claimed_at=self.now - timedelta(minutes=10),
        )

    def test_two_reconcilers_make_only_one_requeue_transition(self):
        barrier = threading.Barrier(2)
        results = []

        def reconcile() -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                results.append(reconcile_launch_requests(
                    now=self.now,
                    task_state_getter=lambda _task_id: None,
                ))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=reconcile) for _index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), "reconciliation must not deadlock")
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, PipelineLaunchRequestState.PENDING)
        self.assertEqual(sum(result["requeued_claims"] for result in results), 1)
