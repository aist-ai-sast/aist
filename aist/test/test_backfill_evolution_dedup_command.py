"""
Tests for the backfill_evolution_dedup management command.

Covers:
  - LinkBuilder.parse      source links the platform built
  - _fetch_file_lines      the version's archive or repository, through the shared file reader
  - annotate phase         lhash written for historical findings, equal to the import-time value
  - match phase            (mocked run_evolution_dedup)
"""
from __future__ import annotations

import base64
from hashlib import sha256
from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from dojo.models import DojoMeta, Finding

from aist.dedupe.evolution import AIST_LHASH_META_NAME
from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder, store_source_link
from aist.management.commands.backfill_evolution_dedup import Command, _fetch_file_lines
from aist.models import AISTPipeline, AISTProjectVersion, RepositoryInfo, ScmType
from aist.parser_overrides import SEMGREP_SCAN_TYPE
from aist.tasks.enrich import annotate_line_hash_batch
from aist.test.pipeline_execution_helpers import make_test_with_finding
from aist.test.source_version_fixtures import COMMIT, OUTSIDE_CONTENT, FakeScmHost, SourceVersionMixin
from aist.test.test_api import AISTApiBase

SOURCE = "def index():\n    return render(request)\n"
SOURCE_FILES = {"app/views.py": SOURCE, "README.md": "# widgets\n"}


# ---------------------------------------------------------------------------
# LinkBuilder.parse
# ---------------------------------------------------------------------------


