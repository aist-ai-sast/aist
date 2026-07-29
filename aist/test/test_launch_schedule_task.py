# aist/test/test_launch_schedule_task.py
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from unittest.mock import patch

from django.utils import timezone

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTProjectLaunchConfig,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.tasks.launch_schedule import process_launch_schedules
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire


class ProcessLaunchSchedulesTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Schedule task organization",
            product_type=self.prod_type,
        )

    def _mk_config_and_schedule(
        self,
        *,
        enabled: bool = True,
        cron_expression: str = "*/5 * * * *",
        max_concurrent_runs: int = 0,
        last_run_at=None,
    ):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Default",
            params={"project_version": {"id": self.pv.id}},
            is_default=True,
        )
        sched = LaunchSchedule.objects.create(
            cron_expression=cron_expression,
            enabled=enabled,
            max_concurrent_runs=max_concurrent_runs,
            launch_config=cfg,
            last_run_at=last_run_at,
        )
        return cfg, sched

    def _mk_dast_config_and_schedule(self):
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="Scheduled DAST",
            config=_integration_config("scheduled-dast"),
            secret="scheduled-runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        target = refresh_dast_targets(
            integration,
            [DastTargetSnapshot.from_snapshot(_target_wire("scheduled-api"))],
            seen_at=timezone.now(),
        )[0]
        DastIntegrationState.objects.create(
            integration=integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=timezone.now(),
            contract_version="2.0",
            capabilities_etag="scheduled-catalog",
            capabilities_synced_at=timezone.now(),
        )
        binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="scheduled-api",
            parameter_snapshot={"depth": "light"},
            autonomous_enabled=True,
        )
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Scheduled DAST config",
            execution_type=PipelineExecutionType.DAST,
            dast_binding=binding,
            trigger_project_version=self.pv,
            params={"depth": "light"},
        )
        schedule = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
            launch_config=config,
        )
        return binding, config, schedule

    def test_disabled_schedule_skips(self):
        _, sched = self._mk_config_and_schedule(enabled=False)

        process_launch_schedules()

        self.assertEqual(PipelineLaunchRequest.objects.count(), 0)
        sched.refresh_from_db()
        self.assertIsNone(sched.last_run_at)

    @patch("aist.tasks.launch_schedule.logger")
    def test_invalid_cron_expression_is_logged_and_skipped(self, mock_logger):
        _, sched = self._mk_config_and_schedule(cron_expression="not a cron")
        LaunchSchedule.objects.filter(pk=sched.pk).update(next_run_at=timezone.now())

        process_launch_schedules()

        self.assertEqual(PipelineLaunchRequest.objects.count(), 0)
        sched.refresh_from_db()
        self.assertIsNone(sched.last_run_at)
        self.assertEqual(sched.last_error_code, "INVALID_CRON")
        self.assertIn("invalid", sched.last_error_detail)
        mock_logger.exception.assert_not_called()

    def test_not_due_when_last_run_at_same_tick(self):
        _, sched = self._mk_config_and_schedule()

        now = timezone.now()
        LaunchSchedule.objects.filter(id=sched.id).update(last_run_at=now, next_run_at=now + timedelta(minutes=5))
        process_launch_schedules()

        self.assertEqual(PipelineLaunchRequest.objects.count(), 0)

    def test_due_enqueues_queue_item_and_updates_last_run_at(self):
        cfg, sched = self._mk_config_and_schedule()

        now = timezone.now()
        due_time = now - timedelta(minutes=5)

        LaunchSchedule.objects.filter(pk=sched.pk).update(next_run_at=due_time)
        with patch("aist.tasks.launch_schedule.timezone.now", return_value=now):
            process_launch_schedules()

        sched.refresh_from_db()
        self.assertEqual(PipelineLaunchRequest.objects.count(), 1, sched.last_error_detail)
        item = PipelineLaunchRequest.objects.get()
        self.assertEqual(item.project_id, self.project.id)
        self.assertEqual(item.schedule_id, sched.id)
        self.assertEqual(item.launch_config_id, cfg.id)
        self.assertFalse(item.dispatched)

        sched.refresh_from_db()
        self.assertEqual(sched.last_run_at, due_time)

    def test_due_dast_schedule_freezes_its_exact_binding_and_replays_one_tick(self):
        binding, config, schedule = self._mk_dast_config_and_schedule()
        now = timezone.now()
        due_time = now - timedelta(minutes=5)

        LaunchSchedule.objects.filter(pk=schedule.pk).update(next_run_at=due_time)
        with patch("aist.tasks.launch_schedule.timezone.now", return_value=now):
            process_launch_schedules()
            LaunchSchedule.objects.filter(pk=schedule.pk).update(last_run_at=None, next_run_at=due_time)
            process_launch_schedules()

        schedule.refresh_from_db()
        self.assertEqual(PipelineLaunchRequest.objects.count(), 1, schedule.last_error_detail)
        request = PipelineLaunchRequest.objects.get()
        self.assertEqual(request.execution_type, PipelineExecutionType.DAST)
        self.assertEqual(request.dast_binding_id, binding.pk)
        self.assertEqual(request.launch_config_id, config.pk)
        self.assertEqual(request.schedule_id, schedule.pk)
        self.assertEqual(request.params_snapshot, {"depth": "light"})
        self.assertEqual(request.capability_snapshot["id"], "scheduled-api")

    def test_overlapping_dast_schedule_ticks_leave_one_pending_request(self):
        _binding, _config, schedule = self._mk_dast_config_and_schedule()
        now = timezone.now()
        first_due_time = now - timedelta(minutes=10)
        second_due_time = now - timedelta(minutes=5)

        with patch("aist.tasks.launch_schedule.timezone.now", return_value=now):
            LaunchSchedule.objects.filter(pk=schedule.pk).update(next_run_at=first_due_time)
            process_launch_schedules()
            LaunchSchedule.objects.filter(pk=schedule.pk).update(last_run_at=None, next_run_at=second_due_time)
            process_launch_schedules()

        schedule.refresh_from_db()
        self.assertEqual(PipelineLaunchRequest.objects.count(), 2, schedule.last_error_detail)
        pending = PipelineLaunchRequest.objects.get(state=PipelineLaunchRequestState.PENDING)
        superseded = PipelineLaunchRequest.objects.get(state=PipelineLaunchRequestState.SUPERSEDED)
        self.assertEqual(superseded.superseded_by_id, pending.pk)
        self.assertEqual(superseded.coalesce_key, pending.coalesce_key)

    def test_naive_last_run_at_is_handled(self):
        """Ensure naive last_run_at is handled without crashing comparisons."""
        naive_last = datetime(2026, 1, 1, 12, 0, 0)  # naive
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"DateTimeField LaunchSchedule.last_run_at received a naive datetime.*",
                category=RuntimeWarning,
            )
            _, _sched = self._mk_config_and_schedule(last_run_at=naive_last)

        now = timezone.now()
        due_time = timezone.make_aware(datetime(2026, 1, 1, 11, 55, 0), timezone.get_default_timezone())

        with (
            patch("aist.tasks.launch_schedule.timezone.now", return_value=now),
            patch.object(LaunchSchedule, "get_next_run_time", return_value=due_time),
        ):
            process_launch_schedules()

        # last_run_at (12:00) >= due_time (11:55) => skip
        self.assertEqual(PipelineLaunchRequest.objects.count(), 0)
