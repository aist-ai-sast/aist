from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.tasks.enrich import enrich_finding_task


class EnrichFindingTaskLoggingTests(SimpleTestCase):
    @patch("aist.tasks.enrich.Finding")
    @patch("aist.tasks.enrich.LinkBuilder")
    def test_logs_context_when_link_builder_build_fails(self, mock_link_builder, mock_finding):
        finding = SimpleNamespace(
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            test_id=189,
            save=MagicMock(),
            delete=MagicMock(),
        )
        mock_finding.objects.select_related.return_value.get.return_value = finding

        linker = MagicMock()
        linker.build.side_effect = RuntimeError("boom")
        mock_link_builder.return_value = linker

        with self.assertLogs("aist.tasks.enrich", level="ERROR") as captured:
            result = enrich_finding_task(
                finding_id=215722,
                trim_path="",
                project_version_descriptor={"project_version": "ef52d134dfaab331e0c107742564b0a6d92b0688"},
            )

        self.assertEqual(result, 0)
        output = "\n".join(captured.output)
        self.assertIn("Failed to build source link for finding enrichment", output)
        self.assertIn("finding_id=215722", output)
        self.assertIn("test_id=189", output)

    @patch("aist.tasks.enrich.DojoMeta")
    @patch("aist.tasks.enrich.Finding")
    @patch("aist.tasks.enrich.LinkBuilder")
    def test_logs_context_when_dojometa_upsert_fails(
        self, mock_link_builder, mock_finding, mock_dojometa,
    ):
        finding = SimpleNamespace(
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            test_id=189,
            save=MagicMock(),
            delete=MagicMock(),
        )
        mock_finding.objects.select_related.return_value.get.return_value = finding

        linker = MagicMock()
        linker.build.return_value = "https://example/link"
        linker.contains_excluded_path.return_value = False
        mock_link_builder.return_value = linker
        mock_dojometa.objects.update_or_create.side_effect = RuntimeError("db failure")

        with self.assertLogs("aist.tasks.enrich", level="ERROR") as captured:
            result = enrich_finding_task(
                finding_id=215722,
                trim_path="",
                project_version_descriptor={"project_version": "ef52d134dfaab331e0c107742564b0a6d92b0688"},
            )

        self.assertEqual(result, 0)
        output = "\n".join(captured.output)
        self.assertIn("Failed to upsert sourcefile_link meta for finding enrichment", output)
        self.assertIn("finding_id=215722", output)
        self.assertIn("test_id=189", output)

    @patch("aist.tasks.enrich.DojoMeta")
    @patch("aist.tasks.enrich.Finding")
    @patch("aist.tasks.enrich.LinkBuilder")
    def test_sourcefile_link_uses_defaults_not_lookup(self, mock_link_builder, mock_finding, mock_dojometa):
        """update_or_create must pass value in defaults, not as a lookup field."""
        finding = SimpleNamespace(
            file_path="src/app.py",
            test_id=42,
            save=MagicMock(),
            delete=MagicMock(),
        )
        mock_finding.objects.select_related.return_value.get.return_value = finding

        linker = MagicMock()
        linker.build.return_value = "https://example.com/src/app.py"
        linker.contains_excluded_path.return_value = False
        mock_link_builder.return_value = linker
        mock_dojometa.objects.update_or_create.return_value = (MagicMock(), True)

        result = enrich_finding_task(
            finding_id=42,
            trim_path="",
            project_version_descriptor={},
        )

        self.assertEqual(result, 1)
        mock_dojometa.objects.update_or_create.assert_called_once()
        call_kwargs = mock_dojometa.objects.update_or_create.call_args.kwargs
        # value must be in defaults, NOT as a top-level lookup keyword
        self.assertIn("defaults", call_kwargs)
        self.assertEqual(call_kwargs["defaults"]["value"], "https://example.com/src/app.py")
        self.assertNotIn("value", {k: v for k, v in call_kwargs.items() if k != "defaults"})
