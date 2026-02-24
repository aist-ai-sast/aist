from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date
from dojo.models import Finding

from aist.dedupe.canonical import MatchVerdict, finding_signature
from aist.dedupe.custom import AIST_DEDUPE_CANDIDATE_TAG as _AIST_DEDUPE_CANDIDATE_TAG
from aist.dedupe.custom import (
    SUPPORTED_SCAN_TYPES,
    clear_aist_duplicate_tags,
    run_canonical_dedupe,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet

AIST_DEDUPE_CANDIDATE_TAG = _AIST_DEDUPE_CANDIDATE_TAG


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
                clear_aist_duplicate_tags(finding)

        result = run_canonical_dedupe(
            findings,
            apply=apply,
            dry_run=dry_run,
            apply_candidates=apply_candidates,
            fallback_ineligible=True,
        )
        summary = result.summary
        total_matched = summary.auto_duplicates + summary.candidates
        no_match = summary.processed - total_matched
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
        self.stdout.write(
            "recompute_aist_duplicates_totals: "
            f"processed={summary.processed} "
            f"auto_matched={summary.auto_duplicates} "
            f"candidates={summary.candidates} "
            f"total_matched={total_matched} "
            f"no_match={no_match} "
            f"promoted_candidates={summary.promoted_candidates} "
            f"applied_duplicates={summary.applied_duplicates} "
            f"conflicts={summary.conflicts}",
        )
        self._print_match_groups(findings=findings, decisions=result.decisions)

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

    def _print_match_groups(self, *, findings: list[Finding], decisions) -> None:  # type: ignore[no-untyped-def]
        groups: dict[tuple[int, str, int], list[Finding]] = defaultdict(list)
        signature_by_id = {finding.id: finding_signature(finding) for finding in findings}
        for finding in findings:
            signature = signature_by_id[finding.id]
            if signature.line is None or not signature.normalized_file_path:
                continue
            group_key = (finding.test.engagement.product_id, signature.normalized_file_path, signature.line)
            groups[group_key].append(finding)
        for group_key, group in groups.items():
            self._print_match_group(
                group_key=group_key,
                findings=sorted(group, key=lambda item: (item.created, item.id)),
                decisions=decisions,
            )

    def _print_match_group(
        self,
        *,
        group_key: tuple[int, str, int],
        findings: list[Finding],
        decisions,
    ) -> None:
        def verdict_for(finding_id: int) -> MatchVerdict:
            decision = decisions.get(finding_id)
            return decision.verdict if decision else MatchVerdict.NO_MATCH

        matched_members = [
            finding
            for finding in findings
            if verdict_for(finding.id) in {MatchVerdict.DUPLICATE, MatchVerdict.CANDIDATE}
        ]
        if not matched_members:
            return

        product_id, normalized_path, line = group_key
        has_duplicate = any(
            verdict_for(finding.id) == MatchVerdict.DUPLICATE
            for finding in findings
        )
        group_label = "duplicate_group" if has_duplicate else "candidate_group"
        self.stdout.write(
            f"{group_label} product_id={product_id} path={normalized_path} line={line} members={len(findings)}",
        )
        for finding in findings:
            decision = decisions.get(finding.id)
            if decision is None:
                continue
            root_part = f" root={decision.root_id}" if decision.root_id else ""
            self.stdout.write(
                f"  - finding_id={finding.id} verdict={decision.verdict} score={decision.score}{root_part} "
                f"scan_type={finding.test.test_type.name} title={finding.title}",
            )
