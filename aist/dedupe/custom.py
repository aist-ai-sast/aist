from __future__ import annotations

from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from dojo.finding import deduplication as dedupe_mod
from dojo.finding.deduplication import set_duplicate
from dojo.models import Finding

from aist.dedupe.canonical import CanonicalSignature, MatchVerdict, finding_signature, score_signatures
from aist.parser_overrides import (
    BEARER_SCAN_TYPE,
    CLAUDE_DIFF_SECURITY_SCAN_TYPE,
    CLAUDE_FULL_SECURITY_SCAN_TYPE,
    HORUSEC_SCAN_TYPE,
    SEMGREP_SCAN_TYPE,
    SNYK_CODE_SCAN_TYPE,
)

AIST_DEDUPE_AUTO_TAG = "aist:duplicate:auto"
AIST_DEDUPE_CANDIDATE_TAG = "aist:duplicate:candidate"
AIST_DEDUPE_TAGS = (AIST_DEDUPE_AUTO_TAG, AIST_DEDUPE_CANDIDATE_TAG)
SUPPORTED_SCAN_TYPES = (
    SNYK_CODE_SCAN_TYPE,
    "SnykCode Scan (Snyk Code Scan)",
    SEMGREP_SCAN_TYPE,
    HORUSEC_SCAN_TYPE,
    BEARER_SCAN_TYPE,
    CLAUDE_DIFF_SECURITY_SCAN_TYPE,
    CLAUDE_FULL_SECURITY_SCAN_TYPE,
)


@dataclass(slots=True)
class CanonicalDedupeSummary:
    processed: int = 0
    auto_duplicates: int = 0
    candidates: int = 0
    promoted_candidates: int = 0
    applied_duplicates: int = 0
    unchanged: int = 0
    conflicts: int = 0


@dataclass(slots=True)
class CanonicalMatchDecision:
    verdict: MatchVerdict
    score: int
    root_id: int | None = None


@dataclass(slots=True)
class CanonicalDedupeResult:
    summary: CanonicalDedupeSummary
    decisions: dict[int, CanonicalMatchDecision]


# ---------------------------------------------------------------------------
# Internal helpers: tag / finding state
# ---------------------------------------------------------------------------

def _is_supported_scan_type(finding: Finding) -> bool:
    test = getattr(finding, "test", None)
    test_type = getattr(test, "test_type", None)
    test_type_name = getattr(test_type, "name", None)
    return bool(test_type_name in SUPPORTED_SCAN_TYPES)


def _set_tag(finding: Finding, tag: str) -> None:
    with suppress(Exception):
        finding.tags.add(tag)


def _clear_tag(finding: Finding, tag: str) -> None:
    with suppress(Exception):
        finding.tags.remove(tag)


def clear_aist_duplicate_tags(finding: Finding) -> None:
    for tag in AIST_DEDUPE_TAGS:
        _clear_tag(finding, tag)


def _canonical_root_candidate(finding: Finding) -> Finding:
    if finding.duplicate and finding.duplicate_finding:
        return finding.duplicate_finding
    return finding


def _prepare_root_for_set_duplicate(finding: Finding) -> Finding:
    if finding.duplicate and finding.duplicate_finding_id is None:
        finding.duplicate = False
    return finding


# ---------------------------------------------------------------------------
# Internal helpers: fallback (non-canonical) deduplication
# ---------------------------------------------------------------------------

def _fallback_single_finding_dedupe(new_finding: Finding) -> None:
    dedup_alg = new_finding.test.deduplication_algorithm
    if dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL:
        dedupe_mod.deduplicate_unique_id_from_tool(new_finding)
    elif dedup_alg == settings.DEDUPE_ALGO_HASH_CODE:
        dedupe_mod.deduplicate_hash_code(new_finding)
    elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE:
        dedupe_mod.deduplicate_uid_or_hash_code(new_finding)
    else:
        dedupe_mod.deduplicate_legacy(new_finding)


def _fallback_batch_dedupe(findings: list[Finding]) -> None:
    if not findings:
        return
    dedup_alg = findings[0].test.deduplication_algorithm
    if dedup_alg == settings.DEDUPE_ALGO_HASH_CODE:
        dedupe_mod._dedupe_batch_hash_code(findings)
    elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL:
        dedupe_mod._dedupe_batch_unique_id(findings)
    elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE:
        dedupe_mod._dedupe_batch_uid_or_hash(findings)
    else:
        dedupe_mod._dedupe_batch_legacy(findings)


