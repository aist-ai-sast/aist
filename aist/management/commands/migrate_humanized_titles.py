from __future__ import annotations

from django.core.management.base import BaseCommand

from aist.utils.bearer_title_migration import migrate_bearer_finding_titles
from aist.utils.horusec_title_migration import migrate_horusec_finding_titles
from aist.utils.semgrep_title_migration import migrate_semgrep_finding_titles
from aist.utils.snyk_title_migration import migrate_snyk_finding_titles


class Command(BaseCommand):
    help = "Migrate legacy Snyk/Semgrep finding titles to short humanized format."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only calculate changes, do not update findings",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit amount of findings to process per scanner",
        )
        parser.add_argument(
            "--engagement-id",
            type=int,
            default=None,
            help="Restrict migration to a single engagement id",
        )
        parser.add_argument(
            "--scan-type",
            choices=["all", "snyk", "semgrep", "horusec", "bearer"],
            default="all",
            help="Run migration for all supported scanners or only one scanner",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        engagement_id = options["engagement_id"]
        scan_type = options["scan_type"]

        if scan_type in {"all", "snyk"}:
            snyk_result = migrate_snyk_finding_titles(
                dry_run=dry_run,
                limit=limit,
                engagement_id=engagement_id,
            )
            stats = snyk_result.as_dict()
            self.stdout.write(
                "migrate_humanized_titles[snyk]: "
                f"dry_run={stats['dry_run']} "
                f"processed={stats['processed']} "
                f"changed={stats['changed']} "
                f"skipped={stats['skipped']}",
            )

        if scan_type in {"all", "semgrep"}:
            semgrep_result = migrate_semgrep_finding_titles(
                dry_run=dry_run,
                limit=limit,
                engagement_id=engagement_id,
            )
            stats = semgrep_result.as_dict()
            self.stdout.write(
                "migrate_humanized_titles[semgrep]: "
                f"dry_run={stats['dry_run']} "
                f"processed={stats['processed']} "
                f"changed={stats['changed']} "
                f"skipped={stats['skipped']}",
            )

        if scan_type in {"all", "horusec"}:
            horusec_result = migrate_horusec_finding_titles(
                dry_run=dry_run,
                limit=limit,
                engagement_id=engagement_id,
            )
            stats = horusec_result.as_dict()
            self.stdout.write(
                "migrate_humanized_titles[horusec]: "
                f"dry_run={stats['dry_run']} "
                f"processed={stats['processed']} "
                f"changed={stats['changed']} "
                f"skipped={stats['skipped']}",
            )

        if scan_type in {"all", "bearer"}:
            bearer_result = migrate_bearer_finding_titles(
                dry_run=dry_run,
                limit=limit,
                engagement_id=engagement_id,
            )
            stats = bearer_result.as_dict()
            self.stdout.write(
                "migrate_humanized_titles[bearer]: "
                f"dry_run={stats['dry_run']} "
                f"processed={stats['processed']} "
                f"changed={stats['changed']} "
                f"skipped={stats['skipped']}",
            )
