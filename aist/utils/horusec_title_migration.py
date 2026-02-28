from __future__ import annotations

from dataclasses import dataclass

from dojo.models import Finding

from aist.parser_overrides import HORUSEC_SCAN_TYPE, extract_horusec_cwe, normalize_horusec_title


@dataclass(frozen=True)
class HorusecMigrationStats:
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


def _queryset():
    return Finding.objects.filter(test__test_type__name__icontains=HORUSEC_SCAN_TYPE).only("id", "title", "description", "cwe")


def _is_missing_cwe(value: object) -> bool:
    if value is None:
        return True
    try:
        return int(value) <= 0
    except (TypeError, ValueError):
        return True


def migrate_horusec_finding_titles(*, dry_run: bool, limit: int | None = None, engagement_id: int | None = None) -> HorusecMigrationStats:
    queryset = _queryset().order_by("id")
    if engagement_id:
        queryset = queryset.filter(test__engagement_id=engagement_id)
    if limit:
        queryset = queryset[:limit]

    processed = 0
    changed = 0
    skipped = 0

    for finding in queryset.iterator(chunk_size=500):
        processed += 1
        old_title = finding.title
        old_cwe = finding.cwe
        new_title = normalize_horusec_title(finding.title)
        extracted_cwe = extract_horusec_cwe(finding.description) or extract_horusec_cwe(finding.title)
        new_cwe = old_cwe
        if _is_missing_cwe(old_cwe) and extracted_cwe:
            new_cwe = extracted_cwe

        if new_title == old_title and new_cwe == old_cwe:
            skipped += 1
            continue

        changed += 1
        if dry_run:
            continue
        update_fields: list[str] = []
        if new_title != old_title:
            finding.title = new_title
            update_fields.append("title")
        if new_cwe != old_cwe:
            finding.cwe = new_cwe
            update_fields.append("cwe")
        if update_fields:
            finding.save(update_fields=update_fields)

    return HorusecMigrationStats(
        processed=processed,
        changed=changed,
        skipped=skipped,
        dry_run=dry_run,
    )