# ---------------------------------------------------------------------------
# Phase 1: pure scoring — no DB writes, no tag mutations
# ---------------------------------------------------------------------------

def _compute_group_decisions(
    group: list[Finding],
    signature_by_id: dict[int, CanonicalSignature],
) -> dict[int, CanonicalMatchDecision]:
    """
    Score all findings within one (product, file, line) group.

    Findings are ordered chronologically; each finding is scored against all
    predecessors. Returns a decision per finding — no side effects.
    """
    ordered = sorted(group, key=lambda f: (f.created, f.id))
    decisions: dict[int, CanonicalMatchDecision] = {}

    for idx, finding in enumerate(ordered):
        if idx == 0:
            # First finding in the group has nothing to compare against.
            decisions[finding.id] = CanonicalMatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)
            continue

        current_sig = signature_by_id[finding.id]
        best_score = 0
        best_verdict = MatchVerdict.NO_MATCH
        best_root: Finding | None = None

        for previous in ordered[:idx]:
            prev_sig = signature_by_id[previous.id]
            result = score_signatures(prev_sig, current_sig)
            if result.score > best_score:
                best_score = result.score
                best_verdict = result.verdict
                best_root = _canonical_root_candidate(previous)

        decisions[finding.id] = CanonicalMatchDecision(
            verdict=best_verdict,
            score=best_score,
            root_id=best_root.id if best_root else None,
        )

    return decisions


# ---------------------------------------------------------------------------
# Phase 2: apply — one function per verdict, isolated mutation
# ---------------------------------------------------------------------------

def _apply_duplicate_decision(
    finding: Finding,
    root: Finding,
    summary: CanonicalDedupeSummary,
    *,
    should_write: bool,
) -> None:
    """Apply a DUPLICATE verdict: mark finding as duplicate of root."""
    if finding.duplicate and finding.duplicate_finding_id == root.id:
        # Already correctly marked — nothing to do.
        summary.auto_duplicates += 1
        return

    if finding.duplicate and finding.duplicate_finding_id not in {root.id, None}:
        # Conflicting duplicate relationship exists.
        summary.conflicts += 1

    if should_write:
        try:
            set_duplicate(finding, _prepare_root_for_set_duplicate(root))
        except Exception:
            summary.conflicts += 1
            return
        _clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
        _set_tag(finding, AIST_DEDUPE_AUTO_TAG)
        summary.applied_duplicates += 1

    summary.auto_duplicates += 1


def _apply_candidate_decision(
    finding: Finding,
    root: Finding,
    summary: CanonicalDedupeSummary,
    decisions: dict[int, CanonicalMatchDecision],
    *,
    should_write: bool,
    apply_candidates: bool,
) -> None:
    """Apply a CANDIDATE verdict: either promote to duplicate or tag as candidate."""
    if should_write and apply_candidates:
        if finding.duplicate and finding.duplicate_finding_id == root.id:
            # Already promoted to duplicate of the right root.
            summary.promoted_candidates += 1
            summary.candidates += 1
            return
        try:
            set_duplicate(finding, _prepare_root_for_set_duplicate(root))
            summary.promoted_candidates += 1
            summary.applied_duplicates += 1
            _clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
            _set_tag(finding, AIST_DEDUPE_AUTO_TAG)
        except Exception:
            summary.conflicts += 1
    else:
        if finding.duplicate:
            summary.conflicts += 1
        if should_write:
            _set_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
            _clear_tag(finding, AIST_DEDUPE_AUTO_TAG)

    if finding.duplicate:
        decisions[finding.id].root_id = finding.duplicate_finding_id

    summary.candidates += 1


# ---------------------------------------------------------------------------
# Scope resolution: expand target findings to include historical matches
# ---------------------------------------------------------------------------

