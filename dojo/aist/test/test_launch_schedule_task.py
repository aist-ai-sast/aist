# dojo/aist/test/test_launch_schedule_task.py
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from unittest.mock import patch

from django.utils import timezone

from dojo.aist.models import AISTProjectLaunchConfig, LaunchSchedule, PipelineLaunchQueue
from dojo.aist.tasks.launch_schedule import process_launch_schedules
from dojo.aist.test.test_api import AISTApiBase


class ProcessLaunchSchedulesTests(AISTApiBase):
    def _mk_config_and_schedule(
        self,
        *,
        enabled: bool = True,
        cron_expression: str = "*/5 * * * *",
        max_concurrent_per_worker: int = 0,
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
            max_concurrent_per_worker=max_concurrent_per_worker,
            launch_config=cfg,
            last_run_at=last_run_at,
        )
        return cfg, sched

    def test_disabled_schedule_skips(self):
        _, sched = self._mk_config_and_schedule(enabled=False)

        process_launch_schedules()

        self.assertEqual(PipelineLaunchQueue.objects.count(), 0)
        sched.refresh_from_db()
        self.assertIsNone(sched.last_run_at)

    @patch("dojo.aist.tasks.launch_schedule.logger")
    def test_invalid_cron_expression_is_logged_and_skipped(self, mock_logger):
        _, sched = self._mk_config_and_schedule(cron_expression="not a cron")

        process_launch_schedules()

        self.assertEqual(PipelineLaunchQueue.objects.count(), 0)
        sched.refresh_from_db()
        self.assertIsNone(sched.last_run_at)
        mock_logger.exception.assert_called()  # ensures exception branch executed

    def test_not_due_when_last_run_at_same_tick(self):
        _, sched = self._mk_config_and_schedule()

        now = timezone.now()
        due_time = now - timedelta(minutes=5)

        # last_run_at >= due_time => already processed tick => skip
        LaunchSchedule.objects.filter(id=sched.id).update(last_run_at=now)

        with patch.object(LaunchSchedule, "get_next_run_time", return_value=due_time):
            process_launch_schedules()

        self.assertEqual(PipelineLaunchQueue.objects.count(), 0)

    def test_due_enqueues_queue_item_and_updates_last_run_at(self):
        cfg, sched = self._mk_config_and_schedule()

        now = timezone.now()
        due_time = now - timedelta(minutes=5)

        with (
            patch("dojo.aist.tasks.launch_schedule.timezone.now", return_value=now),
            patch.object(LaunchSchedule, "get_next_run_time", return_value=due_time),
        ):
            process_launch_schedules()

        self.assertEqual(PipelineLaunchQueue.objects.count(), 1)
        item = PipelineLaunchQueue.objects.get()
        self.assertEqual(item.project_id, self.project.id)
        self.assertEqual(item.schedule_id, sched.id)
        self.assertEqual(item.launch_config_id, cfg.id)
        self.assertFalse(item.dispatched)

        sched.refresh_from_db()
        self.assertEqual(sched.last_run_at, now)

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
            patch("dojo.aist.tasks.launch_schedule.timezone.now", return_value=now),
            patch.object(LaunchSchedule, "get_next_run_time", return_value=due_time),
        ):
            process_launch_schedules()

        # last_run_at (12:00) >= due_time (11:55) => skip
        self.assertEqual(PipelineLaunchQueue.objects.count(), 0)
