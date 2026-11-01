"""Migration 0053 scrubs the SCM credential out of params already in the database."""
from __future__ import annotations

from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from aist.test.migration_state import historical_state

CLONE_URL = "https://oauth2:{}@gitlab.example.com/group/repo.git".format(
    "xglpat-migration1234567890".removeprefix("x"),
)


class StripPersistedScmCredentialsMigrationTests(TransactionTestCase):
    migrate_from = [("aist", "0052_dastintegrationstate_sync_status")]
    migrate_to = [("aist", "0053_strip_persisted_scm_credentials")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        pinned = historical_state(executor, self.migrate_from)
        executor.migrate(pinned)
        old_apps = executor.loader.project_state(pinned).apps

        ProductType = old_apps.get_model("dojo", "Product_Type")
        Product = old_apps.get_model("dojo", "Product")
        SLAConfiguration = old_apps.get_model("dojo", "SLA_Configuration")
        Organization = old_apps.get_model("aist", "Organization")
        AISTProject = old_apps.get_model("aist", "AISTProject")
        AISTProjectVersion = old_apps.get_model("aist", "AISTProjectVersion")
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        PipelineLaunchRequest = old_apps.get_model("aist", "PipelineLaunchRequest")

        product_type = ProductType.objects.create(name="Scrub credentials PT")
        Organization.objects.create(name="Scrub credentials org", product_type=product_type)
        sla = SLAConfiguration.objects.create(name="Scrub credentials SLA")
        product = Product.objects.create(
            name="Scrub credentials product",
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

        # Several credentialed rows, so the batching loop runs with more than one pending
        # instance, plus one row that is already clean and must not be rewritten.
        self.config_ids = [
            AISTProjectLaunchConfig.objects.create(
                project=project,
                name=f"Credentialed preset {index}",
                execution_type="SAST",
                params={
                    "project_version": {"id": version.id},
                    "analyzers": ["semgrep"],
                    "env": {
                        "REPO_URL": CLONE_URL,
                        "PROJECT_NAME": "scrub_credentials_product",
                        "FOO": "1",
                    },
                },
            ).id
            for index in range(3)
        ]
        self.clean_config_id = AISTProjectLaunchConfig.objects.create(
            project=project,
            name="Already clean preset",
            execution_type="SAST",
            params={"project_version": {"id": version.id}, "env": {"FOO": "1"}},
        ).id
        self.request_ids = [
            PipelineLaunchRequest.objects.create(
                project=project,
                execution_type="SAST",
                origin="MANUAL",
                authority_kind="USER",
                params_snapshot={
                    "project_id": project.id,
                    "project_version": {"id": version.id},
                    "env": {
                        "REPO_URL": CLONE_URL,
                        "PROJECT_NAME": "scrub_credentials_product",
                        "FOO": "1",
                    },
                },
            ).id
            for _ in range(3)
        ]

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_every_launch_config_keeps_only_the_operator_env(self):
        AISTProjectLaunchConfig = self.apps.get_model("aist", "AISTProjectLaunchConfig")

        for config_id in self.config_ids:
            config = AISTProjectLaunchConfig.objects.get(pk=config_id)

            self.assertEqual(config.params["env"], {"FOO": "1"})
            self.assertEqual(config.params["analyzers"], ["semgrep"])
            self.assertNotIn(CLONE_URL, str(config.params))

    def test_already_clean_launch_config_is_untouched(self):
        AISTProjectLaunchConfig = self.apps.get_model("aist", "AISTProjectLaunchConfig")

        config = AISTProjectLaunchConfig.objects.get(pk=self.clean_config_id)

        self.assertEqual(config.params["env"], {"FOO": "1"})

    def test_every_launch_request_snapshot_keeps_only_the_operator_env(self):
        PipelineLaunchRequest = self.apps.get_model("aist", "PipelineLaunchRequest")

        for request_id in self.request_ids:
            request = PipelineLaunchRequest.objects.get(pk=request_id)

            self.assertEqual(request.params_snapshot["env"], {"FOO": "1"})
            self.assertIn("project_version", request.params_snapshot)
            self.assertNotIn(CLONE_URL, str(request.params_snapshot))

    def test_snapshot_immutability_trigger_is_restored(self):
        PipelineLaunchRequest = self.apps.get_model("aist", "PipelineLaunchRequest")
        request = PipelineLaunchRequest.objects.get(pk=self.request_ids[0])
        request.params_snapshot = {"env": {"FOO": "tampered"}}

        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            request.save(update_fields=["params_snapshot"])

        self.assertIn("immutable", str(caught.exception).lower())