def _resolve_scope_for_targets(target_findings: list[Finding]) -> list[Finding]:
    if not target_findings:
        return []

    target_by_id = {finding.id: finding for finding in target_findings}
    target_signatures = {finding.id: finding_signature(finding) for finding in target_findings}
    scope_keys: set[tuple[int, str, int]] = set()
    product_line_pairs: set[tuple[int, int]] = set()
    for finding in target_findings:
        signature = target_signatures[finding.id]
        if signature.line is None or not signature.normalized_file_path or not _is_supported_scan_type(finding):
            continue
        product_id = finding.test.engagement.product_id
        key = (product_id, signature.normalized_file_path, signature.line)
        scope_keys.add(key)
        product_line_pairs.add((product_id, signature.line))

    if not scope_keys or not product_line_pairs:
        return target_findings

    product_line_q = Q()
    for product_id, line in product_line_pairs:
        product_line_q |= Q(test__engagement__product_id=product_id, line=line)

    scoped_candidates = list(
        Finding.objects.filter(test__test_type__name__in=SUPPORTED_SCAN_TYPES)
        .filter(product_line_q)
        .select_related("test", "test__engagement", "test__test_type")
        .order_by("id"),
    )

    scoped_findings: dict[int, Finding] = dict(target_by_id)
    for finding in scoped_candidates:
        signature = finding_signature(finding)
        if signature.line is None or not signature.normalized_file_path:
            continue
        key = (
            finding.test.engagement.product_id,
            signature.normalized_file_path,
            signature.line,
        )
        if key in scope_keys:
            scoped_findings[finding.id] = finding

    return sorted(scoped_findings.values(), key=lambda item: item.id)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_canonical_dedupe(
    findings: list[Finding],
    *,
    apply: bool,
    dry_run: bool,
    apply_candidates: bool,
    target_finding_ids: set[int] | None = None,
    fallback_ineligible: bool = False,
) -> CanonicalDedupeResult:
    if target_finding_ids is None:
        target_finding_ids = {f.id for f in findings}

    summary = CanonicalDedupeSummary(processed=len(target_finding_ids))
    findings_by_id = {f.id: f for f in findings}
    signature_by_id = {f.id: finding_signature(f) for f in findings}
    should_write = apply and not dry_run
    decisions: dict[int, CanonicalMatchDecision] = {}
    ineligible_targets: list[Finding] = []

    # --- Phase 1: group eligible findings and compute scores (pure) ---

    groups: dict[tuple[int, str, int], list[Finding]] = defaultdict(list)
    for finding in findings:
        sig = signature_by_id[finding.id]
        if sig.line is None or not sig.normalized_file_path:
            decisions[finding.id] = CanonicalMatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)
            if fallback_ineligible and finding.id in target_finding_ids:
                ineligible_targets.append(finding)
            continue
        group_key = (finding.test.engagement.product_id, sig.normalized_file_path, sig.line)
        groups[group_key].append(finding)

    for group in groups.values():
        decisions.update(_compute_group_decisions(group, signature_by_id))

    # --- Phase 2: apply decisions for target findings (side effects) ---

    for finding_id in target_finding_ids:
        decision = decisions.get(finding_id)
        if decision is None or decision.verdict == MatchVerdict.NO_MATCH:
            continue
        finding = findings_by_id.get(finding_id)
        if finding is None:
            continue
        root = findings_by_id.get(decision.root_id) if decision.root_id else None
        if root is None:
            continue

        if decision.verdict == MatchVerdict.DUPLICATE:
            _apply_duplicate_decision(finding, root, summary, should_write=should_write)
        elif decision.verdict == MatchVerdict.CANDIDATE:
            _apply_candidate_decision(
                finding, root, summary, decisions,
                should_write=should_write,
                apply_candidates=apply_candidates,
            )

    # --- Phase 3: fallback dedupe for findings ineligible for canonical matching ---

    if fallback_ineligible and ineligible_targets:
        _run_fallback_for_ineligible_targets(
            ineligible_target_findings=ineligible_targets,
            target_finding_ids=target_finding_ids,
            decisions=decisions,
            summary=summary,
            apply=apply,
            dry_run=dry_run,
        )

    summary.unchanged = sum(
        1
        for fid in target_finding_ids
        if decisions.get(fid, CanonicalMatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)).verdict
        == MatchVerdict.NO_MATCH
    )
    return CanonicalDedupeResult(summary=summary, decisions=decisions)


# ---------------------------------------------------------------------------
# Public hooks called by DefectDojo deduplication pipeline
# ---------------------------------------------------------------------------

