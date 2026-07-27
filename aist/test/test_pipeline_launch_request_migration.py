from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PipelineLaunchRequestMigrationTests(TransactionTestCase):
    migrate_from = [("aist", "0041_tenant_integrity_invariants")]
    migrate_to = [("aist", "0042_dast_generic_execution")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        ProductType = old_apps.get_model("dojo", "Product_Type")
        Product = old_apps.get_model("dojo", "Product")
        SLAConfiguration = old_apps.get_model("dojo", "SLA_Configuration")
        AISTProject = old_apps.get_model("aist", "AISTProject")
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        LaunchSchedule = old_apps.get_model("aist", "LaunchSchedule")
        PipelineLaunchQueue = old_apps.get_model("aist", "PipelineLaunchQueue")

        product_type = ProductType.objects.create(name="Launch request migration PT")
        sla = SLAConfiguration.objects.create(name="Launch request migration SLA")
        product = Product.objects.create(
            name="Launch request migration Product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
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

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

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

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()
