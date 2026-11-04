from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.tasks.enrich import enrich_finding_task


def _finding() -> SimpleNamespace:
    return SimpleNamespace(id=4101, file_path="web/static/vendor/editor/editor.min.js", test_id=77, save=MagicMock())


class EnrichFindingTaskLoggingTests(SimpleTestCase):

    @patch("aist.tasks.enrich.Finding")
    @patch("aist.tasks.enrich.LinkBuilder")
    def test_logs_context_when_link_builder_build_fails(self, mock_link_builder, mock_finding):
        mock_finding.objects.get.return_value = _finding()
        mock_link_builder.return_value = MagicMock(version_id=12, build=MagicMock(side_effect=RuntimeError("boom")))

        with self.assertLogs("aist.tasks.enrich", level="ERROR") as captured:
            result = enrich_finding_task(finding_id=4101, trim_path="", project_version_id=12)

        self.assertEqual(result, 0)
        output = "\n".join(captured.output)
        self.assertIn("Failed to build source link for finding enrichment", output)
        self.assertIn("finding_id=4101", output)
        self.assertIn("test_id=77", output)

    @patch("aist.tasks.enrich.store_source_link", side_effect=RuntimeError("db failure"))
    @patch("aist.tasks.enrich.Finding")
    @patch("aist.tasks.enrich.LinkBuilder")
    def test_logs_context_when_the_source_link_cannot_be_stored(self, mock_link_builder, mock_finding, mock_store):
        mock_finding.objects.get.return_value = _finding()
        mock_link_builder.return_value = MagicMock(version_id=12, build=MagicMock(return_value="https://aist.example/link"))

        with self.assertLogs("aist.tasks.enrich", level="ERROR") as captured:
            result = enrich_finding_task(finding_id=4101, trim_path="", project_version_id=12)

        self.assertEqual(result, 0)
        output = "\n".join(captured.output)
        self.assertIn("Failed to upsert sourcefile_link meta for finding enrichment", output)
        self.assertIn("finding_id=4101", output)
        self.assertIn("test_id=77", output)
