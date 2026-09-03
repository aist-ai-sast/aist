"""
Unit tests for aist/dedupe/custom.py.

Tests here cover the three phases of run_canonical_dedupe in isolation:
  Phase 1: _compute_group_decisions (pure scoring, no DB)
  Phase 2: _apply_duplicate_decision / _apply_candidate_decision (summary mutations)
  Orchestration: run_canonical_dedupe in dry-run mode (no DB writes)

No database is required — DummyFinding objects replace ORM instances.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from aist.dedupe.canonical import MatchVerdict, finding_signature
from aist.dedupe.custom import (
    AIST_DEDUPE_AUTO_TAG,
    AIST_DEDUPE_CANDIDATE_TAG,
    CanonicalDedupeSummary,
    CanonicalMatchDecision,
    _apply_candidate_decision,
    _apply_duplicate_decision,
    _compute_group_decisions,
    run_canonical_dedupe,
)

# ---------------------------------------------------------------------------
# Minimal stub that satisfies finding_signature() + _compute_group_decisions()
# ---------------------------------------------------------------------------


@dataclass
class _DummyTestType:
    name: str = "Semgrep JSON Report"


@dataclass
class _DummyEngagement:
    product_id: int = 1


@dataclass
class _DummyTest:
    test_type: _DummyTestType = field(default_factory=_DummyTestType)
    engagement: _DummyEngagement = field(default_factory=_DummyEngagement)


@dataclass
class DummyFinding:
    id: int
    title: str
    vuln_id_from_tool: str
    file_path: str
    line: int | None
    cwe: int | None = None
    component_name: str = ""
    component_version: str = ""
    duplicate: bool = False
    duplicate_finding_id: int | None = None
    duplicate_finding: object = None
    test: _DummyTest = field(default_factory=_DummyTest)
    created: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=UTC))

    def __hash__(self):
        return hash(self.id)


def _ssl_finding(finding_id: int, created: datetime | None = None) -> DummyFinding:
    """Helper: create a finding that scores DUPLICATE (family+CWE = score 6)."""
    return DummyFinding(
        id=finding_id,
        title="SSL verification disabled",
        vuln_id_from_tool="python.lang.security.audit.ssl-no-verify",
        file_path="app/net.py",
        line=10,
        cwe=295,
        created=created or datetime(2024, 1, finding_id, tzinfo=UTC),
    )


def _ssl_finding_snyk(finding_id: int, created: datetime | None = None) -> DummyFinding:
    """Helper: create a Snyk variant of the SSL finding — also scores DUPLICATE."""
    return DummyFinding(
        id=finding_id,
        title="SSL verify False",
        vuln_id_from_tool="python/SSLVerificationBypassed",
        file_path="app/net.py",
        line=10,
        cwe=295,
        created=created or datetime(2024, 1, finding_id, tzinfo=UTC),
    )


def _custom_rule_finding(finding_id: int) -> DummyFinding:
    """Helper: same rule key, no CWE/family → scores CANDIDATE (score 2)."""
    return DummyFinding(
        id=finding_id,
        title="Custom issue",
        vuln_id_from_tool="custom_rule_x",
        file_path="app/views.py",
        line=88,
        cwe=None,
        created=datetime(2024, 1, finding_id, tzinfo=UTC),
    )


def _no_line_finding(finding_id: int) -> DummyFinding:
    return DummyFinding(
        id=finding_id,
        title="Ineligible finding",
        vuln_id_from_tool="some_rule",
        file_path="app/main.py",
        line=None,
        created=datetime(2024, 1, finding_id, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Phase 1: _compute_group_decisions
# ---------------------------------------------------------------------------

class ComputeGroupDecisionsTests(SimpleTestCase):
    def test_single_finding_gets_no_match(self):
        f = _ssl_finding(1)
        sig_by_id = {f.id: finding_signature(f)}
        decisions = _compute_group_decisions([f], sig_by_id)
        self.assertEqual(decisions[f.id].verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(decisions[f.id].score, 0)

    def test_second_finding_scores_duplicate_when_family_and_cwe_match(self):
        f1 = _ssl_finding(1)
        f2 = _ssl_finding_snyk(2)
        sig_by_id = {f.id: finding_signature(f) for f in [f1, f2]}
        decisions = _compute_group_decisions([f1, f2], sig_by_id)
        self.assertEqual(decisions[f1.id].verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(decisions[f2.id].verdict, MatchVerdict.DUPLICATE)
        self.assertIsNotNone(decisions[f2.id].root_id)
        self.assertEqual(decisions[f2.id].root_id, f1.id)

    @override_settings(
        AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD=4,
        AIST_CANONICAL_CANDIDATE_MIN_SCORE=2,
    )
    def test_second_finding_scores_candidate_when_only_rule_key_matches(self):
        f1 = _custom_rule_finding(1)
        f2 = _custom_rule_finding(2)
        sig_by_id = {f.id: finding_signature(f) for f in [f1, f2]}
        decisions = _compute_group_decisions([f1, f2], sig_by_id)
        self.assertEqual(decisions[f1.id].verdict, MatchVerdict.NO_MATCH)
        # score=2 (rule match only) → DUPLICATE since threshold is 4 by default?
        # Actually SCORE_RULE_MATCH=2, DEFAULT_CANDIDATE_MIN_SCORE=2, DEFAULT_AUTO_DUPLICATE_THRESHOLD=4
        # so score=2 falls into CANDIDATE range
        self.assertEqual(decisions[f2.id].verdict, MatchVerdict.CANDIDATE)

    def test_ordering_uses_created_then_id(self):
        """Older finding (by created) should be the root, not the one with lower id."""
        newer_low_id = DummyFinding(
            id=1,
            title="SQL injection",
            vuln_id_from_tool="python/sql-injection",
            file_path="src/db.py",
            line=5,
            cwe=89,
            created=datetime(2024, 2, 1, tzinfo=UTC),  # newer
        )
        older_high_id = DummyFinding(
            id=2,
            title="SQL injection",
            vuln_id_from_tool="python/sql-injection",
            file_path="src/db.py",
            line=5,
            cwe=89,
            created=datetime(2024, 1, 1, tzinfo=UTC),  # older
        )
        sig_by_id = {f.id: finding_signature(f) for f in [newer_low_id, older_high_id]}
        decisions = _compute_group_decisions([newer_low_id, older_high_id], sig_by_id)
        # older_high_id (id=2) was created first → should be root
        self.assertEqual(decisions[older_high_id.id].verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(decisions[newer_low_id.id].verdict, MatchVerdict.DUPLICATE)
        self.assertEqual(decisions[newer_low_id.id].root_id, older_high_id.id)

    def test_best_root_follows_existing_duplicate_chain(self):
        """When the best previous finding is itself a duplicate, root should be its duplicate_finding."""
        root = _ssl_finding(1)
        broken = _ssl_finding_snyk(2)
        broken_root_obj = _ssl_finding(10)
        broken.duplicate = True
        broken.duplicate_finding = broken_root_obj
        third = _ssl_finding_snyk(3)

        sig_by_id = {f.id: finding_signature(f) for f in [root, broken, third]}
        decisions = _compute_group_decisions([root, broken, third], sig_by_id)
        # third scores best against broken; broken.duplicate → root is broken_root_obj
        self.assertEqual(decisions[third.id].root_id, broken_root_obj.id)


# ---------------------------------------------------------------------------
# Phase 2: _apply_candidate_decision
# ---------------------------------------------------------------------------

class ApplyCandidateDecisionTests(SimpleTestCase):
    def _make_summary(self) -> CanonicalDedupeSummary:
        return CanonicalDedupeSummary()

    def _make_decision(self, finding_id: int, root_id: int) -> dict[int, CanonicalMatchDecision]:
        return {finding_id: CanonicalMatchDecision(verdict=MatchVerdict.CANDIDATE, score=2, root_id=root_id)}

    def test_no_write_no_apply_candidates_just_counts(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        _apply_candidate_decision(finding, root, summary, decisions, should_write=False, apply_candidates=False)
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.conflicts, 0)
        self.assertEqual(summary.promoted_candidates, 0)

    def test_no_write_no_apply_finding_already_duplicate_increments_conflicts(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1,
                               duplicate=True, duplicate_finding_id=99)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        _apply_candidate_decision(finding, root, summary, decisions, should_write=False, apply_candidates=False)
        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(summary.candidates, 1)

    def test_apply_candidates_already_correct_root_counts_promoted(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1,
                               duplicate=True, duplicate_finding_id=1)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        _apply_candidate_decision(finding, root, summary, decisions, should_write=True, apply_candidates=True)
        self.assertEqual(summary.promoted_candidates, 1)
        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.applied_duplicates, 0)  # no write needed

    def test_apply_candidates_write_success_promotes(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        with patch("aist.dedupe.custom.set_duplicate"):
            _apply_candidate_decision(finding, root, summary, decisions, should_write=True, apply_candidates=True)
        self.assertEqual(summary.promoted_candidates, 1)
        self.assertEqual(summary.applied_duplicates, 1)
        self.assertEqual(summary.candidates, 1)

    def test_apply_candidates_write_exception_increments_conflicts(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        with patch("aist.dedupe.custom.set_duplicate", side_effect=RuntimeError("DB error")):
            _apply_candidate_decision(finding, root, summary, decisions, should_write=True, apply_candidates=True)
        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(summary.promoted_candidates, 0)
        self.assertEqual(summary.candidates, 1)

    def test_write_tag_as_candidate_when_not_apply_candidates(self):
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1)
        finding.tags = MagicMock()
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        with patch("aist.dedupe.custom.set_duplicate") as mock_set:
            _apply_candidate_decision(finding, root, summary, decisions, should_write=True, apply_candidates=False)
        mock_set.assert_not_called()
        finding.tags.add.assert_called_with(AIST_DEDUPE_CANDIDATE_TAG)
        finding.tags.remove.assert_called_with(AIST_DEDUPE_AUTO_TAG)
        self.assertEqual(summary.candidates, 1)

    def test_candidate_with_existing_duplicate_updates_root_id_in_decisions(self):
        """When finding is already a duplicate, root_id in decisions should reflect its actual duplicate_finding_id."""
        root = DummyFinding(id=1, title="root", vuln_id_from_tool="", file_path="", line=1)
        finding = DummyFinding(id=2, title="cand", vuln_id_from_tool="", file_path="", line=1,
                               duplicate=True, duplicate_finding_id=99)
        summary = self._make_summary()
        decisions = self._make_decision(2, 1)
        _apply_candidate_decision(finding, root, summary, decisions, should_write=False, apply_candidates=False)
        self.assertEqual(decisions[2].root_id, 99)


# ---------------------------------------------------------------------------
# Orchestration: run_canonical_dedupe in dry-run mode (no DB)
# ---------------------------------------------------------------------------

class RunCanonicalDedupeOrchestrationTests(SimpleTestCase):
    def test_dry_run_computes_correct_decisions_no_db_writes(self):
        f1 = _ssl_finding(1)
        f2 = _ssl_finding_snyk(2)
        result = run_canonical_dedupe(
            [f1, f2],
            apply=True,
            dry_run=True,
            apply_candidates=False,
        )
        self.assertEqual(result.decisions[f1.id].verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(result.decisions[f2.id].verdict, MatchVerdict.DUPLICATE)
        self.assertEqual(result.decisions[f2.id].root_id, f1.id)
        self.assertEqual(result.summary.auto_duplicates, 1)
        self.assertEqual(result.summary.applied_duplicates, 0)  # dry run

    def test_ineligible_finding_no_line_gets_no_match(self):
        f_no_line = _no_line_finding(1)
        f_eligible = _ssl_finding(2)
        result = run_canonical_dedupe(
            [f_no_line, f_eligible],
            apply=False,
            dry_run=True,
            apply_candidates=False,
        )
        self.assertEqual(result.decisions[f_no_line.id].verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(result.decisions[f_eligible.id].verdict, MatchVerdict.NO_MATCH)

    def test_target_finding_ids_restricts_application(self):
        """Decisions are computed for all but side-effects only for targets."""
        f1 = _ssl_finding(1)
        f2 = _ssl_finding_snyk(2)
        # f2 is a duplicate of f1 by score, but we only target f1
        result = run_canonical_dedupe(
            [f1, f2],
            apply=True,
            dry_run=True,
            apply_candidates=False,
            target_finding_ids={f1.id},
        )
        # decisions computed for both
        self.assertIn(f1.id, result.decisions)
        self.assertIn(f2.id, result.decisions)
        # f1 is NO_MATCH (it's the root), so unchanged=1
        self.assertEqual(result.summary.unchanged, 1)

    def test_unchanged_counts_no_match_targets(self):
        f1 = _ssl_finding(1)
        f2 = _ssl_finding(2)  # different file path from f1 after different product_id
        f2.test.engagement.product_id = 2  # different product → different group
        result = run_canonical_dedupe(
            [f1, f2],
            apply=False,
            dry_run=True,
            apply_candidates=False,
        )
        self.assertEqual(result.summary.unchanged, 2)
        self.assertEqual(result.summary.processed, 2)

    @override_settings(
        AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD=4,
        AIST_CANONICAL_CANDIDATE_MIN_SCORE=2,
    )
    def test_candidate_verdict_in_dry_run(self):
        f1 = _custom_rule_finding(1)
        f2 = _custom_rule_finding(2)
        result = run_canonical_dedupe(
            [f1, f2],
            apply=True,
            dry_run=True,
            apply_candidates=False,
        )
        self.assertEqual(result.decisions[f2.id].verdict, MatchVerdict.CANDIDATE)
        self.assertEqual(result.summary.candidates, 1)
        self.assertEqual(result.summary.applied_duplicates, 0)

    def test_apply_false_same_as_dry_run_for_summary(self):
        """apply=False should produce the same summary as dry_run=True for eligible findings."""
        f1 = _ssl_finding(1)
        f2 = _ssl_finding_snyk(2)
        result_no_apply = run_canonical_dedupe(
            [f1, f2], apply=False, dry_run=False, apply_candidates=False,
        )
        result_dry_run = run_canonical_dedupe(
            [f1, f2], apply=True, dry_run=True, apply_candidates=False,
        )
        self.assertEqual(result_no_apply.summary.auto_duplicates, result_dry_run.summary.auto_duplicates)
        self.assertEqual(result_no_apply.summary.applied_duplicates, result_dry_run.summary.applied_duplicates)
