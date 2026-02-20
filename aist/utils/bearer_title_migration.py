from __future__ import annotations

from dataclasses import dataclass

from dojo.models import Finding

from aist.parser_overrides import BEARER_SCAN_TYPE, normalize_bearer_title


@dataclass(frozen=True)
class BearerMigrationStats:
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
    return Finding.objects.filter(test__test_type__name__icontains=BEARER_SCAN_TYPE).only("id", "title")


def migrate_bearer_finding_titles(*, dry_run: bool, limit: int | None = None, engagement_id: int | None = None) -> BearerMigrationStats:
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
        new_title = normalize_bearer_title(finding.title)
        if new_title == finding.title:
            skipped += 1
            continue
        changed += 1
        if dry_run:
            continue
        finding.title = new_title
        finding.save(update_fields=["title"])

    return BearerMigrationStats(
        processed=processed,
        changed=changed,
        skipped=skipped,
        dry_run=dry_run,
    )
