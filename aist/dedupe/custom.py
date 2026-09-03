from __future__ import annotations

import logging
from collections import Counter, defaultdict
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q, prefetch_related_objects
from dojo.finding import deduplication as dedupe_mod
from dojo.finding.deduplication import set_duplicate
from dojo.models import Endpoint, Finding, Vulnerability_Id

from aist.dedupe.canonical import (
    DAST_SCAN_TYPE,
    CanonicalIdentityKind,
    CanonicalSignature,
    DastIdentityKey,
    LocationStrength,
    MatchVerdict,
    dast_identity_keys,
    dynamic_semantic_group_keys,
    finding_signature,
    location_strength,
    score_signatures,
)
from aist.logging_transport import install_pipeline_logging, uninstall_pipeline_file_logging
from aist.models import AISTPipeline
from aist.parser_overrides import (
    BEARER_SCAN_TYPE,
    CLAUDE_DIFF_SECURITY_SCAN_TYPE,
    CLAUDE_FULL_SECURITY_SCAN_TYPE,
    CLAUDE_INTAKE_DIFF_SCAN_TYPE,
    CLAUDE_INTAKE_REVIEW_SCAN_TYPE,
    HORUSEC_SCAN_TYPE,
    SEMGREP_SCAN_TYPE,
    SNYK_CODE_SCAN_TYPE,
)

AIST_DEDUPE_AUTO_TAG = "aist:duplicate:auto"
AIST_DEDUPE_CANDIDATE_TAG = "aist:duplicate:candidate"
AIST_DEDUPE_TAGS = (AIST_DEDUPE_AUTO_TAG, AIST_DEDUPE_CANDIDATE_TAG)
logger = logging.getLogger(__name__)
SUPPORTED_SCAN_TYPES = (
    SNYK_CODE_SCAN_TYPE,
    "SnykCode Scan (Snyk Code Scan)",
    SEMGREP_SCAN_TYPE,
    HORUSEC_SCAN_TYPE,
    BEARER_SCAN_TYPE,
    CLAUDE_DIFF_SECURITY_SCAN_TYPE,
    CLAUDE_FULL_SECURITY_SCAN_TYPE,
    CLAUDE_INTAKE_REVIEW_SCAN_TYPE,
    CLAUDE_INTAKE_DIFF_SCAN_TYPE,
    DAST_SCAN_TYPE,
)
_SEVERITY_RANK = {"Info": 0, "Low": 1, "Medium": 2, "High": 3, "Critical": 4}


class DedupeDecisionSource(StrEnum):
    CANONICAL = "canonical"
    UNIQUE_ID_FROM_TOOL = "unique_id_from_tool"
    CONFIGURED_FALLBACK = "configured_fallback"


@dataclass(slots=True)
class CanonicalDedupeSummary:
    processed: int = 0
    exact_duplicates: int = 0
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
    source: DedupeDecisionSource = DedupeDecisionSource.CANONICAL
    root_id: int | None = None
    evidence_contributions: dict[str, int] = field(default_factory=dict)
    location_strength: LocationStrength = LocationStrength.NONE
    fallback_reason: str = ""
    candidate_count: int = 0
    duration_ms: float = 0.0
    identity_kinds: tuple[CanonicalIdentityKind, ...] = ()
    conflicting_root_ids: tuple[int, ...] = ()
    dast_binding_id: int | None = None
    compared_finding_ids: set[int] = field(default_factory=set, repr=False)


@dataclass(slots=True)
class CanonicalDedupeResult:
    summary: CanonicalDedupeSummary
    decisions: dict[int, CanonicalMatchDecision]


@dataclass(slots=True)
class _ExactUidPhaseResult:
    resolved_finding_ids: set[int] = field(default_factory=set)
    roots_by_id: dict[int, Finding] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ScopedDastIdentity:
    dast_binding_id: int
    identity: DastIdentityKey


def _dast_binding_by_finding_id(
    findings: list[Finding],
    *,
    lock_pipelines: bool = False,
) -> dict[int, int | None]:
    """Resolve the DAST namespace from the pipeline that owns each finding's Test."""
    finding_ids_by_test: dict[int, list[int]] = defaultdict(list)
    result: dict[int, int | None] = {}
    for finding in findings:
        test_id = getattr(finding, "test_id", None)
        if test_id is None:
            result[finding.id] = None
            continue
        finding_ids_by_test[test_id].append(finding.id)

    binding_ids_by_test: dict[int, set[int]] = defaultdict(set)
    pipelines = (
        AISTPipeline.objects
        .filter(tests__id__in=finding_ids_by_test, dast_binding_id__isnull=False)
        .values_list("tests__id", "dast_binding_id")
    )
    if lock_pipelines:
        pipelines = pipelines.select_for_update()
    for test_id, binding_id in pipelines:
        binding_ids_by_test[test_id].add(binding_id)

    for test_id, finding_ids in finding_ids_by_test.items():
        binding_ids = binding_ids_by_test[test_id]
        binding_id = next(iter(binding_ids)) if len(binding_ids) == 1 else None
        result.update(dict.fromkeys(finding_ids, binding_id))
    return result


