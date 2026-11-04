"""Every analyzer the packaged catalog can launch writes a report the platform has a parser to import."""

from django.test import SimpleTestCase
from dojo.tools.factory import PARSERS

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.config_utils import AnalyzersConfigHelper  # noqa: E402


class AnalyzerScanTypeTests(SimpleTestCase):

    def test_each_launchable_analyzer_report_names_a_registered_parser(self):
        catalog = AnalyzersConfigHelper()
        launchable = set(catalog.get_supported_analyzers())
        reports = [report for report in catalog.get_analyzer_reports() if report.analyzer_name in launchable]

        self.assertTrue(reports)
        for report in reports:
            with self.subTest(analyzer=report.analyzer_name):
                self.assertIn(report.scan_type, PARSERS)