class SourceLinkParseTests(SimpleTestCase):

    @override_settings(PUBLIC_BASE_URL="https://old-installation.example")
    def test_source_link_is_independent_of_the_deployment_origin(self):
        self.assertEqual(
            LinkBuilder(42).build("src/sub dir/naïve.py"),
            "/api/v2/aist/projects_version/42/files/blob/src/sub%20dir/na%C3%AFve.py",
        )

    def test_source_link_built_for_a_finding(self):
        self.assertEqual(LinkBuilder.parse(LinkBuilder(42).build("src/views.py")), (42, "src/views.py"))

    def test_nested_path_with_characters_the_link_encodes(self):
        link = LinkBuilder(7).build("app/sub dir/naïve.py")
        self.assertEqual(LinkBuilder.parse(link), (7, "app/sub dir/naïve.py"))

    def test_query_string_ignored(self):
        link = LinkBuilder.build_project_version_file_blob(3, "main.py") + "?download=1"
        self.assertEqual(LinkBuilder.parse(link), (3, "main.py"))

    def test_other_platform_link_returns_none(self):
        link = "https://aist.example.com" + reverse("aist_api:test_detail", kwargs={"test_id": 3})
        self.assertIsNone(LinkBuilder.parse(link))

    def test_code_host_link_returns_none(self):
        self.assertIsNone(LinkBuilder.parse("https://github.com/org/repo/blob/main/src/f.py"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(LinkBuilder.parse(""))


# ---------------------------------------------------------------------------
# _fetch_file_lines
# ---------------------------------------------------------------------------


class _SourceVersionsBase(SourceVersionMixin, AISTApiBase):
    def _github_version(self) -> AISTProjectVersion:
        return self.repository_version(RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="widgets"))

    def _gerrit_version(self) -> AISTProjectVersion:
        repository, _binding = self.gerrit_repository()
        return self.repository_version(repository)


class FetchFileLinesTests(_SourceVersionsBase):

    def test_returns_none_when_version_not_found(self):
        self.assertIsNone(_fetch_file_lines(999_999, "src/a.py"))

    def test_archive_version_reads_the_extracted_file(self):
        pv = self.archive_version(SOURCE_FILES)

        self.assertEqual(_fetch_file_lines(pv.id, "app/views.py"), ["def index():", "    return render(request)"])

    def test_archive_path_leaving_the_extraction_is_not_read(self):
        pv = self.archive_version(SOURCE_FILES)
        root = pv.ensure_extracted()
        outside = root.parent / "secret.py"
        outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")
        (root / "app" / "linked.py").symlink_to(outside)

        for subpath in ("../secret.py", "app/../../secret.py", str(outside), "app/linked.py"):
            with self.subTest(subpath=subpath):
                self.assertIsNone(_fetch_file_lines(pv.id, subpath))

    def test_repository_path_leaving_the_repository_is_never_requested(self):
        pv = self._github_version()

        with FakeScmHost() as host:
            self.assertIsNone(_fetch_file_lines(pv.id, "../../acme/payroll/main/.env"))
        self.assertEqual(host.requests, [])

    def test_public_github_repository_is_read_from_its_raw_url(self):
        pv = self._github_version()

        with FakeScmHost(body=SOURCE.encode()) as host:
            lines = _fetch_file_lines(pv.id, "app/views.py")

        self.assertEqual(lines, ["def index():", "    return render(request)"])
        self.assertEqual(host.last.url, f"https://raw.githubusercontent.com/acme/widgets/{COMMIT}/app/views.py")

    def test_gerrit_content_is_base64_decoded(self):
        pv = self._gerrit_version()

        with FakeScmHost(body=base64.b64encode(SOURCE.encode())):
            self.assertEqual(_fetch_file_lines(pv.id, "app/views.py"), ["def index():", "    return render(request)"])

    def test_unreadable_repository_file_returns_none(self):
        pv = self._github_version()
        outcomes = {
            "missing": FakeScmHost(body=b"Not Found", status_code=404),
            "host failure": FakeScmHost(body=b"oops", status_code=503),
            "connection error": FakeScmHost(error=requests.ConnectionError("unreachable")),
        }
        for label, host in outcomes.items():
            with self.subTest(label), host:
                self.assertIsNone(_fetch_file_lines(pv.id, "app/views.py"))


# ---------------------------------------------------------------------------
# Command: annotate phase on stored findings
# ---------------------------------------------------------------------------


class AnnotatePhaseTests(_SourceVersionsBase):

    """A backfilled lhash equals the value the import-time enrichment writes for the same line."""

    EXPECTED_LHASH = sha256(b"return render(request)").hexdigest()[:16]

    def _finding_with_source_link(self, pv: AISTProjectVersion) -> Finding:
        test, finding = make_test_with_finding(
            self.product, self.user, file_path="app/views.py", line=2, test_type_name=SEMGREP_SCAN_TYPE,
        )
        pipeline = AISTPipeline.objects.create(id=f"pipe-backfill-{pv.id}", project=self.project, project_version=pv)
        pipeline.tests.add(test)
        store_source_link(finding.id, LinkBuilder(pv.id).build(finding.file_path))
        return finding

    @staticmethod
    def _annotate(pv: AISTProjectVersion, *, dry_run: bool = False) -> str:
        return _run_command(annotate=True, dry_run=dry_run, pipeline_id=f"pipe-backfill-{pv.id}")

    def _stored_lhash(self, finding: Finding) -> str | None:
        meta = DojoMeta.objects.filter(finding=finding, name=AIST_LHASH_META_NAME).first()
        return meta.value if meta else None

    def test_archive_backfill_matches_import_time_fingerprint(self):
        pv = self.archive_version(SOURCE_FILES)
        finding = self._finding_with_source_link(pv)
        annotate_line_hash_batch([finding.id], str(pv.ensure_extracted()))
        import_time = self._stored_lhash(finding)
        DojoMeta.objects.filter(finding=finding, name=AIST_LHASH_META_NAME).delete()

        output = self._annotate(pv)

        self.assertIn("annotated=1", output)
        self.assertEqual(import_time, self.EXPECTED_LHASH)
        self.assertEqual(self._stored_lhash(finding), self.EXPECTED_LHASH)

    def test_gerrit_backfill_hashes_the_decoded_source(self):
        pv = self._gerrit_version()
        finding = self._finding_with_source_link(pv)

        with FakeScmHost(body=base64.b64encode(SOURCE.encode())):
            output = self._annotate(pv)

        self.assertIn("annotated=1", output)
        self.assertEqual(self._stored_lhash(finding), self.EXPECTED_LHASH)

    def test_dry_run_counts_the_finding_but_writes_nothing(self):
        pv = self.archive_version(SOURCE_FILES)
        finding = self._finding_with_source_link(pv)

        output = self._annotate(pv, dry_run=True)

        self.assertIn("annotated=1", output)
        self.assertIsNone(self._stored_lhash(finding))

    def test_unreachable_source_is_reported_and_leaves_the_finding_unannotated(self):
        pv = self._github_version()
        finding = self._finding_with_source_link(pv)

        with FakeScmHost(body=b"oops", status_code=503):
            output = self._annotate(pv)

        self.assertIn("skipped_fetch_error=1", output)
        self.assertIsNone(self._stored_lhash(finding))
        self.assertTrue(DojoMeta.objects.filter(finding=finding, name=SOURCEFILE_LINK_META_NAME).exists())


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
