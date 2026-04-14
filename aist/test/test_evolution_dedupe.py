"""
Tests for evolution deduplication.

Covers two units independently (no shared state):
  1. annotate_line_hash_batch — reads source files, writes DojoMeta aist:lhash
  2. run_evolution_dedup     — matches findings across pipelines by lhash

All tests use mocks/stubs; no database or filesystem is required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# annotate_line_hash_batch
# ---------------------------------------------------------------------------

class AnnotateLineHashBatchTests(SimpleTestCase):

    def _run(self, finding_ids, source_root, findings, *, expected_creates):
        """
        Helper: patches Finding.objects and DojoMeta.objects, runs the task,
        asserts bulk_create received exactly expected_creates DojoMeta entries.
        """
        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter(findings)

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta") as mock_dojometa,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs
            result = annotate_line_hash_batch(finding_ids, source_root)

        if expected_creates:
            mock_dojometa.objects.bulk_create.assert_called_once()
            created = mock_dojometa.objects.bulk_create.call_args[0][0]
            self.assertEqual(len(created), len(expected_creates))
            for entry, (exp_finding_id, exp_hash) in zip(created, expected_creates, strict=False):
                self.assertEqual(entry.finding_id, exp_finding_id)
                self.assertEqual(entry.value, exp_hash)
        else:
            mock_dojometa.objects.bulk_create.assert_not_called()

        return result

    def test_empty_source_root_returns_zero(self):
        result = annotate_line_hash_batch([1, 2], "")
        self.assertEqual(result, 0)

    def test_reads_file_once_per_unique_path(self):
        """Two findings in the same file → file opened once."""
        src = "/src"
        f1 = _Finding(id=1, file_path="app/views.py", line=1)
        f2 = _Finding(id=2, file_path="app/views.py", line=2)

        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter([f1, f2])

        file_content = "def login():\n    pass\n"

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta"),
            patch("aist.tasks.enrich.Path") as mock_path_cls,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs

            mock_path_instance = MagicMock()
            mock_path_instance.is_absolute.return_value = False
            mock_path_cls.return_value = mock_path_instance

            joined = MagicMock()
            joined.read_text.return_value = file_content
            mock_path_instance.__truediv__ = MagicMock(return_value=joined)

            annotate_line_hash_batch([1, 2], src)

        # read_text called once despite two findings in the file
        joined.read_text.assert_called_once()

    def test_skips_finding_without_line(self):
        f = _Finding(id=10, file_path="src/a.py", line=None)
        result = self._run([10], "/src", [f], expected_creates=None)
        self.assertEqual(result, 0)

    def test_skips_blank_line_content(self):
        f = _Finding(id=11, file_path="src/a.py", line=2)

        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter([f])

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta") as mock_dojometa,
            patch("aist.tasks.enrich.Path") as mock_path_cls,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs
            p = MagicMock()
            p.is_absolute.return_value = False
            mock_path_cls.return_value = p
            joined = MagicMock()
            joined.read_text.return_value = "line1\n   \nline3\n"  # line 2 is blank
            p.__truediv__ = MagicMock(return_value=joined)

            annotate_line_hash_batch([11], "/src")

        mock_dojometa.objects.bulk_create.assert_not_called()

    def test_skips_on_oserror(self):
        f = _Finding(id=12, file_path="missing/file.py", line=1)

        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter([f])

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta") as mock_dojometa,
            patch("aist.tasks.enrich.Path") as mock_path_cls,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs
            p = MagicMock()
            p.is_absolute.return_value = False
            mock_path_cls.return_value = p
            joined = MagicMock()
            joined.read_text.side_effect = OSError("not found")
            p.__truediv__ = MagicMock(return_value=joined)

            result = annotate_line_hash_batch([12], "/src")

        self.assertEqual(result, 0)
        mock_dojometa.objects.bulk_create.assert_not_called()

    def test_absolute_file_path_used_directly(self):
        """If file_path is absolute, Path(file_path) is used as-is."""
        f = _Finding(id=20, file_path="/abs/path/file.py", line=1)

        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter([f])

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta"),
            patch("aist.tasks.enrich.Path") as mock_path_cls,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs

            abs_path_mock = MagicMock()
            abs_path_mock.is_absolute.return_value = True
            abs_path_mock.read_text.return_value = "vulnerable_line\n"
            mock_path_cls.return_value = abs_path_mock

            annotate_line_hash_batch([20], "/source_root")

        # Path() called with the absolute file_path, not joined with source_root
        mock_path_cls.assert_any_call("/abs/path/file.py")
        abs_path_mock.read_text.assert_called_once()

    def test_hash_is_16_hex_chars(self):
        """The stored hash must be a 16-character hex substring of SHA-256."""
        f = _Finding(id=30, file_path="src/b.py", line=1)

        mock_qs = MagicMock()
        mock_qs.__iter__ = lambda _: iter([f])

        with (
            patch("aist.tasks.enrich.Finding") as mock_finding,
            patch("aist.tasks.enrich.DojoMeta") as mock_dojometa,
            patch("aist.tasks.enrich.Path") as mock_path_cls,
        ):
            mock_finding.objects.filter.return_value.only.return_value = mock_qs
            p = MagicMock()
            p.is_absolute.return_value = False
            mock_path_cls.return_value = p
            joined = MagicMock()
            joined.read_text.return_value = "some_vulnerable_code()\n"
            p.__truediv__ = MagicMock(return_value=joined)

            annotate_line_hash_batch([30], "/src")

        # DojoMeta() was called once; check the value kwarg passed to the constructor
        mock_dojometa.objects.bulk_create.assert_called_once()
        created = mock_dojometa.objects.bulk_create.call_args[0][0]
        self.assertEqual(len(created), 1)
        # Each entry is the return value of DojoMeta(...); check via call_args_list
        self.assertEqual(mock_dojometa.call_count, 1)
        value_arg = mock_dojometa.call_args.kwargs["value"]
        self.assertEqual(len(value_arg), 16)
        int(value_arg, 16)  # must be valid hex


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
        FP / OOS / RA ancestors must NOT be filtered out: a finding already
        reviewed and dismissed should suppress the same code without re-triage.
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

        filter_kwargs = mock_dojometa.objects.filter.call_args.kwargs
        # Only mitigated findings are excluded from ancestry
        self.assertEqual(filter_kwargs.get("finding__is_mitigated"), False)
        # FP / OOS / RA are NOT filtered — reviewed decisions should suppress new findings
        self.assertNotIn("finding__false_p", filter_kwargs)
        self.assertNotIn("finding__out_of_scope", filter_kwargs)
        self.assertNotIn("finding__risk_accepted", filter_kwargs)
