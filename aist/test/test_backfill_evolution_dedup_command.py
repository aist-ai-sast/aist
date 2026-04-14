"""
Unit tests for backfill_evolution_dedup management command.

Covers:
  - _parse_blob_url        (pure function, no DB)
  - _fetch_file_lines      (mocked DB + filesystem/HTTP)
  - Command annotate phase (mocked DB)
  - Command match phase    (mocked run_evolution_dedup)
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.management.commands.backfill_evolution_dedup import (
    Command,
    _fetch_file_lines,
    _parse_blob_url,
)
from aist.models import VersionType

# ---------------------------------------------------------------------------
# _parse_blob_url
# ---------------------------------------------------------------------------


class ParseBlobUrlTests(SimpleTestCase):

    def test_full_url(self):
        url = "https://aist.example.com/api/projects_version/42/files/blob/src/views.py"
        self.assertEqual(_parse_blob_url(url), (42, "src/views.py"))

    def test_nested_path(self):
        url = "http://localhost/projects_version/7/files/blob/app/sub/dir/file.py"
        self.assertEqual(_parse_blob_url(url), (7, "app/sub/dir/file.py"))

    def test_query_string_stripped(self):
        url = "https://host/projects_version/3/files/blob/main.py?foo=bar"
        self.assertEqual(_parse_blob_url(url), (3, "main.py"))

    def test_unrecognized_url_returns_none(self):
        self.assertIsNone(_parse_blob_url("https://github.com/org/repo/blob/main/src/f.py"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_blob_url(""))


# ---------------------------------------------------------------------------
# _fetch_file_lines
# ---------------------------------------------------------------------------

class FetchFileLinesTests(SimpleTestCase):

    @patch("aist.management.commands.backfill_evolution_dedup.AISTProjectVersion")
    def test_returns_none_when_version_not_found(self, mock_pv_cls):
        mock_pv_cls.objects.select_related.return_value.get.side_effect = (
            mock_pv_cls.DoesNotExist
        )
        mock_pv_cls.DoesNotExist = Exception
        result = _fetch_file_lines(999, "src/a.py")
        self.assertIsNone(result)

    @patch("aist.management.commands.backfill_evolution_dedup.AISTProjectVersion")
    def test_file_hash_reads_from_archive(self, mock_pv_cls):
        pv = MagicMock()
        pv.version_type = VersionType.FILE_HASH
        root = MagicMock()
        pv.ensure_extracted.return_value = root

        # root / subpath .resolve() → file_path
        file_path = MagicMock()
        file_path.exists.return_value = True
        file_path.is_file.return_value = True
        file_path.read_text.return_value = "line1\nline2\nline3\n"
        root.__truediv__ = MagicMock(return_value=MagicMock(resolve=MagicMock(return_value=file_path)))

        mock_pv_cls.objects.select_related.return_value.get.return_value = pv
        mock_pv_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})

        result = _fetch_file_lines(1, "src/a.py")
        self.assertEqual(result, ["line1", "line2", "line3"])

    @patch("aist.management.commands.backfill_evolution_dedup.requests")
    @patch("aist.management.commands.backfill_evolution_dedup.AISTProjectVersion")
    def test_git_hash_fetches_via_scm_binding(self, mock_pv_cls, mock_requests):
        pv = MagicMock()
        pv.version_type = VersionType.GIT_HASH
        pv.version = "abc123"
        pv.last_resolved_commit = ""

        binding = MagicMock()
        binding.build_raw_url.return_value = "https://raw.example.com/file.py"
        binding.get_auth_headers.return_value = {"Authorization": "token xxx"}
        pv.project.repository.get_binding.return_value = binding

        mock_pv_cls.objects.select_related.return_value.get.return_value = pv
        mock_pv_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})

        resp = MagicMock()
        resp.status_code = 200
        resp.text = "def foo():\n    pass\n"
        mock_requests.get.return_value = resp

        result = _fetch_file_lines(2, "src/b.py")

        self.assertEqual(result, ["def foo():", "    pass"])
        mock_requests.get.assert_called_once_with(
            "https://raw.example.com/file.py",
            headers={"Authorization": "token xxx"},
            timeout=10,
            allow_redirects=True,
        )

    @patch("aist.management.commands.backfill_evolution_dedup.requests")
    @patch("aist.management.commands.backfill_evolution_dedup.AISTProjectVersion")
    def test_scm_http_404_returns_none(self, mock_pv_cls, mock_requests):
        pv = MagicMock()
        pv.version_type = VersionType.GIT_HASH
        pv.version = "abc"
        pv.last_resolved_commit = ""
        pv.project.repository.get_binding.return_value = MagicMock(
            build_raw_url=MagicMock(return_value="https://raw/f.py"),
            get_auth_headers=MagicMock(return_value={}),
        )
        mock_pv_cls.objects.select_related.return_value.get.return_value = pv
        mock_pv_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})

        resp = MagicMock()
        resp.status_code = 404
        mock_requests.get.return_value = resp

        self.assertIsNone(_fetch_file_lines(3, "src/c.py"))

    @patch("aist.management.commands.backfill_evolution_dedup.requests")
    @patch("aist.management.commands.backfill_evolution_dedup.AISTProjectVersion")
    def test_request_exception_returns_none(self, mock_pv_cls, mock_requests):
        pv = MagicMock()
        pv.version_type = VersionType.GIT_HASH
        pv.version = "abc"
        pv.last_resolved_commit = ""
        pv.project.repository.get_binding.return_value = MagicMock(
            build_raw_url=MagicMock(return_value="https://raw/f.py"),
            get_auth_headers=MagicMock(return_value={}),
        )
        mock_pv_cls.objects.select_related.return_value.get.return_value = pv
        mock_pv_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_requests.get.side_effect = mock_requests.RequestException("timeout")

        self.assertIsNone(_fetch_file_lines(4, "src/d.py"))


# ---------------------------------------------------------------------------
# Helpers for command tests
# ---------------------------------------------------------------------------

def _run_command(**kwargs):
    out = StringIO()
    cmd = Command(stdout=out, stderr=StringIO())
    options = {
        "dry_run": False,
        "annotate": False,
        "match": False,
        "pipeline_id": None,
        "product_id": None,
        "batch_size": 500,
        **kwargs,
    }
    cmd.handle(**options)
    return out.getvalue()


def _mock_pipelines(test_ids_per_pipeline: dict[str, list[int]]):
    """Return a list of mock AISTPipeline objects."""
    pipelines = []
    for pid, tids in test_ids_per_pipeline.items():
        p = MagicMock()
        p.id = pid
        p.tests.values_list.return_value = tids
        pipelines.append(p)
    return pipelines


# ---------------------------------------------------------------------------
# Command: annotate phase
# ---------------------------------------------------------------------------

class CommandAnnotatePhaseTests(SimpleTestCase):

    @patch("aist.management.commands.backfill_evolution_dedup._fetch_file_lines")
    @patch("aist.management.commands.backfill_evolution_dedup.DojoMeta")
    @patch("aist.management.commands.backfill_evolution_dedup.Finding")
    @patch.object(Command, "_pipelines_in_scope")
    def test_dry_run_does_not_write(self, mock_pipelines, mock_finding, mock_dojometa, mock_fetch):
        mock_pipelines.return_value = _mock_pipelines({"pipe-1": [10]})

        # Findings with no lhash
        mock_finding.objects.filter.return_value \
            .exclude.return_value \
            .only.return_value \
            .order_by.return_value \
            .values_list.return_value = [1]

        # sourcefile_link values
        mock_dojometa.objects.filter.return_value.values_list.side_effect = [
            [(1, "https://host/projects_version/5/files/blob/src/a.py")],  # link_by_finding
            [(1, 3)],   # line_by_finding
        ]

        mock_fetch.return_value = ["x", "y", "vuln_line"]

        _run_command(annotate=True, dry_run=True)

        mock_dojometa.objects.bulk_create.assert_not_called()

    @patch("aist.management.commands.backfill_evolution_dedup._fetch_file_lines")
    @patch("aist.management.commands.backfill_evolution_dedup.DojoMeta")
    @patch("aist.management.commands.backfill_evolution_dedup.Finding")
    @patch.object(Command, "_pipelines_in_scope")
    def test_apply_writes_dojometa(self, mock_pipelines, mock_finding, mock_dojometa, mock_fetch):
        mock_pipelines.return_value = _mock_pipelines({"pipe-1": [10]})

        mock_finding.objects.filter.return_value \
            .exclude.return_value \
            .only.return_value \
            .order_by.return_value \
            .values_list.return_value = [1]

        # line_by_finding: Finding.objects.filter(id__in=...).values_list("id", "line")
        mock_finding.objects.filter.return_value.values_list.return_value = [(1, 2)]

        # link_by_finding: DojoMeta.objects.filter(...).values_list("finding_id", "value")
        mock_dojometa.objects.filter.return_value.values_list.return_value = \
            [(1, "https://host/projects_version/5/files/blob/src/a.py")]

        mock_fetch.return_value = ["line1", "vulnerable_code()"]

        _run_command(annotate=True, dry_run=False)

        mock_dojometa.objects.bulk_create.assert_called_once()
        created = mock_dojometa.objects.bulk_create.call_args[0][0]
        self.assertEqual(len(created), 1)
        # DojoMeta is mocked — check the hash value passed to the constructor
        h = mock_dojometa.call_args.kwargs["value"]
        self.assertEqual(len(h), 16)
        int(h, 16)  # valid hex

    @patch("aist.management.commands.backfill_evolution_dedup._fetch_file_lines")
    @patch("aist.management.commands.backfill_evolution_dedup.DojoMeta")
    @patch("aist.management.commands.backfill_evolution_dedup.Finding")
    @patch.object(Command, "_pipelines_in_scope")
    def test_fetch_error_reported_in_output(
        self, mock_pipelines, mock_finding, mock_dojometa, mock_fetch,
    ):
        mock_pipelines.return_value = _mock_pipelines({"pipe-1": [10]})

        mock_finding.objects.filter.return_value \
            .exclude.return_value \
            .only.return_value \
            .order_by.return_value \
            .values_list.return_value = [1]

        # line_by_finding: Finding.objects.filter(id__in=...).values_list("id", "line")
        mock_finding.objects.filter.return_value.values_list.return_value = [(1, 1)]

        # link_by_finding: DojoMeta.objects.filter(...).values_list("finding_id", "value")
        mock_dojometa.objects.filter.return_value.values_list.return_value = \
            [(1, "https://host/projects_version/5/files/blob/src/a.py")]

        mock_fetch.return_value = None  # fetch failed

        output = _run_command(annotate=True)

        self.assertIn("skipped_fetch_error=1", output)
        mock_dojometa.objects.bulk_create.assert_not_called()

    @patch.object(Command, "_pipelines_in_scope")
    def test_no_tests_in_scope_reports_skip(self, mock_pipelines):
        mock_pipelines.return_value = []
        output = _run_command(annotate=True)
        self.assertIn("no tests in scope", output)


# ---------------------------------------------------------------------------
# Command: match phase
# ---------------------------------------------------------------------------

class CommandMatchPhaseTests(SimpleTestCase):

    @patch("aist.management.commands.backfill_evolution_dedup.run_evolution_dedup")
    @patch.object(Command, "_pipelines_in_scope")
    def test_dry_run_passes_dry_run_true(self, mock_pipelines, mock_run):
        mock_pipelines.return_value = _mock_pipelines({"p1": [1, 2]})
        mock_run.return_value = 0

        _run_command(match=True, dry_run=True)

        self.assertTrue(mock_run.call_args.kwargs["dry_run"])

    @patch("aist.management.commands.backfill_evolution_dedup.run_evolution_dedup")
    @patch.object(Command, "_pipelines_in_scope")
    def test_apply_passes_dry_run_false(self, mock_pipelines, mock_run):
        mock_pipelines.return_value = _mock_pipelines({"p1": [1]})
        mock_run.return_value = 0

        _run_command(match=True, dry_run=False)

        self.assertFalse(mock_run.call_args.kwargs["dry_run"])

    @patch("aist.management.commands.backfill_evolution_dedup.run_evolution_dedup")
    @patch.object(Command, "_pipelines_in_scope")
    def test_totals_summed_across_pipelines(self, mock_pipelines, mock_run):
        mock_pipelines.return_value = _mock_pipelines({"p1": [1], "p2": [2]})
        mock_run.side_effect = [4, 6]

        output = _run_command(match=True)

        self.assertIn("total_matched=10", output)

    @patch("aist.management.commands.backfill_evolution_dedup.run_evolution_dedup")
    @patch.object(Command, "_pipelines_in_scope")
    def test_pipeline_with_no_tests_skipped(self, mock_pipelines, mock_run):
        mock_pipelines.return_value = _mock_pipelines({"p1": []})

        _run_command(match=True)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Command: default (both phases)
# ---------------------------------------------------------------------------

class CommandDefaultBothPhasesTests(SimpleTestCase):

    @patch("aist.management.commands.backfill_evolution_dedup.run_evolution_dedup")
    @patch.object(Command, "_pipelines_in_scope")
    def test_both_phases_run_when_no_flags(self, mock_pipelines, mock_run):
        mock_pipelines.return_value = _mock_pipelines({"p1": []})
        mock_run.return_value = 0

        output = _run_command()  # neither --annotate nor --match

        self.assertIn("annotate:", output)
        self.assertIn("match:", output)
