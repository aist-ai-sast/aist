from __future__ import annotations

import textwrap
from dataclasses import dataclass

from dojo.models import Finding

from aist.parser_overrides import HumanizedSnykCodeParser

SNYK_CODE_SCAN_TYPE = "Snyk Code Scan"


@dataclass(frozen=True)
class LegacyTitle:
    rule_id: str
    file_path: str | None


@dataclass(frozen=True)
class MigrationStats:
    processed: int
    changed: int
    skipped: int
    dry_run: bool

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "processed": self.processed,
            "changed": self.changed,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }


def _parse_legacy_title(title: str) -> LegacyTitle | None:
    if "_" not in title:
        return None
    maybe_rule, maybe_file_path = title.split("_", 1)
    if "/" not in maybe_rule:
        return None
    cleaned_path = maybe_file_path.strip() or None
    return LegacyTitle(rule_id=maybe_rule.strip(), file_path=cleaned_path)


def _get_rule_id(finding: Finding) -> str | None:
    vuln_id = (finding.vuln_id_from_tool or "").strip()
    if "/" in vuln_id:
        return vuln_id

    parsed = _parse_legacy_title(finding.title)
    if parsed:
        return parsed.rule_id
    return None


def _get_short_rule_title(rule_id: str | None, fallback_title: str) -> str:
    if rule_id:
        humanized = HumanizedSnykCodeParser._humanize_rule_id(rule_id)
        if humanized:
            return humanized
    return textwrap.shorten(fallback_title, 80)


def build_snyk_humanized_title(finding: Finding) -> str:
    rule_id = _get_rule_id(finding)
    return _get_short_rule_title(rule_id, finding.title)


def _queryset():
    return Finding.objects.filter(test__test_type__name__icontains=SNYK_CODE_SCAN_TYPE).select_related("test__test_type")


def migrate_snyk_finding_titles(
    *,
    dry_run: bool,
    limit: int | None = None,
    engagement_id: int | None = None,
) -> MigrationStats:
    queryset = _queryset().order_by("id")
    if engagement_id:
        queryset = queryset.filter(test__engagement_id=engagement_id)
    if limit:
        queryset = queryset[:limit]

    processed = 0
    changed = 0
    skipped = 0
    for finding in queryset:
        processed += 1
        new_title = build_snyk_humanized_title(finding)
        if new_title == finding.title:
            skipped += 1
            continue

        changed += 1
        if dry_run:
            continue
        finding.title = new_title
        finding.save(update_fields=["title"])

    return MigrationStats(
        processed=processed,
        changed=changed,
        skipped=skipped,
        dry_run=dry_run,
    )