def _log_title(value: object) -> str:
    return " ".join(str(value or "").split())[:160]


def _log_pipeline_dedupe_result(
    *,
    target_findings: list[Finding],
    scoped_findings: list[Finding],
    result: CanonicalDedupeResult,
) -> None:
    """Write this applied batch to its existing per-pipeline log before duplicate cleanup."""
    if not any(finding.test.test_type.name == DAST_SCAN_TYPE for finding in target_findings):
        return
    test_ids = {finding.test_id for finding in target_findings}
    pipeline_ids = list(
        AISTPipeline.objects
        .filter(tests__id__in=test_ids)
        .values_list("id", flat=True)
        .distinct(),
    )
    if not pipeline_ids:
        return

    summary = result.summary
    findings_by_id = {finding.id: finding for finding in scoped_findings}
    missing_root_ids = {
        decision.root_id
        for finding in target_findings
        if (decision := result.decisions.get(finding.id)) is not None
        and decision.root_id is not None
        and decision.root_id not in findings_by_id
    }
    findings_by_id.update({
        finding.id: finding
        for finding in Finding.objects.filter(id__in=missing_root_ids).only("id", "title")
    })
    for pipeline_id in pipeline_ids:
        pipeline_logger = None
        try:
            pipeline_logger = install_pipeline_logging(pipeline_id, "INFO")
            pipeline_logger.info(
                "AIST dedupe: processed=%s exact=%s canonical_auto=%s candidates=%s no_match=%s "
                "applied=%s conflicts=%s",
                summary.processed,
                summary.exact_duplicates,
                summary.auto_duplicates - summary.exact_duplicates,
                summary.candidates,
                summary.unchanged,
                summary.applied_duplicates,
                summary.conflicts,
            )
            for finding in sorted(target_findings, key=lambda item: item.id):
                decision = result.decisions.get(finding.id)
                if decision is None or decision.verdict == MatchVerdict.NO_MATCH:
                    continue
                identity = (
                    decision.source
                    if decision.source == DedupeDecisionSource.UNIQUE_ID_FROM_TOOL
                    else ",".join(decision.identity_kinds) or "similarity"
                )
                root = findings_by_id.get(decision.root_id) if decision.root_id else None
                root_ids = decision.conflicting_root_ids or (() if decision.root_id is None else (decision.root_id,))
                pipeline_logger.info(
                    "AIST dedupe %s: finding=%s test=%s title=%r roots=%s root_title=%r "
                    "source=%s identity=%s score=%s reason=%s",
                    decision.verdict.upper(),
                    finding.id,
                    finding.test_id,
                    _log_title(finding.title),
                    ",".join(str(root_id) for root_id in root_ids) or "none",
                    _log_title(root.title) if root is not None else "",
                    decision.source,
                    identity,
                    decision.score,
                    decision.fallback_reason or "score",
                )
        except Exception:
            logger.exception(
                "Could not write canonical dedupe diagnostics (pipeline_id=%s)",
                pipeline_id,
            )
        finally:
            if pipeline_logger is not None:
                uninstall_pipeline_file_logging(pipeline_id)


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


def _automatic_merge_blocker(finding: Finding, root: Finding) -> str | None:
    """Return why an exact identity match is unsafe to apply without human review."""
    if (
        not root.active
        or root.false_p
        or root.out_of_scope
        or root.risk_accepted
        or root.is_mitigated
        or root.mitigated is not None
    ):
        return "canonical_root_is_not_actionable"
    if _SEVERITY_RANK.get(root.severity, -1) < _SEVERITY_RANK.get(finding.severity, -1):
        return "canonical_root_would_lower_severity"
    return None


def _lock_dynamic_identity_tables() -> None:
    """Keep DAST relation identities stable from apply-time validation through mutation."""
    tables = (
        Finding.endpoints.through._meta.db_table,
        Endpoint._meta.db_table,
        Vulnerability_Id._meta.db_table,
        AISTPipeline.tests.through._meta.db_table,
    )
    quoted = ", ".join(connection.ops.quote_name(table) for table in tables)
    with connection.cursor() as cursor:
        # All identifiers come from Django model metadata. SHARE permits concurrent dedupe readers
        # and holds endpoint/CVE/binding writers until the enclosing apply transaction commits.
        cursor.execute(f"LOCK TABLE {quoted} IN SHARE MODE")


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


