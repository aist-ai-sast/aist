
from aist.test.migration_state import HistoricalMigrationTestCase, seed_historical_project


class PipelineLaunchRequestMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0041_tenant_integrity_invariants")]
    migrate_to = [("aist", "0042_dast_generic_execution")]

    def prepare(self, old_apps):
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        LaunchSchedule = old_apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchQueue = old_apps.get_model("aist", "PipelineLaunchQueue")

        project = seed_historical_project(old_apps, "Launch request migration")
        launch_config = AISTProjectLaunchConfig.objects.create(
            project=project,
            name="Legacy SAST config",
            params={"project_version": {"id": 42}, "analyzers": ["semgrep"]},
        )
        schedule = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            launch_config=launch_config,
        )
        self.scheduled_id = PipelineLaunchQueue.objects.create(
            project=project,
            launch_config=launch_config,
            schedule=schedule,
            dispatched=True,
        ).id
        self.manual_id = PipelineLaunchQueue.objects.create(
            project=project,
            launch_config=launch_config,
            dispatched=False,
        ).id

    def test_legacy_rows_are_backfilled_as_sast_requests(self):
        LaunchRequest = self.apps.get_model("aist", "PipelineLaunchRequest")
        scheduled = LaunchRequest.objects.get(pk=self.scheduled_id)
        manual = LaunchRequest.objects.get(pk=self.manual_id)

        self.assertEqual(scheduled.execution_type, "SAST")
        self.assertEqual(scheduled.origin, "SCHEDULE")
        self.assertEqual(scheduled.authority_kind, "SCHEDULE")
        self.assertEqual(scheduled.state, "DISPATCHED")
        self.assertEqual(scheduled.params_snapshot["analyzers"], ["semgrep"])
        self.assertIsNotNone(scheduled.task_id)
        self.assertEqual(manual.origin, "MANUAL")
        self.assertEqual(manual.authority_kind, "USER")
        self.assertEqual(manual.state, "PENDING")