def custom_dedupe_finding(new_finding: Finding, *args, **kwargs) -> None:
    if not _is_supported_scan_type(new_finding):
        _fallback_single_finding_dedupe(new_finding)
        return

    scoped = _resolve_scope_for_targets([new_finding])
    run_canonical_dedupe(
        scoped,
        apply=True,
        dry_run=False,
        apply_candidates=False,
        target_finding_ids={new_finding.id},
        fallback_ineligible=True,
    )


def custom_dedupe_batch(findings: list[Finding], *args, **kwargs) -> None:
    if not findings:
        return

    supported_findings = [f for f in findings if _is_supported_scan_type(f)]
    unsupported_findings = [f for f in findings if not _is_supported_scan_type(f)]

    if supported_findings:
        scoped = _resolve_scope_for_targets(supported_findings)
        run_canonical_dedupe(
            scoped,
            apply=True,
            dry_run=False,
            apply_candidates=False,
            target_finding_ids={f.id for f in supported_findings},
            fallback_ineligible=True,
        )

    if unsupported_findings:
        groups: dict[int, list[Finding]] = defaultdict(list)
        for finding in unsupported_findings:
            groups[finding.test_id].append(finding)
        for group in groups.values():
            ordered = sorted(group, key=lambda f: f.id)
            _fallback_batch_dedupe(ordered)


# ---------------------------------------------------------------------------
# Fallback deduplication for ineligible targets (no file/line)
# ---------------------------------------------------------------------------

def _run_fallback_for_ineligible_targets(
    *,
    ineligible_target_findings: list[Finding],
    target_finding_ids: set[int],
    decisions: dict[int, CanonicalMatchDecision],
    summary: CanonicalDedupeSummary,
    apply: bool,
    dry_run: bool,
) -> None:
    findings_by_test: dict[int, list[Finding]] = defaultdict(list)
    for finding in ineligible_target_findings:
        findings_by_test[finding.test_id].append(finding)

    for test_targets in findings_by_test.values():
        ordered_targets = sorted(test_targets, key=lambda f: f.id)
        test = ordered_targets[0].test
        dedup_alg = test.deduplication_algorithm

        if dedup_alg == settings.DEDUPE_ALGO_HASH_CODE:
            candidates_by_hash = dedupe_mod.find_candidates_for_deduplication_hash(test, ordered_targets)
        elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL:
            candidates_by_uid = dedupe_mod.find_candidates_for_deduplication_unique_id(test, ordered_targets)
        elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE:
            candidates_by_uid, candidates_by_hash = dedupe_mod.find_candidates_for_deduplication_uid_or_hash(
                test,
                ordered_targets,
            )
        else:
            candidates_by_title, candidates_by_cwe = dedupe_mod.find_candidates_for_deduplication_legacy(
                test,
                ordered_targets,
            )

        for finding in ordered_targets:
            if finding.id not in target_finding_ids:
                continue
            if dedup_alg == settings.DEDUPE_ALGO_HASH_CODE:
                matches = dedupe_mod.get_matches_from_hash_candidates(finding, candidates_by_hash)
            elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL:
                matches = dedupe_mod.get_matches_from_unique_id_candidates(finding, candidates_by_uid)
            elif dedup_alg == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE:
                matches = dedupe_mod.get_matches_from_uid_or_hash_candidates(
                    finding,
                    candidates_by_uid,
                    candidates_by_hash,
                )
            else:
                matches = dedupe_mod.get_matches_from_legacy_candidates(
                    finding,
                    candidates_by_title,
                    candidates_by_cwe,
                )
            match = next(iter(matches), None)
            if not match:
                continue

            best_root = match.duplicate_finding if match.duplicate else match
            if not best_root:
                continue

            decisions[finding.id] = CanonicalMatchDecision(
                verdict=MatchVerdict.DUPLICATE,
                score=0,
                root_id=best_root.id,
            )
            if finding.duplicate and finding.duplicate_finding_id == best_root.id:
                summary.auto_duplicates += 1
                continue
            if finding.duplicate and finding.duplicate_finding_id not in {best_root.id, None}:
                summary.conflicts += 1
            if apply and not dry_run:
                try:
                    set_duplicate(finding, best_root)
                    summary.applied_duplicates += 1
                except Exception:
                    summary.conflicts += 1
                    continue
                _clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
                _set_tag(finding, AIST_DEDUPE_AUTO_TAG)
            summary.auto_duplicates += 1