def _run_exact_uid_dedupe(
    target_findings: list[Finding],
    *,
    decisions: dict[int, CanonicalMatchDecision],
) -> _ExactUidPhaseResult:
    """Run DefectDojo's exact producer-identity match before canonical correlation."""
    result = _ExactUidPhaseResult()
    targets_by_test: dict[int, list[Finding]] = defaultdict(list)
    for finding in target_findings:
        if getattr(finding, "unique_id_from_tool", None) and not finding.duplicate:
            targets_by_test[finding.test_id].append(finding)

    for test_targets in targets_by_test.values():
        ordered_targets = sorted(test_targets, key=lambda finding: finding.id)
        test = ordered_targets[0].test
        candidates_by_uid = dedupe_mod.find_candidates_for_deduplication_unique_id(
            test,
            ordered_targets,
        )
        dast_binding_id = None
        if test.test_type.name == DAST_SCAN_TYPE:
            candidate_findings = [
                candidate
                for candidates in candidates_by_uid.values()
                for candidate in candidates
            ]
            binding_by_finding_id = _dast_binding_by_finding_id([*ordered_targets, *candidate_findings])
            target_binding_ids = {binding_by_finding_id[finding.id] for finding in ordered_targets}
            if len(target_binding_ids) != 1 or None in target_binding_ids:
                continue
            dast_binding_id = next(iter(target_binding_ids))
            candidates_by_uid = {
                unique_id: [
                    candidate
                    for candidate in candidates
                    if binding_by_finding_id[candidate.id] == dast_binding_id
                ]
                for unique_id, candidates in candidates_by_uid.items()
            }

        for finding in ordered_targets:
            for root in dedupe_mod.get_matches_from_unique_id_candidates(finding, candidates_by_uid):
                blocker = _automatic_merge_blocker(finding, root)
                result.roots_by_id[root.id] = root
                if blocker is not None:
                    decisions[finding.id] = CanonicalMatchDecision(
                        verdict=MatchVerdict.CANDIDATE,
                        score=0,
                        source=DedupeDecisionSource.UNIQUE_ID_FROM_TOOL,
                        root_id=root.id,
                        fallback_reason=blocker,
                        dast_binding_id=dast_binding_id,
                    )
                    result.resolved_finding_ids.add(finding.id)
                    break
                decisions[finding.id] = CanonicalMatchDecision(
                    verdict=MatchVerdict.DUPLICATE,
                    score=0,
                    source=DedupeDecisionSource.UNIQUE_ID_FROM_TOOL,
                    root_id=root.id,
                    fallback_reason="exact_unique_id_from_tool",
                    dast_binding_id=dast_binding_id,
                )
                result.resolved_finding_ids.add(finding.id)
                break

    return result


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
            decisions[finding.id] = CanonicalMatchDecision(
                verdict=MatchVerdict.NO_MATCH,
                score=0,
                fallback_reason="no_comparable_older_finding",
            )
            continue

        current_sig = signature_by_id[finding.id]
        started = perf_counter()
        best_score = 0
        best_verdict = MatchVerdict.NO_MATCH
        best_root: Finding | None = None
        best_result = None

        for previous in ordered[:idx]:
            prev_sig = signature_by_id[previous.id]
            result = score_signatures(prev_sig, current_sig)
            current_rank = {
                MatchVerdict.NO_MATCH: 0,
                MatchVerdict.CANDIDATE: 1,
                MatchVerdict.DUPLICATE: 2,
            }[result.verdict]
            best_rank = {
                MatchVerdict.NO_MATCH: 0,
                MatchVerdict.CANDIDATE: 1,
                MatchVerdict.DUPLICATE: 2,
            }[best_verdict]
            if (current_rank, result.score) > (best_rank, best_score):
                best_score = result.score
                best_verdict = result.verdict
                best_root = _canonical_root_candidate(previous)
                best_result = result

        decisions[finding.id] = CanonicalMatchDecision(
            verdict=best_verdict,
            score=best_score,
            root_id=best_root.id if best_root else None,
            evidence_contributions=dict(best_result.evidence_contributions) if best_result else {},
            location_strength=best_result.location_strength if best_result else LocationStrength.NONE,
            fallback_reason=(best_result.fallback_reason if best_result else "no_comparable_older_finding"),
            candidate_count=idx,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            compared_finding_ids={previous.id for previous in ordered[:idx]},
        )

    return decisions


def _merge_group_decisions(
    decisions: dict[int, CanonicalMatchDecision],
    group_decisions: dict[int, CanonicalMatchDecision],
) -> None:
    verdict_rank = {
        MatchVerdict.NO_MATCH: 0,
        MatchVerdict.CANDIDATE: 1,
        MatchVerdict.DUPLICATE: 2,
    }
    for finding_id, candidate in group_decisions.items():
        existing = decisions.get(finding_id)
        if existing is None:
            decisions[finding_id] = candidate
            continue
        compared_ids = existing.compared_finding_ids | candidate.compared_finding_ids
        duration_ms = round(existing.duration_ms + candidate.duration_ms, 3)
        if (
            verdict_rank[candidate.verdict], candidate.score,
        ) > (
            verdict_rank[existing.verdict], existing.score,
        ):
            candidate.compared_finding_ids = compared_ids
            candidate.candidate_count = len(compared_ids)
            candidate.duration_ms = duration_ms
            decisions[finding_id] = candidate
        else:
            existing.compared_finding_ids = compared_ids
            existing.candidate_count = len(compared_ids)
            existing.duration_ms = duration_ms


