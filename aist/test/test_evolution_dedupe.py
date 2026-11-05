"""
Tests for evolution deduplication.

Covers two units independently (no shared state):
  1. annotate_line_hash_batch — reads source files, writes DojoMeta aist:lhash
  2. run_evolution_dedup     — matches findings across pipelines by lhash

No database is required: the ORM is stubbed. annotate_line_hash_batch reads real files under a temporary
directory.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.dedupe.evolution import (
    AIST_EVOLUTION_TAG,
    AIST_LHASH_META_NAME,
    _normalize_vuln_id,
    run_evolution_dedup,
)
from aist.tasks.enrich import annotate_line_hash_batch

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


@dataclass
class _Meta:
    name: str
    value: str


class _MetaManager:

    """Mimics a Django RelatedManager so .all() works on the stub."""

    def __init__(self, items: list[_Meta]) -> None:
        self._items = items

    def all(self):
        return iter(self._items)


@dataclass
class _Engagement:
    product_id: int = 1


@dataclass
class _Test:
    engagement: _Engagement = field(default_factory=_Engagement)


@dataclass
class _Finding:
    id: int
    file_path: str
    line: int | None
    vuln_id_from_tool: str = "python/sql-injection"
    duplicate: bool = False
    test: _Test = field(default_factory=_Test)
    finding_meta: _MetaManager = field(default_factory=lambda: _MetaManager([]))
    tags: MagicMock = field(default_factory=MagicMock)
    created: datetime = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _normalize_vuln_id
# ---------------------------------------------------------------------------

class NormalizeVulnIdTests(SimpleTestCase):

    def test_dots_replaced(self):
        self.assertEqual(
            _normalize_vuln_id("javascript.browser.security.xss"),
            "javascript_browser_security_xss",
        )

    def test_hyphens_replaced(self):
        self.assertEqual(
            _normalize_vuln_id("insufficient-postmessage-origin-validation"),
            "insufficient_postmessage_origin_validation",
        )

    def test_mixed_separators(self):
        # Real Semgrep rule that changed format between versions
        old = "javascript.browser.security.insufficient-postmessage-origin-validation.insufficient-postmessage-origin-validation"
        new = "javascript_browser_security_insufficient_postmessage_origin_validation_insufficient_postmessage_origin_validation"
        self.assertEqual(_normalize_vuln_id(old), _normalize_vuln_id(new))

    def test_empty_string(self):
        self.assertEqual(_normalize_vuln_id(""), "")

    def test_already_normalized(self):
        s = "python_sqli_injection"
        self.assertEqual(_normalize_vuln_id(s), s)

    def test_slash_rule_id_matches_underscored_format(self):
        """Snyk Code switched from "python/XSS" to "python_xss"; both must normalize equally."""
        self.assertEqual(_normalize_vuln_id("python/XSS"), _normalize_vuln_id("python_xss"))


# ---------------------------------------------------------------------------
# annotate_line_hash_batch
# ---------------------------------------------------------------------------

class AnnotateLineHashBatchTests(SimpleTestCase):

    """The task reads real files under a temporary checkout; only the ORM is stubbed."""

    def setUp(self):
        super().setUp()
        self.source_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.source_root, ignore_errors=True)
        self.outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def _write(self, root: Path, relative: str, content: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _run(self, findings, *, trim_path=""):
        """Run the task over ``findings``; return the (finding_id, value) pairs it stored."""
        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter(findings)
        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta", side_effect=SimpleNamespace) as mock_dojometa,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs
            result = annotate_line_hash_batch([f.id for f in findings], str(self.source_root), trim_path)
        created = mock_dojometa.objects.bulk_create.call_args[0][0] if mock_dojometa.objects.bulk_create.called else []
        self.assertEqual(result, len(created))
        for entry in created:
            self.assertEqual(entry.name, AIST_LHASH_META_NAME)
        return [(entry.finding_id, entry.value) for entry in created]

    @staticmethod
    def _expected(line: str) -> str:
        return hashlib.sha256(line.strip().encode()).hexdigest()[:16]

    def test_empty_source_root_returns_zero(self):
        result = annotate_line_hash_batch([1, 2], "")
        self.assertEqual(result, 0)

    def test_each_finding_gets_the_fingerprint_of_its_line(self):
        self._write(self.source_root, "app/views.py", "def login():\n    run(query)\n")
        stored = self._run([
            _Finding(id=1, file_path="app/views.py", line=1),
            _Finding(id=2, file_path="app/views.py", line=2),
        ])
        self.assertEqual(stored, [(1, self._expected("def login():")), (2, self._expected("run(query)"))])

    def test_reads_file_once_per_unique_path(self):
        self._write(self.source_root, "app/views.py", "def login():\n    pass\n")
        with patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read_text:
            self._run([
                _Finding(id=1, file_path="app/views.py", line=1),
                _Finding(id=2, file_path="app/views.py", line=2),
            ])
        self.assertEqual(read_text.call_count, 1)

    def test_skips_finding_without_line(self):
        self._write(self.source_root, "src/a.py", "code()\n")
        self.assertEqual(self._run([_Finding(id=10, file_path="src/a.py", line=None)]), [])

    def test_skips_blank_line_content(self):
        self._write(self.source_root, "src/a.py", "line1\n   \nline3\n")
        self.assertEqual(self._run([_Finding(id=11, file_path="src/a.py", line=2)]), [])

    def test_a_missing_file_is_skipped(self):
        self.assertEqual(self._run([_Finding(id=12, file_path="missing/file.py", line=1)]), [])

    def test_an_absolute_path_inside_the_source_root_is_read(self):
        path = self._write(self.source_root, "src/b.py", "vulnerable_line\n")
        self.assertEqual(self._run([_Finding(id=20, file_path=str(path), line=1)]), [(20, self._expected("vulnerable_line"))])

    def test_an_absolute_container_path_under_the_checkout_root_is_read_from_the_source_root(self):
        self._write(self.source_root, "src/b.py", "vulnerable_line\n")
        stored = self._run(
            [_Finding(id=21, file_path="/workspace/shop/src/b.py", line=1)], trim_path="/workspace/shop",
        )
        self.assertEqual(stored, [(21, self._expected("vulnerable_line"))])

    def test_a_path_outside_the_source_root_is_not_read(self):
        outside = self._write(self.outside, "secret.txt", "worker secret\n")
        cases = {
            "absolute path": str(outside),
            "file URI": outside.as_uri(),
            "traversal": f"../{self.outside.name}/secret.txt",
            "encoded traversal": f"file:///workspace/shop/%2e%2e/{self.outside.name}/secret.txt",
        }
        for label, file_path in cases.items():
            with self.subTest(label):
                self.assertEqual(self._run([_Finding(id=30, file_path=file_path, line=1)], trim_path="/workspace/shop"), [])

    def test_a_symlink_out_of_the_source_root_is_not_followed(self):
        outside = self._write(self.outside, "secret.txt", "worker secret\n")
        (self.source_root / "src").mkdir()
        (self.source_root / "src" / "link.py").symlink_to(outside)
        self.assertEqual(self._run([_Finding(id=31, file_path="src/link.py", line=1)]), [])


# ---------------------------------------------------------------------------
# run_evolution_dedup
# ---------------------------------------------------------------------------

class RunEvolutionDedupTests(SimpleTestCase):

    def _make_finding(self, fid, lhash=None, file_path="src/a.py",
                      vuln_id="python/sqli", product_id=1, test_id=99,
                      scan_type="Semgrep JSON Report"):
        metas = [_Meta(name=AIST_LHASH_META_NAME, value=lhash)] if lhash else []
        return _Finding(
            id=fid,
            file_path=file_path,
            line=10,
            vuln_id_from_tool=vuln_id,
            test=SimpleNamespace(
                id=test_id,
                engagement=SimpleNamespace(product_id=product_id),
                test_type=SimpleNamespace(name=scan_type),
            ),
            finding_meta=_MetaManager(metas),
        )

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_no_findings_returns_zero(self, mock_finding, mock_dojometa):
        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = []

        result = run_evolution_dedup(
            pipeline_id="pipe-1",
            test_ids=[1],
            logger=MagicMock(),
        )
        self.assertEqual(result, 0)
        mock_dojometa.objects.filter.assert_not_called()

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_finding_without_lhash_not_matched(self, mock_finding, mock_dojometa):
        f = self._make_finding(1, lhash=None)  # no lhash meta
        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [f]
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = []

        result = run_evolution_dedup(
            pipeline_id="pipe-1",
            test_ids=[99],
            logger=MagicMock(),
        )
        self.assertEqual(result, 0)

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_match_marks_duplicate_and_adds_tag(self, mock_finding, mock_dojometa):
        new_f = self._make_finding(2, lhash="abc123def456abcd", test_id=200)

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        # Ancestor in another test (test_id=100) with same lhash
        ancestor_f = self._make_finding(1, lhash="abc123def456abcd", test_id=100)
        ancestor_meta = SimpleNamespace(
            finding=ancestor_f,
            value="abc123def456abcd",
        )
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = [ancestor_meta]

        set_dup_mock = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", set_dup_mock):
            result = run_evolution_dedup(
                pipeline_id="pipe-2",
                test_ids=[200],
                logger=MagicMock(),
            )

        self.assertEqual(result, 1)
        set_dup_mock.assert_called_once_with(new_f, ancestor_f)
        new_f.tags.add.assert_called_once_with(AIST_EVOLUTION_TAG)

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_different_rule_not_matched(self, mock_finding, mock_dojometa):
        """Same lhash but different vuln_id_from_tool → no match."""
        new_f = self._make_finding(
            2, lhash="abc123def456abcd", vuln_id="python/sqli", test_id=200,
        )

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        # Ancestor has different rule
        ancestor_f = self._make_finding(
            1, lhash="abc123def456abcd", vuln_id="python/xss", test_id=100,
        )
        ancestor_meta = SimpleNamespace(finding=ancestor_f, value="abc123def456abcd")
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = [ancestor_meta]

        set_dup_mock = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", set_dup_mock):
            result = run_evolution_dedup(
                pipeline_id="pipe-3",
                test_ids=[200],
                logger=MagicMock(),
            )

        self.assertEqual(result, 0)
        set_dup_mock.assert_not_called()

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_set_duplicate_exception_is_swallowed(self, mock_finding, mock_dojometa):
        """Exception in set_duplicate must not abort processing of remaining findings."""
        new_f = self._make_finding(2, lhash="abc123def456abcd", test_id=200)

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        ancestor_f = self._make_finding(1, lhash="abc123def456abcd", test_id=100)
        ancestor_meta = SimpleNamespace(finding=ancestor_f, value="abc123def456abcd")
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = [ancestor_meta]

        mock_logger = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", side_effect=RuntimeError("db")):
            result = run_evolution_dedup(
                pipeline_id="pipe-4",
                test_ids=[200],
                logger=mock_logger,
            )

        self.assertEqual(result, 0)
        mock_logger.exception.assert_called_once()

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_oldest_ancestor_wins(self, mock_finding, mock_dojometa):
        """When multiple ancestors match, the first in (created, id) order is used."""
        new_f = self._make_finding(3, lhash="deadbeef12345678", test_id=300)

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        # Two ancestors — returned in (created, id) order; first one should win.
        oldest = self._make_finding(1, lhash="deadbeef12345678", test_id=100)
        newer = self._make_finding(2, lhash="deadbeef12345678", test_id=200)
        metas = [
            SimpleNamespace(finding=oldest, value="deadbeef12345678"),
            SimpleNamespace(finding=newer, value="deadbeef12345678"),
        ]
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = metas

        set_dup_mock = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", set_dup_mock):
            result = run_evolution_dedup(
                pipeline_id="pipe-5",
                test_ids=[300],
                logger=MagicMock(),
            )

        self.assertEqual(result, 1)
        set_dup_mock.assert_called_once_with(new_f, oldest)

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_vuln_id_format_change_still_matches(self, mock_finding, mock_dojometa):
        """Ancestor has dots/hyphens in vuln_id; new finding uses underscores — must match."""
        new_f = self._make_finding(
            2, lhash="abc123def456abcd",
            vuln_id="javascript_browser_security_xss_xss",
            test_id=200,
        )
        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        ancestor_f = self._make_finding(
            1, lhash="abc123def456abcd",
            vuln_id="javascript.browser.security.xss.xss",
            test_id=100,
        )
        ancestor_meta = SimpleNamespace(finding=ancestor_f, value="abc123def456abcd")
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = [ancestor_meta]

        set_dup_mock = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", set_dup_mock):
            result = run_evolution_dedup(
                pipeline_id="pipe-8",
                test_ids=[200],
                logger=MagicMock(),
            )

        self.assertEqual(result, 1)
        set_dup_mock.assert_called_once_with(new_f, ancestor_f)

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_different_scan_type_not_matched(self, mock_finding, mock_dojometa):
        """Same lhash + vuln_id but different scan type → no match."""
        new_f = self._make_finding(
            2, lhash="abc123def456abcd", scan_type="Semgrep JSON Report", test_id=200,
        )

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        ancestor_f = self._make_finding(
            1, lhash="abc123def456abcd", scan_type="Bandit", test_id=100,
        )
        ancestor_meta = SimpleNamespace(finding=ancestor_f, value="abc123def456abcd")
        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = [ancestor_meta]

        set_dup_mock = MagicMock()
        with patch("aist.dedupe.evolution.set_duplicate", set_dup_mock):
            result = run_evolution_dedup(
                pipeline_id="pipe-6",
                test_ids=[200],
                logger=MagicMock(),
            )

        self.assertEqual(result, 0)
        set_dup_mock.assert_not_called()

    @patch("aist.dedupe.evolution.DojoMeta")
    @patch("aist.dedupe.evolution.Finding")
    def test_mitigated_excluded_fp_oos_ra_included_in_ancestor_query(self, mock_finding, mock_dojometa):
        """
        Mitigated ancestors must be excluded (new occurrence = regression).
        FP / OOS / RA ancestors must stay eligible even though closing a
        finding as FP also sets is_mitigated: a finding already reviewed and
        dismissed should suppress the same code without re-triage.
        """
        new_f = self._make_finding(2, lhash="abc123def456abcd", test_id=200)

        mock_finding.objects.filter.return_value \
            .prefetch_related.return_value \
            .select_related.return_value = [new_f]

        mock_dojometa.objects.filter.return_value \
            .exclude.return_value \
            .select_related.return_value \
            .order_by.return_value = []

        run_evolution_dedup(pipeline_id="pipe-7", test_ids=[200], logger=MagicMock())

        call = mock_dojometa.objects.filter.call_args
        # A bare is_mitigated=False kwarg would drop every FP-closed ancestor.
        self.assertNotIn("finding__is_mitigated", call.kwargs)
        (status_q,) = call.args
        self.assertEqual(status_q.connector, "OR")
        self.assertCountEqual(
            status_q.children,
            [
                ("finding__is_mitigated", False),
                ("finding__false_p", True),
                ("finding__out_of_scope", True),
                ("finding__risk_accepted", True),
            ],
        )
