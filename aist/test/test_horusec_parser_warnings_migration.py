"""Migration 0062 finishes cleanly the SAST runs with no findings whose only recorded warning is the Horusec parser failure."""
from __future__ import annotations

from django.utils import timezone

from aist.test.migration_state import (
    HistoricalMigrationTestCase,
    seed_historical_finding,
    seed_historical_scan_owner,
    seed_historical_test,
)

PARSER_FAILURE = {"source": "analyzer:horusec", "code": "import_failed", "message": "Report import failed: TypeError"}
BRIDGE_FAILURE = {"source": "analyzer:claude-diff-security", "code": "bridge_error", "message": "Internal Server Error"}


class HorusecParserWarningsMigrationTests(HistoricalMigrationTestCase):
    migrate_from = [("aist", "0061_relative_source_links")]
    migrate_to = [("aist", "0062_clear_horusec_parser_warnings")]

    def prepare(self, old_apps):
        owner = seed_historical_scan_owner(old_apps, "horusec-warning-migration", test_type="Semgrep JSON Report")
        pipeline_model = old_apps.get_model("aist", "AISTPipeline")

        def run(run_id, *, reasons, findings=0, status="FINISHED_WITH_WARNINGS", execution_type="SAST", **launch_data):
            test = seed_historical_test(old_apps, owner)
            for index in range(findings):
                seed_historical_finding(old_apps, test, owner.user, title=f"Finding {index}", hash_code=f"{run_id}-{index}")
            pipeline = pipeline_model.objects.create(
                id=run_id, project=owner.project,
                project_version=owner.version if execution_type == "SAST" else None,
                status=status, execution_type=execution_type, finished_at=timezone.now(),
                launch_data={"output_dir": f"/srv/aist/output/{run_id}", "analyzer_degraded_reasons": reasons, **launch_data},
            )
            pipeline.tests.add(test)
            return run_id

        self.clean_scan = run("clean-scan", reasons=[PARSER_FAILURE])
        other_import = {**PARSER_FAILURE, "message": "Report import failed: ValidationError"}
        self.kept = {
            run("found-something", reasons=[PARSER_FAILURE], findings=2):
                ("FINISHED_WITH_WARNINGS", [PARSER_FAILURE]),
            run("bridge-failed", reasons=[BRIDGE_FAILURE, PARSER_FAILURE]):
                ("FINISHED_WITH_WARNINGS", [BRIDGE_FAILURE, PARSER_FAILURE]),
            run("other-import", reasons=[other_import]): ("FINISHED_WITH_WARNINGS", [other_import]),
            run("no-reason", reasons=[]): ("FINISHED_WITH_WARNINGS", []),
            run("reconciled", reasons=[PARSER_FAILURE], execution_reconciliation={"code": "DEAD_EXECUTION_TASK"}):
                ("FINISHED_WITH_WARNINGS", [PARSER_FAILURE]),
            run("still-running", reasons=[PARSER_FAILURE], status="WAITING_RESULT_FROM_AI"):
                ("WAITING_RESULT_FROM_AI", [PARSER_FAILURE]),
            run("manual-import", reasons=[PARSER_FAILURE], execution_type="MANUAL_IMPORT"):
                ("FINISHED_WITH_WARNINGS", [PARSER_FAILURE]),
        }

    def _pipeline(self, run_id):
        return self.apps.get_model("aist", "AISTPipeline").objects.get(id=run_id)

    def test_a_run_that_found_nothing_and_only_tripped_the_horusec_parser_finishes_cleanly(self):
        pipeline = self._pipeline(self.clean_scan)

        self.assertEqual(pipeline.status, "FINISHED")
        self.assertEqual(pipeline.launch_data["analyzer_degraded_reasons"], [])
        self.assertEqual(pipeline.launch_data["output_dir"], "/srv/aist/output/clean-scan")
        self.assertIsNotNone(pipeline.finished_at)

    def test_every_other_run_keeps_its_status_and_recorded_reasons(self):
        """Findings, another reason, a reconciliation failure, an unfinished run or a non-SAST run is out of scope."""
        for run_id, (status, reasons) in self.kept.items():
            with self.subTest(run_id=run_id):
                pipeline = self._pipeline(run_id)
                self.assertEqual(pipeline.status, status)
                self.assertEqual(pipeline.launch_data["analyzer_degraded_reasons"], reasons)