def _resolve_dynamic_identity_clusters(
    findings: list[Finding],
    signature_by_id: dict[int, CanonicalSignature],
    binding_by_finding_id: dict[int, int | None],
    decisions: dict[int, CanonicalMatchDecision],
) -> None:
    """Promote only unambiguous, exact DAST identity clusters to automatic duplicates."""
    external_roots = {
        root.id: root
        for finding in findings
        if (root := _canonical_root_candidate(finding)).id not in binding_by_finding_id
    }
    if external_roots:
        root_findings = list(external_roots.values())
        binding_by_finding_id.update(_dast_binding_by_finding_id(root_findings))
        signature_by_id.update({root.id: finding_signature(root) for root in root_findings})

    members_by_key: dict[_ScopedDastIdentity, list[Finding]] = defaultdict(list)
    for finding in findings:
        signature = signature_by_id[finding.id]
        binding_id = binding_by_finding_id[finding.id]
        if not signature.dynamic or binding_id is None:
            continue
        for identity in dast_identity_keys(signature):
            members_by_key[_ScopedDastIdentity(binding_id, identity)].append(finding)

    ambiguous_keys = {
        scoped_key
        for scoped_key, members in members_by_key.items()
        if any(
            count > 1
            for test_id, count in Counter(member.test_id for member in members).items()
            if test_id is not None
        )
    }
    ordered_members_by_key = {
        key: sorted(members, key=lambda finding: (finding.created, finding.id))
        for key, members in members_by_key.items()
    }

    for finding in findings:
        signature = signature_by_id[finding.id]
        binding_id = binding_by_finding_id[finding.id]
        if not signature.dynamic or binding_id is None:
            continue
        decision = decisions.setdefault(
            finding.id,
            CanonicalMatchDecision(
                verdict=MatchVerdict.NO_MATCH,
                score=0,
                fallback_reason="no_comparable_older_finding",
            ),
        )
        finding_order = (finding.created, finding.id)
        roots: dict[int, tuple[Finding, list[DastIdentityKey]]] = {}
        has_ambiguous_identity = False
        has_incompatible_existing_root = False
        for identity in dast_identity_keys(signature):
            scoped_key = _ScopedDastIdentity(binding_id, identity)
            older_members = [
                member
                for member in ordered_members_by_key.get(scoped_key, ())
                if (member.created, member.id) < finding_order
            ]
            if not older_members:
                continue
            if scoped_key in ambiguous_keys:
                has_ambiguous_identity = True
                continue
            root = _canonical_root_candidate(older_members[0])
            root_binding_id = binding_by_finding_id.get(root.id)
            root_signature = signature_by_id.get(root.id)
            if (
                root_binding_id != binding_id
                or root_signature is None
                or identity not in dast_identity_keys(root_signature)
            ):
                has_incompatible_existing_root = True
                continue
            if root.id not in roots:
                roots[root.id] = (older_members[0], [])
            roots[root.id][1].append(identity)

        if has_incompatible_existing_root:
            decision.verdict = MatchVerdict.CANDIDATE
            decision.root_id = None
            decision.conflicting_root_ids = tuple(sorted(roots))
            decision.fallback_reason = "existing_root_outside_canonical_identity"
            continue
        if not roots:
            if has_ambiguous_identity:
                decision.verdict = MatchVerdict.CANDIDATE
                decision.fallback_reason = "identity_not_unique_within_test"
            continue
        if len(roots) > 1:
            decision.verdict = MatchVerdict.CANDIDATE
            decision.root_id = None
            decision.identity_kinds = tuple(sorted({
                identity.kind
                for _, identities in roots.values()
                for identity in identities
            }))
            decision.conflicting_root_ids = tuple(sorted(roots))
            decision.fallback_reason = "identity_keys_resolve_to_different_roots"
            continue

        root_id, (matched_member, identities) = next(iter(roots.items()))
        score = score_signatures(signature_by_id[matched_member.id], signature)
        decision.score = score.score
        decision.root_id = root_id
        decision.evidence_contributions = dict(score.evidence_contributions)
        decision.location_strength = score.location_strength
        decision.identity_kinds = tuple(sorted({identity.kind for identity in identities}))
        blocker = _automatic_merge_blocker(finding, _canonical_root_candidate(matched_member))
        if blocker is not None:
            decision.verdict = MatchVerdict.CANDIDATE
            decision.fallback_reason = blocker
            continue
        decision.verdict = MatchVerdict.DUPLICATE
        decision.fallback_reason = "exact_canonical_identity"


