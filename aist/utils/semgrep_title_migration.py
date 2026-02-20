from __future__ import annotations

from dataclasses import dataclass

from dojo.models import Finding

from aist.parser_overrides import SEMGREP_SCAN_TYPE, build_semgrep_humanized_title


@dataclass(frozen=True)
class SemgrepMigrationStats:
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
    return Finding.objects.filter(test__test_type__name__icontains=SEMGREP_SCAN_TYPE).only(
        "id",
        "title",
        "vuln_id_from_tool",
        "file_path",
        "line",
    )


def migrate_semgrep_finding_titles(*, dry_run: bool, limit: int | None = None, engagement_id: int | None = None) -> SemgrepMigrationStats:
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
        check_id = str(finding.vuln_id_from_tool or finding.title or "")
        new_title = build_semgrep_humanized_title(
            check_id=check_id,
            file_path=finding.file_path,
            line=finding.line,
        )
        if new_title == finding.title:
            skipped += 1
            continue
        changed += 1
        if dry_run:
            continue
        finding.title = new_title
        finding.save(update_fields=["title"])

    return SemgrepMigrationStats(
        processed=processed,
        changed=changed,
        skipped=skipped,
        dry_run=dry_run,
    )
