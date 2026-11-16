
from aist.test.migration_state import (
    HistoricalMigrationTestCase,
    seed_historical_organization,
    seed_historical_project,
    seed_historical_version,
)


class GenericExecutionForwardMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0041_tenant_integrity_invariants")]
    migrate_to = [("aist", "0042_dast_generic_execution")]

    def prepare(self, old_apps):
        AISTPipeline = old_apps.get_model("aist", "AISTPipeline")
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        LaunchSchedule = old_apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchQueue = old_apps.get_model("aist", "PipelineLaunchQueue")

        organization = seed_historical_organization(old_apps, "Generic execution migration")
        project = seed_historical_project(
            old_apps, "Generic execution migration", product_type=organization.product_type,
        )
        version = seed_historical_version(
            old_apps, project, version="0123456789abcdef0123456789abcdef01234567", version_type="GIT_HASH",
        )
        AISTPipeline.objects.create(
            id="pre-generic-sast",
            project=project,
            project_version=version,
        )
        AISTPipeline.objects.create(
            id="pre-generic-manual",
            project=project,
            launch_data={"source": "manual_import", "scan_type": "DAST Autonomous Scan"},
        )
        launch_config = AISTProjectLaunchConfig.objects.create(
            project=project,
            name="Pre-generic config",
            params={"project_version": {"id": version.id}, "analyzers": ["semgrep"]},
            is_default=True,
        )
        schedule = LaunchSchedule.objects.create(
            cron_expression="15 3 * * *",
            enabled=True,
            launch_config=launch_config,
        )
        self.queue_id = PipelineLaunchQueue.objects.create(
            project=project,
            schedule=schedule,
            launch_config=launch_config,
            dispatched=False,
        ).id
        self.project_id = project.id
        self.config_id = launch_config.id
        self.schedule_id = schedule.id

    def test_pre_generic_sast_data_survives_with_deterministic_types(self):
        AISTPipeline = self.apps.get_model("aist", "AISTPipeline")
        AISTProjectLaunchConfig = self.apps.get_model("aist", "AISTProjectLaunchConfig")
        LaunchSchedule = self.apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchRequest = self.apps.get_model("aist", "PipelineLaunchRequest")

        self.assertEqual(AISTPipeline.objects.get(pk="pre-generic-sast").execution_type, "SAST")
        self.assertEqual(AISTPipeline.objects.get(pk="pre-generic-manual").execution_type, "MANUAL_IMPORT")

        config = AISTProjectLaunchConfig.objects.get(pk=self.config_id)
        self.assertEqual(config.project_id, self.project_id)
        self.assertEqual(config.execution_type, "SAST")
        self.assertIsNone(config.dast_binding_id)
        self.assertEqual(config.params["analyzers"], ["semgrep"])
        self.assertEqual(LaunchSchedule.objects.get(pk=self.schedule_id).launch_config_id, config.id)

        request = PipelineLaunchRequest.objects.get(pk=self.queue_id)
        self.assertEqual(request.state, "PENDING")
        self.assertEqual(request.origin, "SCHEDULE")
        self.assertEqual(request.execution_type, "SAST")
        self.assertEqual(request.params_snapshot, config.params)
        self.assertIsNotNone(request.task_id)
