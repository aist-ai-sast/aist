
from aist.test.migration_state import HistoricalMigrationTestCase, seed_historical_project, seed_historical_version


class PipelineExecutionMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0041_tenant_integrity_invariants")]
    migrate_to = [("aist", "0042_dast_generic_execution")]

    def prepare(self, old_apps):
        AISTPipeline = old_apps.get_model("aist", "AISTPipeline")

        project = seed_historical_project(old_apps, "Pipeline execution migration")
        version = seed_historical_version(
            old_apps, project, version="0123456789abcdef0123456789abcdef01234567", version_type="GIT_HASH",
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

    def test_existing_sast_and_manual_import_rows_are_classified(self):
        AISTPipeline = self.apps.get_model("aist", "AISTPipeline")

        self.assertEqual(AISTPipeline.objects.get(pk="legacy-sast").execution_type, "SAST")
        self.assertEqual(
            AISTPipeline.objects.get(pk="legacy-manual-import").execution_type,
            "MANUAL_IMPORT",
        )