# ---------------------------------------------------------------------------
# Phase 2: apply — one function per verdict, isolated mutation
# ---------------------------------------------------------------------------

def _downgrade_duplicate_at_apply(
    finding: Finding,
    decision: CanonicalMatchDecision,
    summary: CanonicalDedupeSummary,
    *,
    reason: str,
    conflict: bool = False,
    conflicting_root_ids: tuple[int, ...] = (),
) -> None:
    """Keep the finding active when facts changed after the phase-1 decision."""
    decision.verdict = MatchVerdict.CANDIDATE
    decision.fallback_reason = reason
    if conflicting_root_ids:
        decision.conflicting_root_ids = tuple(sorted(set(conflicting_root_ids)))
    _set_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
    _clear_tag(finding, AIST_DEDUPE_AUTO_TAG)
    summary.candidates += 1
    if conflict:
        summary.conflicts += 1


def _exact_uid_root_is_still_valid(
    finding: Finding,
    root: Finding,
    decision: CanonicalMatchDecision,
    fresh_bindings: dict[int, int | None],
) -> bool:
    """Recheck the DefectDojo-owned exact identity and AIST-owned DAST namespace."""
    candidates_by_uid = dedupe_mod.find_candidates_for_deduplication_unique_id(
        finding.test,
        [finding],
    )
    exact_match_ids = {
        match.id
        for match in dedupe_mod.get_matches_from_unique_id_candidates(
            finding,
            candidates_by_uid,
        )
    }
    if root.id not in exact_match_ids:
        return False
    if decision.dast_binding_id is None:
        return True
    binding_id = fresh_bindings[finding.id]
    return (
        binding_id is not None
        and binding_id == fresh_bindings[root.id]
        and binding_id == decision.dast_binding_id
    )


def _apply_duplicate_decision(
    finding: Finding,
    root: Finding,
    decision: CanonicalMatchDecision,
    summary: CanonicalDedupeSummary,
    *,
    should_write: bool,
) -> None:
    """Apply a DUPLICATE verdict: mark finding as duplicate of root."""
    if not should_write:
        summary.auto_duplicates += 1
        return

    try:
        with transaction.atomic():
            locked = {
                row.id: row
                for row in Finding.objects
                .select_for_update()
                .filter(pk__in=(finding.id, root.id))
                .select_related("test", "test__engagement", "test__test_type")
                .prefetch_related("endpoints", "vulnerability_id_set")
            }
            finding = locked.get(finding.id)
            root = locked.get(root.id)
            if finding is None or root is None:
                summary.conflicts += 1
                return
            if finding.duplicate and finding.duplicate_finding_id == root.id:
                summary.auto_duplicates += 1
                return
            if finding.duplicate and finding.duplicate_finding_id not in {root.id, None}:
                _downgrade_duplicate_at_apply(
                    finding,
                    decision,
                    summary,
                    reason="duplicate_root_changed_before_apply",
                    conflict=True,
                    conflicting_root_ids=(root.id, finding.duplicate_finding_id),
                )
                return
            exact_decision = decision.source == DedupeDecisionSource.UNIQUE_ID_FROM_TOOL
            dast_decision = decision.dast_binding_id is not None
            fresh_bindings = (
                _dast_binding_by_finding_id([finding, root], lock_pipelines=True)
                if dast_decision
                else {}
            )
            if exact_decision and not _exact_uid_root_is_still_valid(
                finding,
                root,
                decision,
                fresh_bindings,
            ):
                _downgrade_duplicate_at_apply(
                    finding,
                    decision,
                    summary,
                    reason="exact_identity_changed_before_apply",
                    conflict=True,
                )
                return

            dynamic_decision = not exact_decision and dast_decision
            if dynamic_decision:
                fresh_signatures = {
                    finding.id: finding_signature(finding),
                    root.id: finding_signature(root),
                }
                dynamic_match = (
                    fresh_signatures[finding.id].dynamic
                    and fresh_signatures[root.id].dynamic
                )
                binding_id = fresh_bindings[finding.id]
                shared_identities = (
                    set(dast_identity_keys(fresh_signatures[finding.id]))
                    & set(dast_identity_keys(fresh_signatures[root.id]))
                )
                if (
                    not dynamic_match
                    or binding_id is None
                    or binding_id != fresh_bindings[root.id]
                    or binding_id != decision.dast_binding_id
                    or not shared_identities
                ):
                    _downgrade_duplicate_at_apply(
                        finding,
                        decision,
                        summary,
                        reason="canonical_identity_changed_before_apply",
                        conflict=True,
                    )
                    return
                scope = resolve_dedupe_scope([finding])
                fresh_scope = list(
                    Finding.objects
                    .select_for_update()
                    .filter(pk__in=[row.id for row in scope])
                    .select_related("test", "test__engagement", "test__test_type")
                    .prefetch_related("endpoints", "vulnerability_id_set")
                    .order_by("id"),
                )
                _dast_binding_by_finding_id(fresh_scope, lock_pipelines=True)
                refreshed = run_canonical_dedupe(
                    fresh_scope,
                    apply=False,
                    dry_run=True,
                    apply_candidates=False,
                    target_finding_ids={finding.id},
                ).decisions.get(finding.id)
                if (
                    refreshed is None
                    or refreshed.verdict != MatchVerdict.DUPLICATE
                    or refreshed.root_id != root.id
                ):
                    _downgrade_duplicate_at_apply(
                        finding,
                        decision,
                        summary,
                        reason="canonical_cluster_changed_before_apply",
                        conflict=True,
                        conflicting_root_ids=(
                            refreshed.conflicting_root_ids if refreshed is not None else ()
                        ),
                    )
                    return
            blocker = None
            if root.duplicate_finding_id is not None:
                blocker = "canonical_root_changed_before_apply"
            elif exact_decision or dynamic_decision:
                blocker = _automatic_merge_blocker(finding, root)
            if blocker is not None:
                _downgrade_duplicate_at_apply(
                    finding,
                    decision,
                    summary,
                    reason=blocker,
                )
                return
            set_duplicate(finding, _prepare_root_for_set_duplicate(root))
            _clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
            _set_tag(finding, AIST_DEDUPE_AUTO_TAG)
            summary.applied_duplicates += 1
    except Exception:
        summary.conflicts += 1
        return

    summary.auto_duplicates += 1


