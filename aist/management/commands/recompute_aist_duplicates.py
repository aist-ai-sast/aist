from __future__ import annotations

from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date
from dojo.finding.deduplication import set_duplicate
from dojo.models import Finding

from aist.dedupe.canonical import MatchVerdict, finding_signature, score_signatures
from aist.parser_overrides import (
    BEARER_SCAN_TYPE,
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
)

if TYPE_CHECKING:
    from django.db.models import QuerySet


@dataclass(slots=True)
class RecomputeSummary:
    processed: int = 0
    auto_duplicates: int = 0
    candidates: int = 0
    promoted_candidates: int = 0
    applied_duplicates: int = 0
    unchanged: int = 0
    conflicts: int = 0


@dataclass(slots=True)
class MatchDecision:
    verdict: MatchVerdict
    score: int
    root_id: int | None = None


class Command(BaseCommand):
    help = "Recompute AIST duplicate/candidate matches using canonical cross-scanner matching."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--apply", action="store_true", default=False)
        parser.add_argument("--apply-candidates", action="store_true", default=False)
        parser.add_argument("--pipeline-id", type=str, default=None)
        parser.add_argument("--product-id", type=int, default=None)
        parser.add_argument("--since", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--clear-existing-aist-duplicate-tags", action="store_true", default=False)

    def handle(self, *args, **options):
        dry_run_opt = bool(options["dry_run"])
        apply_candidates = bool(options["apply_candidates"])
        apply = bool(options["apply"]) or apply_candidates
        dry_run = dry_run_opt or not apply

        since_raw = options.get("since")
        since_date = None
        if since_raw:
            since_date = parse_date(since_raw)
            if since_date is None:
                msg = "Invalid --since format. Expected YYYY-MM-DD"
                raise CommandError(msg)

        queryset = self._base_queryset(
            pipeline_id=options.get("pipeline_id"),
            product_id=options.get("product_id"),
            since_date=since_date,
        )
        limit = options.get("limit")
        if limit:
            queryset = queryset[:limit]
        findings = list(queryset)

        if apply and options.get("clear_existing_aist_duplicate_tags"):
            for finding in findings:
                self._clear_aist_tags(finding)

        summary = self._recompute(
            findings,
            apply=apply,
            dry_run=dry_run,
            apply_candidates=apply_candidates,
        )
        self.stdout.write(
            "recompute_aist_duplicates: "
            f"mode={'dry-run' if dry_run else 'apply'} "
            f"processed={summary.processed} "
            f"auto_duplicates={summary.auto_duplicates} "
            f"candidates={summary.candidates} "
            f"promoted_candidates={summary.promoted_candidates} "
            f"applied_duplicates={summary.applied_duplicates} "
            f"unchanged={summary.unchanged} "
            f"conflicts={summary.conflicts}",
        )

    def _base_queryset(self, *, pipeline_id: str | None, product_id: int | None, since_date) -> QuerySet[Finding]:
        qs = (
            Finding.objects
            .filter(test__test_type__name__in=SUPPORTED_SCAN_TYPES)
            .select_related("test", "test__engagement", "test__test_type")
            .order_by("id")
        )
        if pipeline_id:
            qs = qs.filter(test__aist_pipelines__id=pipeline_id)
        if product_id:
            qs = qs.filter(test__engagement__product_id=product_id)
        if since_date:
            qs = qs.filter(date__gte=since_date)
        return qs.distinct()

    def _recompute(
        self,
        findings: list[Finding],
        *,
        apply: bool,
        dry_run: bool,
        apply_candidates: bool,
    ) -> RecomputeSummary:
        summary = RecomputeSummary(processed=len(findings))
        decision_by_finding: dict[int, MatchDecision] = {}
        groups: dict[tuple[int, str, int], list[Finding]] = defaultdict(list)
        signature_by_id = {finding.id: finding_signature(finding) for finding in findings}

        for finding in findings:
            signature = signature_by_id[finding.id]
            if signature.line is None or not signature.normalized_file_path:
                decision_by_finding[finding.id] = MatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)
                continue
            group_key = (finding.test.engagement.product_id, signature.normalized_file_path, signature.line)
            groups[group_key].append(finding)

        for group_key, group in groups.items():
            ordered = sorted(group, key=lambda item: (item.created, item.id))
            for idx, finding in enumerate(ordered):
                if idx == 0:
                    decision_by_finding[finding.id] = MatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)
                    continue
                current_signature = signature_by_id[finding.id]
                best_score = 0
                best_verdict = MatchVerdict.NO_MATCH
                best_root: Finding | None = None

                for previous in ordered[:idx]:
                    prev_signature = signature_by_id[previous.id]
                    score = score_signatures(prev_signature, current_signature)
                    if score.score > best_score:
                        best_score = score.score
                        best_verdict = score.verdict
                        best_root = previous.duplicate_finding if previous.duplicate else previous

                if best_verdict == MatchVerdict.DUPLICATE and best_root:
                    decision_by_finding[finding.id] = MatchDecision(
                        verdict=MatchVerdict.DUPLICATE,
                        score=best_score,
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
                        self._clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
                        self._set_tag(finding, AIST_DEDUPE_AUTO_TAG)
                    summary.auto_duplicates += 1
                    continue

                if best_verdict == MatchVerdict.CANDIDATE:
                    decision_by_finding[finding.id] = MatchDecision(
                        verdict=MatchVerdict.CANDIDATE,
                        score=best_score,
                        root_id=best_root.id if best_root else None,
                    )
                    if apply and not dry_run and apply_candidates and best_root:
                        if finding.duplicate and finding.duplicate_finding_id == best_root.id:
                            summary.promoted_candidates += 1
                            summary.candidates += 1
                            continue
                        try:
                            set_duplicate(finding, best_root)
                            summary.promoted_candidates += 1
                            summary.applied_duplicates += 1
                            self._clear_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
                            self._set_tag(finding, AIST_DEDUPE_AUTO_TAG)
                        except Exception:
                            summary.conflicts += 1
                    else:
                        if finding.duplicate:
                            summary.conflicts += 1
                        if apply and not dry_run:
                            self._set_tag(finding, AIST_DEDUPE_CANDIDATE_TAG)
                            self._clear_tag(finding, AIST_DEDUPE_AUTO_TAG)
                    if finding.duplicate:
                        decision_by_finding[finding.id].root_id = finding.duplicate_finding_id
                    summary.candidates += 1
                    continue

                decision_by_finding[finding.id] = MatchDecision(verdict=MatchVerdict.NO_MATCH, score=best_score)

            self._print_match_group(group_key=group_key, findings=ordered, decisions=decision_by_finding)

        summary.unchanged = sum(
            1
            for finding in findings
            if decision_by_finding.get(finding.id, MatchDecision(verdict=MatchVerdict.NO_MATCH, score=0)).verdict
            == MatchVerdict.NO_MATCH
        )
        return summary

    def _print_match_group(
        self,
        *,
        group_key: tuple[int, str, int],
        findings: list[Finding],
        decisions: dict[int, MatchDecision],
    ) -> None:
        matched_members = [
            finding
            for finding in findings
            if decisions.get(finding.id, MatchDecision(MatchVerdict.NO_MATCH, 0)).verdict
            in {MatchVerdict.DUPLICATE, MatchVerdict.CANDIDATE}
        ]
        if not matched_members:
            return

        product_id, normalized_path, line = group_key
        has_duplicate = any(
            decisions.get(finding.id, MatchDecision(MatchVerdict.NO_MATCH, 0)).verdict == MatchVerdict.DUPLICATE
            for finding in findings
        )
        group_label = "duplicate_group" if has_duplicate else "candidate_group"
        self.stdout.write(
            f"{group_label} product_id={product_id} path={normalized_path} line={line} members={len(findings)}",
        )
        for finding in findings:
            decision = decisions.get(finding.id, MatchDecision(verdict=MatchVerdict.NO_MATCH, score=0))
            root_part = f" root={decision.root_id}" if decision.root_id else ""
            self.stdout.write(
                f"  - finding_id={finding.id} verdict={decision.verdict} score={decision.score}{root_part} "
                f"scan_type={finding.test.test_type.name} title={finding.title}",
            )

    def _clear_aist_tags(self, finding: Finding) -> None:
        for tag in AIST_DEDUPE_TAGS:
            self._clear_tag(finding, tag)

    @staticmethod
    def _set_tag(finding: Finding, tag: str) -> None:
        with suppress(Exception):
            finding.tags.add(tag)

    @staticmethod
    def _clear_tag(finding: Finding, tag: str) -> None:
        with suppress(Exception):
            finding.tags.remove(tag)
