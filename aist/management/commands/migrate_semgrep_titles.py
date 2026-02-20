from __future__ import annotations

from django.core.management.base import BaseCommand

from aist.utils.semgrep_title_migration import migrate_semgrep_finding_titles


class Command(BaseCommand):
    help = "Migrate legacy Semgrep finding titles to a short humanized format."

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
            help="Limit amount of Semgrep findings to process",
        )
        parser.add_argument(
            "--engagement-id",
            type=int,
            default=None,
            help="Restrict migration to a single engagement id",
        )

    def handle(self, *args, **options):
        result = migrate_semgrep_finding_titles(
            dry_run=options["dry_run"],
            limit=options["limit"],
            engagement_id=options["engagement_id"],
        )
        stats = result.as_dict()
        self.stdout.write(
            "migrate_semgrep_titles: "
            f"dry_run={stats['dry_run']} "
            f"processed={stats['processed']} "
            f"changed={stats['changed']} "
            f"skipped={stats['skipped']}",
        )