def _apply_candidate_decision(
    finding: Finding,
    root: Finding | None,
    summary: CanonicalDedupeSummary,
    decisions: dict[int, CanonicalMatchDecision],
    *,
    should_write: bool,
    apply_candidates: bool,
) -> None:
    """Apply a CANDIDATE verdict: either promote to duplicate or tag as candidate."""
    if should_write and apply_candidates and root is not None:
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
        if decisions[finding.id].conflicting_root_ids or finding.duplicate:
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

def resolve_dedupe_scope(target_findings: list[Finding]) -> list[Finding]:
    """Expand target findings with historical candidates required by AIST dedupe."""
    if not target_findings:
        return []

    prefetch_related_objects(target_findings, "endpoints", "vulnerability_id_set")
    target_by_id = {finding.id: finding for finding in target_findings}
    target_signatures = {finding.id: finding_signature(finding) for finding in target_findings}
    target_bindings = _dast_binding_by_finding_id(target_findings)
    scope_keys: set[tuple[int, str, int]] = set()
    product_line_pairs: set[tuple[int, int]] = set()
    dynamic_product_ids: dict[int, int] = {}
    dynamic_endpoint_ids: dict[int, set[int]] = defaultdict(set)
    dynamic_paths: dict[int, set[str | None]] = defaultdict(set)
    dynamic_vulnerability_ids: dict[int, set[str]] = defaultdict(set)
    dynamic_component_cwes: dict[int, set[tuple[str, int]]] = defaultdict(set)
    dynamic_service_cwes: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for finding in target_findings:
        signature = target_signatures[finding.id]
        if not _is_supported_scan_type(finding):
            continue
        product_id = finding.test.engagement.product_id
        if signature.dynamic:
            binding_id = target_bindings[finding.id]
            if binding_id is None:
                continue
            dynamic_product_ids[binding_id] = product_id
            for endpoint in finding.endpoints.all():
                if endpoint.pk is not None:
                    dynamic_endpoint_ids[binding_id].add(endpoint.pk)
                # The Endpoint model already canonicalizes a root path to NULL. Include that stored
                # value directly so equivalent root endpoints with a different endpoint row (for
                # example explicit versus implicit default port) still reach the canonical URL scorer.
                dynamic_paths[binding_id].add(endpoint.path)
            dynamic_vulnerability_ids[binding_id].update(signature.vulnerability_ids)
            if signature.cwe and not signature.cwe_inferred:
                if signature.component_name:
                    dynamic_component_cwes[binding_id].add((signature.component_name, signature.cwe))
                if signature.service:
                    dynamic_service_cwes[binding_id].add((signature.service, signature.cwe))
        elif signature.line is not None and signature.normalized_file_path:
            key = (product_id, signature.normalized_file_path, signature.line)
            scope_keys.add(key)
            product_line_pairs.add((product_id, signature.line))

    if not any((
        product_line_pairs,
        dynamic_endpoint_ids,
        dynamic_paths,
        dynamic_vulnerability_ids,
        dynamic_component_cwes,
        dynamic_service_cwes,
    )):
        return target_findings

    candidate_q: Q | None = None

    def include(query: Q) -> None:
        nonlocal candidate_q
        candidate_q = query if candidate_q is None else candidate_q | query

    for product_id, line in product_line_pairs:
        include(Q(test__engagement__product_id=product_id, line=line))
    for binding_id in set(dynamic_endpoint_ids) | set(dynamic_paths):
        endpoint_q = Q(endpoints__id__in=dynamic_endpoint_ids[binding_id])
        stored_paths = {path for path in dynamic_paths[binding_id] if path is not None}
        if stored_paths:
            endpoint_q |= Q(endpoints__path__in=stored_paths)
        if None in dynamic_paths[binding_id]:
            endpoint_q |= Q(endpoints__path__isnull=True)
        include(
            Q(test__aist_pipelines__dast_binding_id=binding_id)
            & Q(test__engagement__product_id=dynamic_product_ids[binding_id])
            & endpoint_q,
        )
    for binding_id, vulnerability_ids in dynamic_vulnerability_ids.items():
        if vulnerability_ids:
            include(Q(
                test__aist_pipelines__dast_binding_id=binding_id,
                test__engagement__product_id=dynamic_product_ids[binding_id],
                vulnerability_id__vulnerability_id__in=vulnerability_ids,
            ))
    for binding_id, component_cwes in dynamic_component_cwes.items():
        for component_name, cwe in component_cwes:
            include(Q(
                test__aist_pipelines__dast_binding_id=binding_id,
                test__engagement__product_id=dynamic_product_ids[binding_id],
                component_name__iexact=component_name,
                cwe=cwe,
            ))
    for binding_id, service_cwes in dynamic_service_cwes.items():
        for service, cwe in service_cwes:
            include(Q(
                test__aist_pipelines__dast_binding_id=binding_id,
                test__engagement__product_id=dynamic_product_ids[binding_id],
                service__iexact=service,
                cwe=cwe,
            ))

    if candidate_q is None:
        return target_findings

    scoped_candidates = list(
        Finding.objects.filter(test__test_type__name__in=SUPPORTED_SCAN_TYPES)
        .filter(candidate_q)
        .select_related("test", "test__engagement", "test__test_type")
        .prefetch_related("endpoints", "vulnerability_id_set")
        .distinct()
        .order_by("id"),
    )
    candidate_bindings = _dast_binding_by_finding_id(scoped_candidates)

    scoped_findings: dict[int, Finding] = dict(target_by_id)
    for finding in scoped_candidates:
        signature = finding_signature(finding)
        product_id = finding.test.engagement.product_id
        key = (product_id, signature.normalized_file_path, signature.line)
        comparable_to_dynamic_target = any(
            target.test.engagement.product_id == product_id
            and target_bindings[target.id] == candidate_bindings[finding.id]
            and (
                location_strength(target_signatures[target.id], signature) != LocationStrength.NONE
                or bool(
                    set(dynamic_semantic_group_keys(target_signatures[target.id]))
                    & set(dynamic_semantic_group_keys(signature)),
                )
            )
            for target in target_findings
            if target_signatures[target.id].dynamic
        )
        if key in scope_keys or comparable_to_dynamic_target:
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
    all_target_finding_ids = (
        {f.id for f in findings}
        if target_finding_ids is None
        else set(target_finding_ids)
    )

    summary = CanonicalDedupeSummary(processed=len(all_target_finding_ids))
    findings_by_id = {f.id: f for f in findings}
    should_write = apply and not dry_run
    decisions: dict[int, CanonicalMatchDecision] = {}

    # Phase 0: preserve DefectDojo's exact producer identity before AIST adds
    # cross-scanner canonical correlation for the findings that remain.
    exact_phase = _run_exact_uid_dedupe(
        [
            findings_by_id[finding_id]
            for finding_id in all_target_finding_ids
            if finding_id in findings_by_id
        ],
        decisions=decisions,
    )
    findings_by_id.update(exact_phase.roots_by_id)
    canonical_target_finding_ids = all_target_finding_ids - exact_phase.resolved_finding_ids
    findings = [
        finding
        for finding in findings
        if finding.id not in exact_phase.resolved_finding_ids
    ]
    signature_by_id = {f.id: finding_signature(f) for f in findings}
    binding_by_finding_id = _dast_binding_by_finding_id(findings)
    ineligible_targets: list[Finding] = []

    # --- Phase 1: group eligible findings and compute scores (pure) ---

    groups: dict[tuple, list[Finding]] = defaultdict(list)
    for finding in findings:
        sig = signature_by_id[finding.id]
        product_id = finding.test.engagement.product_id
        if sig.dynamic:
            binding_id = binding_by_finding_id[finding.id]
            if binding_id is None:
                decisions[finding.id] = CanonicalMatchDecision(
                    verdict=MatchVerdict.NO_MATCH,
                    score=0,
                    fallback_reason="missing_dast_binding_scope",
                )
                continue
            dynamic_keys: list[tuple] = []
            for location in sig.web_locations:
                location_key = (
                    "web-path", binding_id, location.path,
                ) if location.path else (
                    "web-root", binding_id, location.scheme, location.host, location.port,
                )
                dynamic_keys.append(location_key)
            dynamic_keys.extend(
                ("semantic", binding_id, *semantic_key)
                for semantic_key in dynamic_semantic_group_keys(sig)
            )
            for dynamic_key in dynamic_keys:
                if all(member.id != finding.id for member in groups[dynamic_key]):
                    groups[dynamic_key].append(finding)
            if dynamic_keys:
                continue
        if not sig.dynamic and sig.line is not None and sig.normalized_file_path:
            groups["source", product_id, sig.normalized_file_path, sig.line].append(finding)
            continue
        decisions[finding.id] = CanonicalMatchDecision(
            verdict=MatchVerdict.NO_MATCH,
            score=0,
            fallback_reason="no_usable_location",
        )
        if fallback_ineligible and not sig.dynamic and finding.id in canonical_target_finding_ids:
            ineligible_targets.append(finding)

    for group in groups.values():
        _merge_group_decisions(decisions, _compute_group_decisions(group, signature_by_id))

    _resolve_dynamic_identity_clusters(findings, signature_by_id, binding_by_finding_id, decisions)

    for finding in findings:
        if finding.id not in decisions:
            decisions[finding.id] = CanonicalMatchDecision(
                verdict=MatchVerdict.NO_MATCH,
                score=0,
                fallback_reason="no_comparable_older_finding",
            )
        if signature_by_id[finding.id].dynamic:
            decisions[finding.id].dast_binding_id = binding_by_finding_id[finding.id]

    # --- Phase 2: apply decisions for target findings (side effects) ---

    has_dynamic_auto = any(
        decision.verdict == MatchVerdict.DUPLICATE
        and decision.source == DedupeDecisionSource.CANONICAL
        and signature_by_id[finding_id].dynamic
        for finding_id, decision in decisions.items()
        if finding_id in canonical_target_finding_ids
    )
    apply_context = transaction.atomic() if should_write and has_dynamic_auto else nullcontext()
    with apply_context:
        if should_write and has_dynamic_auto:
            _lock_dynamic_identity_tables()
        for finding_id in all_target_finding_ids:
            decision = decisions.get(finding_id)
            if decision is None or decision.verdict == MatchVerdict.NO_MATCH:
                continue
            finding = findings_by_id.get(finding_id)
            if finding is None:
                continue
            if decision.verdict == MatchVerdict.DUPLICATE:
                root = findings_by_id.get(decision.root_id) if decision.root_id else None
                if root is None:
                    continue
                auto_before = summary.auto_duplicates
                _apply_duplicate_decision(
                    finding,
                    root,
                    decision,
                    summary,
                    should_write=should_write,
                )
                if (
                    decision.source == DedupeDecisionSource.UNIQUE_ID_FROM_TOOL
                    and summary.auto_duplicates > auto_before
                ):
                    summary.exact_duplicates += 1
            elif decision.verdict == MatchVerdict.CANDIDATE:
                root = findings_by_id.get(decision.root_id) if decision.root_id else None
                _apply_candidate_decision(
                    finding, root, summary, decisions,
                    should_write=should_write,
                    apply_candidates=(
                        apply_candidates
                        and decision.source != DedupeDecisionSource.UNIQUE_ID_FROM_TOOL
                    ),
                )

    # --- Phase 3: fallback dedupe for findings ineligible for canonical matching ---

    if fallback_ineligible and ineligible_targets:
        _run_fallback_for_ineligible_targets(
            ineligible_target_findings=ineligible_targets,
            target_finding_ids=canonical_target_finding_ids,
            decisions=decisions,
            summary=summary,
            apply=apply,
            dry_run=dry_run,
        )

    summary.unchanged = sum(
        1
        for fid in all_target_finding_ids
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

    scoped = resolve_dedupe_scope([new_finding])
    result = run_canonical_dedupe(
        scoped,
        apply=True,
        dry_run=False,
        apply_candidates=False,
        target_finding_ids={new_finding.id},
        fallback_ineligible=True,
    )
    _log_pipeline_dedupe_result(
        target_findings=[new_finding],
        scoped_findings=scoped,
        result=result,
    )


def custom_dedupe_batch(findings: list[Finding], *args, **kwargs) -> None:
    if not findings:
        return

    supported_findings = [f for f in findings if _is_supported_scan_type(f)]
    unsupported_findings = [f for f in findings if not _is_supported_scan_type(f)]

    if supported_findings:
        scoped = resolve_dedupe_scope(supported_findings)
        result = run_canonical_dedupe(
            scoped,
            apply=True,
            dry_run=False,
            apply_candidates=False,
            target_finding_ids={f.id for f in supported_findings},
            fallback_ineligible=True,
        )
        _log_pipeline_dedupe_result(
            target_findings=supported_findings,
            scoped_findings=scoped,
            result=result,
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
                source=DedupeDecisionSource.CONFIGURED_FALLBACK,
                root_id=best_root.id,
                fallback_reason="configured_noncanonical_fallback",
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
