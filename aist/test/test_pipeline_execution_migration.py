from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PipelineExecutionMigrationTests(TransactionTestCase):
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
        AISTProjectVersion = old_apps.get_model("aist", "AISTProjectVersion")
        AISTPipeline = old_apps.get_model("aist", "AISTPipeline")

        product_type = ProductType.objects.create(name="Pipeline execution migration PT")
        sla = SLAConfiguration.objects.create(name="Pipeline execution migration SLA")
        product = Product.objects.create(
            name="Pipeline execution migration Product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
        version = AISTProjectVersion.objects.create(
            project=project,
            version="0123456789abcdef0123456789abcdef01234567",
            version_type="GIT_HASH",
        )
        AISTPipeline.objects.create(
            id="legacy-sast",
            project=project,
            project_version=version,
        )
        AISTPipeline.objects.create(
            id="legacy-manual-import",
            project=project,
            launch_data={"source": "manual_import", "sha256": "deadbeef"},
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_sast_and_manual_import_rows_are_classified(self):
        AISTPipeline = self.apps.get_model("aist", "AISTPipeline")

        self.assertEqual(AISTPipeline.objects.get(pk="legacy-sast").execution_type, "SAST")
        self.assertEqual(
            AISTPipeline.objects.get(pk="legacy-manual-import").execution_type,
            "MANUAL_IMPORT",
        )
