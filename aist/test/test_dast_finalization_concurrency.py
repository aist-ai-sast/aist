"""Exercise the import lock order using independent PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from aist.models import AISTPipeline, AISTStatus, PipelineExecutionType
from aist.services.dast_finalization import finalize_dast_report
from aist.test import test_dast_finalization as fixtures


class DastFinalizationConcurrencyTests(TransactionTestCase):
    setUp = fixtures.DastFinalizationTests.setUp
    _sectioned_report = fixtures.DastFinalizationTests._sectioned_report

    def _finalize_pair(self, runs, report):
        barrier = Barrier(2)

        def finalize(run):
            pipeline, binding = run
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    return finalize_dast_report(
                        pipeline_id=pipeline.pk, report=report, binding=binding, logger=fixtures.LOGGER, lead=self.lead,
                    )
            finally:
                close_old_connections()

        with patch("aist.services.dast_finalization.finish_or_schedule_pipeline_results"), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(finalize, run) for run in runs]
            return [future.result(timeout=20) for future in futures]

    def test_parallel_runs_launched_from_opposite_projects_finish_without_deadlock(self):
        destination, report = self._sectioned_report()
        other = AISTPipeline.objects.create(
            project=destination.project, dast_binding=destination,
            execution_type=PipelineExecutionType.MANUAL_IMPORT, status=AISTStatus.ADMITTED,
        )
        results = self._finalize_pair([(self.remote, self.binding), (other, destination)], report)
        self.assertTrue(all(len(result.sections) == 2 for result in results))
        self.assertEqual(self.remote.tests.count(), 2)
        self.assertEqual(other.tests.count(), 2)

    def test_parallel_delivery_of_one_report_imports_each_section_once(self):
        _destination, report = self._sectioned_report()
        results = self._finalize_pair([(self.remote, self.binding)] * 2, report)
        self.assertEqual(sorted(result.already_finalized for result in results), [False, True])
        self.assertEqual(results[0].sections, results[1].sections)
        self.assertEqual(self.remote.tests.count(), 2)
