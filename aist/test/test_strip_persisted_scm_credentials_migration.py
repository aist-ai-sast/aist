"""Migration 0053 scrubs the SCM credential out of params already in the database."""
from __future__ import annotations

from django.db import DatabaseError, transaction

from aist.test.migration_state import (
    HistoricalMigrationTestCase,
    seed_historical_organization,
    seed_historical_project,
    seed_historical_version,
)

CLONE_URL = "https://oauth2:{}@gitlab.example.com/group/repo.git".format(
    "xglpat-migration1234567890".removeprefix("x"),
)


class StripPersistedScmCredentialsMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0052_dastintegrationstate_sync_status")]
    migrate_to = [("aist", "0053_strip_persisted_scm_credentials")]

    def prepare(self, old_apps):
        AISTProjectLaunchConfig = old_apps.get_model("aist", "AISTProjectLaunchConfig")
        PipelineLaunchRequest = old_apps.get_model("aist", "PipelineLaunchRequest")

        organization = seed_historical_organization(old_apps, "Scrub credentials")
        project = seed_historical_project(old_apps, "Scrub credentials", product_type=organization.product_type)
        version = seed_historical_version(old_apps, project)

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
