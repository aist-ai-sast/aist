import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.utils import timezone

from aist.execution.adapters import LaunchAdapterRegistry
from aist.execution.claiming import ClaimResult, claim_next_launch_request, revalidate_claimed_authority
from aist.execution.contracts import PipelineTaskName
from aist.execution.dispatching import (
    LaunchAcceptance,
    LaunchDispatchError,
    LaunchPlanningResult,
    LaunchPlanningStatus,
    LaunchPublishCommand,
    accept_published_launch,
    plan_claimed_launch,
    prepare_launch_publish,
)
from aist.execution.enqueue import LaunchPrincipal, enqueue_pipeline_launch
from aist.execution.sast import SastPipelineLaunchAdapter
from aist.models import (
    AISTPipeline,
    AISTProjectLaunchConfig,
    AISTStatus,
    LaunchSchedule,
    Organization,
    PipelineExecutionLease,
    PipelineLaunchRequestState,
    PullRequest,
    RepositoryInfo,
    ScmType,
)
from aist.tasks import pipeline_dispatcher
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import set_pipeline_status


class GenericPipelineDispatcherTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name=f"Dispatcher org {uuid.uuid4().hex}",
            product_type=self.prod_type,
        )
        self.publisher = Mock()
        self.publisher.apply_async.return_value = Mock(id="ignored-broker-id")

    def _enqueue(self, *, log_level="INFO", capacity=1, enabled=True):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name=f"Dispatcher config {uuid.uuid4().hex}",
            params={
                "project_version": {"id": self.pv.pk},
                "log_level": log_level,
            },
        )
        schedule = LaunchSchedule.objects.create(
            launch_config=config,
            cron_expression="*/5 * * * *",
            enabled=enabled,
            max_concurrent_runs=capacity,
        )
        return enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_schedule(organization=self.organization),
            raw_params={},
            launch_config=config,
            schedule=schedule,
        ).request

    def _dispatch(self, *, batch_size=None):
        with patch.dict(
            pipeline_dispatcher._PUBLISH_TASKS,
            {PipelineTaskName.RUN_SAST_PIPELINE.value: self.publisher},
            clear=True,
        ):
            pipeline_dispatcher.dispatch_queued_pipelines(batch_size=batch_size)

    def test_claim_plan_lease_and_publish_use_one_stable_outbox_identity(self):
        request = self._enqueue()

        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)
        self.assertEqual(request.pipeline_id, request.task_id.hex)
        self.assertEqual(request.pipeline.run_task_id, str(request.task_id))
        self.assertEqual(request.task_name, PipelineTaskName.RUN_SAST_PIPELINE.value)
        self.assertEqual(request.task_args_snapshot[0]["launch_config_id"], request.launch_config_id)
        lease = PipelineExecutionLease.objects.get(request=request)
        self.assertEqual(lease.pipeline_id, request.pipeline_id)
        self.publisher.apply_async.assert_called_once_with(
            args=(request.pipeline_id, *request.task_args_snapshot),
            task_id=str(request.task_id),
        )

    def test_scm_webhook_request_preserves_validated_pull_request_link(self):
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="dispatcher-owner",
            repo_name="dispatcher-repo",
            base_url="https://github.com",
        )
        self.project.repository = repository
        self.project.save(update_fields=["repository"])
        pull_request = PullRequest.objects.create(
            project_version=self.pv,
            repository=repository,
            pr_number=17,
        )
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_scm_webhook(organization=self.organization),
            raw_params={"project_version": self.pv.as_dict(), "pr_launch": True},
            client_request_key="github-pr-17",
            initial_launch_data={"pull_request_id": pull_request.pk},
        ).request

        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)
        self.assertEqual(request.pipeline.pull_request, pull_request)

    def test_worker_accepts_first_delivery_and_duplicate_is_noop(self):
        request = self._enqueue()
        self._dispatch()
        request.refresh_from_db()

        first = accept_published_launch(
            pipeline_id=request.pipeline_id,
            task_id=str(request.task_id),
        )
        duplicate = accept_published_launch(
            pipeline_id=request.pipeline_id,
            task_id=str(request.task_id),
        )

        request.refresh_from_db()
        self.assertEqual(first, LaunchAcceptance.ACCEPTED)
        self.assertEqual(duplicate, LaunchAcceptance.DUPLICATE)
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        self.assertIsNotNone(request.dispatched_at)

    def test_wrong_broker_task_id_is_rejected_without_state_change(self):
        request = self._enqueue()
        self._dispatch()
        request.refresh_from_db()

        result = accept_published_launch(
            pipeline_id=request.pipeline_id,
            task_id="forged-task-id",
        )

        request.refresh_from_db()
        self.assertEqual(result, LaunchAcceptance.REJECTED)
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)

    def test_pipeline_without_launch_request_is_rejected_without_legacy_fallback(self):
        pipeline = AISTPipeline.objects.create(
            id="unmanaged-sast",
            project=self.project,
            project_version=self.pv,
        )

        self.assertEqual(
            accept_published_launch(pipeline_id=pipeline.pk, task_id="legacy-task"),
            LaunchAcceptance.REJECTED,
        )

    def test_crash_after_planning_is_republished_without_second_pipeline(self):
        request = self._enqueue()
        claim = claim_next_launch_request(claim_owner="crashed-dispatcher")
        self.assertTrue(revalidate_claimed_authority(
            request_id=claim.request_id,
            claim_owner=claim.claim_owner,
        ))
        result = plan_claimed_launch(
            request_id=claim.request_id,
            claim_owner=claim.claim_owner,
            adapter_registry=LaunchAdapterRegistry(SastPipelineLaunchAdapter()),
        )
        self.assertEqual(result.status, LaunchPlanningStatus.READY)
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PLANNED)

        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)
        self.assertEqual(AISTPipeline.objects.filter(launch_request=request).count(), 1)
        self.publisher.apply_async.assert_called_once_with(
            args=(request.pipeline_id, *request.task_args_snapshot),
            task_id=str(request.task_id),
        )

    def test_ambiguous_publish_is_retried_with_same_pipeline_and_task_id(self):
        request = self._enqueue()
        self.publisher.apply_async.side_effect = RuntimeError("broker acknowledgement lost")
        self._dispatch()
        request.refresh_from_db()
        first_pipeline_id = request.pipeline_id
        first_task_id = str(request.task_id)
        self.assertEqual(request.state, PipelineLaunchRequestState.PUBLISHED)

        self.publisher.apply_async.reset_mock()
        self.publisher.apply_async.side_effect = None
        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.pipeline_id, first_pipeline_id)
        self.assertEqual(str(request.task_id), first_task_id)
        self.assertEqual(AISTPipeline.objects.filter(launch_request=request).count(), 1)
        self.publisher.apply_async.assert_called_once_with(
            args=(first_pipeline_id, *request.task_args_snapshot),
            task_id=first_task_id,
        )

    def test_busy_resource_keeps_next_request_pending_without_tight_loop(self):
        first = self._enqueue(log_level="INFO", capacity=1)
        second = self._enqueue(log_level="DEBUG", capacity=1)
        dispatch_time = timezone.now()

        with patch("aist.execution.dispatching.timezone.now", return_value=dispatch_time):
            self._dispatch()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, PipelineLaunchRequestState.PUBLISHED)
        self.assertEqual(second.state, PipelineLaunchRequestState.PENDING)
        self.assertIsNone(second.claim_owner)
        self.assertEqual(second.capacity_retry_count, 1)
        self.assertGreaterEqual(second.not_before, dispatch_time + timedelta(seconds=15))
        self.assertLessEqual(second.not_before, dispatch_time + timedelta(seconds=30))
        self.assertEqual(PipelineExecutionLease.objects.filter(released_at__isnull=True).count(), 1)

        self.publisher.apply_async.reset_mock()
        self._dispatch()

        second.refresh_from_db()
        self.assertEqual(second.capacity_retry_count, 1)
        self.assertIsNone(second.pipeline_id)
        self.publisher.apply_async.assert_called_once_with(
            args=(first.pipeline_id, *first.task_args_snapshot),
            task_id=str(first.task_id),
        )

    def test_dispatcher_batch_is_bounded_and_busy_dast_does_not_block_sast(self):
        claims = (
            ClaimResult(request_id=101, claim_owner="dispatcher"),
            ClaimResult(request_id=102, claim_owner="dispatcher"),
            ClaimResult(request_id=103, claim_owner="dispatcher"),
        )
        publish_command = LaunchPublishCommand(
            request_id=102,
            pipeline_id="sast-after-busy-dast",
            task_name=PipelineTaskName.RUN_SAST_PIPELINE.value,
            task_id="sast-task",
            task_args=({},),
        )
        with (
            patch.object(pipeline_dispatcher, "claim_next_launch_request", side_effect=claims) as claim,
            patch.object(pipeline_dispatcher, "revalidate_claimed_authority", return_value=True),
            patch.object(
                pipeline_dispatcher,
                "plan_claimed_launch",
                side_effect=(
                    LaunchPlanningResult(status=LaunchPlanningStatus.BUSY, request_id=101),
                    LaunchPlanningResult(status=LaunchPlanningStatus.READY, request_id=102),
                ),
            ) as plan,
            patch.object(pipeline_dispatcher, "prepare_launch_publish", return_value=publish_command),
            patch.object(pipeline_dispatcher, "_publish") as publish,
        ):
            pipeline_dispatcher.dispatch_queued_pipelines(batch_size=2)

        self.assertEqual(claim.call_count, 2)
        self.assertEqual(plan.call_count, 2)
        publish.assert_called_once_with(publish_command)

    def test_dispatcher_rejects_unbounded_batch_configuration(self):
        for batch_size in (0, 201, True, "50"):
            with self.subTest(batch_size=batch_size), self.assertRaises(ValueError):
                pipeline_dispatcher.dispatch_queued_pipelines(batch_size=batch_size)

    def test_request_past_max_age_expires_without_creating_pipeline(self):
        request = self._enqueue()
        dispatch_time = timezone.now()
        type(request).objects.filter(pk=request.pk).update(expires_at=dispatch_time)

        with patch("aist.execution.claiming.timezone.now", return_value=dispatch_time):
            self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.EXPIRED)
        self.assertEqual(request.failure_code, "LAUNCH_MAX_AGE_EXCEEDED")
        self.assertIsNone(request.pipeline_id)
        self.assertFalse(PipelineExecutionLease.objects.filter(request=request).exists())
        self.publisher.apply_async.assert_not_called()

    def test_existing_unfinished_version_restores_pending_and_releases_temporary_lease(self):
        request = self._enqueue()
        AISTPipeline.objects.create(
            id="already-running",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.SAST_LAUNCHED,
        )

        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.PENDING)
        self.assertIsNone(request.pipeline_id)
        self.assertFalse(PipelineExecutionLease.objects.filter(released_at__isnull=True).exists())
        self.publisher.apply_async.assert_not_called()

    def test_disabled_schedule_fails_authority_before_pipeline_creation(self):
        request = self._enqueue(enabled=False)

        self._dispatch()

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.FAILED)
        self.assertEqual(request.failure_code, "AUTHORITY_REVOKED")
        self.assertIsNone(request.pipeline_id)
        self.publisher.apply_async.assert_not_called()

    def test_prepare_publish_rejects_non_outbox_state(self):
        request = self._enqueue()

        with self.assertRaises(LaunchDispatchError):
            prepare_launch_publish(request_id=request.pk)

    def test_terminal_pipeline_releases_its_execution_lease(self):
        request = self._enqueue()
        self._dispatch()
        request.refresh_from_db()
        accept_published_launch(
            pipeline_id=request.pipeline_id,
            task_id=str(request.task_id),
        )
        pipeline = request.pipeline
        set_pipeline_status(pipeline, AISTStatus.SAST_LAUNCHED)
        set_pipeline_status(pipeline, AISTStatus.FINISHED)

        self.assertIsNotNone(PipelineExecutionLease.objects.get(request=request).released_at)


class SetPipelineStatusRunTaskIdTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        Organization.objects.create(
            name=f"Pipeline status org {uuid.uuid4().hex}",
            product_type=self.prod_type,
        )

    def _pipeline(self, *, status, run_task_id=None):
        return AISTPipeline.objects.create(
            id=uuid.uuid4().hex[:8],
            project=self.project,
            project_version=self.pv,
            status=status,
            run_task_id=run_task_id,
        )

    def test_terminal_status_clears_run_task_id(self):
        pipeline = self._pipeline(status=AISTStatus.SAST_LAUNCHED, run_task_id="task-abc")

        set_pipeline_status(pipeline, AISTStatus.FINISHED)

        pipeline.refresh_from_db()
        self.assertIsNone(pipeline.run_task_id)

    def test_non_terminal_status_keeps_run_task_id(self):
        pipeline = self._pipeline(status=AISTStatus.FINISHED, run_task_id="task-abc")

        set_pipeline_status(pipeline, AISTStatus.SAST_LAUNCHED)

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.run_task_id, "task-abc")
