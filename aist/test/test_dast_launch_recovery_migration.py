from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DastLaunchRecoveryMigrationTests(TransactionTestCase):
    migrate_from = [("aist", "0042_dast_generic_execution")]
    migrate_to = [("aist", "0043_pipeline_lifecycle_states")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        ProductType = old_apps.get_model("dojo", "Product_Type")
        Product = old_apps.get_model("dojo", "Product")
        SLAConfiguration = old_apps.get_model("dojo", "SLA_Configuration")
        Organization = old_apps.get_model("aist", "Organization")
        OrgIntegration = old_apps.get_model("aist", "OrgIntegration")
        DastTarget = old_apps.get_model("aist", "DastTarget")
        DastProjectBinding = old_apps.get_model("aist", "DastProjectBinding")
        AISTProject = old_apps.get_model("aist", "AISTProject")
        AISTProjectVersion = old_apps.get_model("aist", "AISTProjectVersion")
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        AISTLaunchConfigAction = old_apps.get_model("aist", "AISTLaunchConfigAction")
        AISTPipeline = old_apps.get_model("aist", "AISTPipeline")
        LaunchSchedule = old_apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchRequest = old_apps.get_model("aist", "PipelineLaunchRequest")

        product_type = ProductType.objects.create(name="DAST recovery migration PT")
        organization = Organization.objects.create(
            name="DAST recovery migration org",
            product_type=product_type,
        )
        sla = SLAConfiguration.objects.create(name="DAST recovery migration SLA")
        product = Product.objects.create(
            name="DAST recovery migration product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
        version = AISTProjectVersion.objects.create(
            project=project,
            version="main",
            version_type="GIT_BRANCH",
        )
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type="DAST",
            name="Migration gateway",
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="migration-api",
            display_name="Migration API",
            contract_revision="2.0",
            capability_revision="sha256:capability",
            schema_digest="sha256:schema",
            parameter_schema={"type": "object", "properties": {}},
            provider_defaults={},
            repository_keys=["source"],
            autonomous_ready=True,
            last_seen_at="2026-07-29T00:00:00Z",
        )
        binding = DastProjectBinding.objects.create(
            project=project,
            target=target,
            source_repo_key="source",
        )

        terminal = AISTPipeline.objects.create(
            id="historical-terminal",
            project=project,
            project_version=version,
            status="FINISHED",
        )
        active = AISTPipeline.objects.create(
            id="active-outbox",
            project=project,
            project_version=version,
            status="FINISHED",
            run_task_id="active-task",
        )
        AISTPipeline.objects.create(
            id="legacy-executing",
            project=project,
            project_version=version,
            status="SAST_LAUNCHED",
        )
        PipelineLaunchRequest.objects.create(
            project=project,
            execution_type="SAST",
            state="PUBLISHED",
            pipeline=active,
            task_name="aist.tasks.pipeline.run_pipeline_execution",
            task_args_snapshot=[],
        )

        sast_config = AISTProjectLaunchConfig.objects.create(
            project=project,
            name="SAST preset",
            params={"project_version": {"id": version.pk}},
        )
        AISTLaunchConfigAction.objects.create(
            launch_config=sast_config,
            trigger_status="SAST_LAUNCHED",
            action_type="CUSTOM_SCRIPT",
            config={},
        )

        dast_pipeline = AISTPipeline.objects.create(
            id="preserved-dast-history",
            project=project,
            trigger_project_version=version,
            execution_type="DAST",
            status="SAST_LAUNCHED",
            external_run_id="provider-run",
            external_log_cursor=17,
            external_execution_outcome="STOP_PENDING",
        )
        dast_config = AISTProjectLaunchConfig.objects.create(
            project=project,
            name="Invalid legacy DAST preset",
            execution_type="DAST",
            dast_binding=binding,
            params={},
        )
        schedule = LaunchSchedule.objects.create(
            launch_config=dast_config,
            cron_expression="*/5 * * * *",
        )
        request = PipelineLaunchRequest.objects.create(
            project=project,
            execution_type="DAST",
            dast_binding=binding,
            trigger_project_version=version,
            launch_config=dast_config,
            schedule=schedule,
            state="DISPATCHED",
            pipeline=dast_pipeline,
        )

        self.active_id = active.pk
        self.terminal_id = terminal.pk
        self.dast_pipeline_id = dast_pipeline.pk
        self.dast_config_id = dast_config.pk
        self.dast_request_id = request.pk
        self.dast_schedule_id = schedule.pk

        connection.commit()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_lifecycle_and_dast_launch_control_are_migrated_without_losing_history(self):
        AISTPipeline = self.apps.get_model("aist", "AISTPipeline")
        AISTProjectLaunchConfig = self.apps.get_model("aist", "AISTProjectLaunchConfig")
        AISTLaunchConfigAction = self.apps.get_model("aist", "AISTLaunchConfigAction")
        DastExecutionState = self.apps.get_model("aist", "DastExecutionState")
        LaunchSchedule = self.apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchRequest = self.apps.get_model("aist", "PipelineLaunchRequest")

        active = AISTPipeline.objects.get(pk=self.active_id)
        self.assertEqual(active.status, "ADMITTED")
        self.assertIsNone(active.started)
        self.assertIsNone(active.finished_at)

        terminal = AISTPipeline.objects.get(pk=self.terminal_id)
        self.assertEqual(terminal.status, "FINISHED")
        self.assertIsNotNone(terminal.finished_at)
        self.assertEqual(AISTPipeline.objects.get(pk="legacy-executing").status, "EXECUTING")
        self.assertEqual(AISTLaunchConfigAction.objects.get().trigger_status, "EXECUTING")

        dast_pipeline = AISTPipeline.objects.get(pk=self.dast_pipeline_id)
        self.assertEqual(dast_pipeline.status, "EXECUTING")
        state = DastExecutionState.objects.get(pipeline_id=dast_pipeline.pk)
        self.assertEqual(state.run_id, "provider-run")
        self.assertEqual(state.log_cursor, 17)
        self.assertEqual(state.outcome, "STOP_PENDING")

        self.assertFalse(AISTProjectLaunchConfig.objects.filter(pk=self.dast_config_id).exists())
        self.assertFalse(PipelineLaunchRequest.objects.filter(pk=self.dast_request_id).exists())
        self.assertFalse(LaunchSchedule.objects.filter(pk=self.dast_schedule_id).exists())
        self.assertTrue(AISTPipeline.objects.filter(pk=self.dast_pipeline_id).exists())
