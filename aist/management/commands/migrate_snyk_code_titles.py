from __future__ import annotations

from django.core.management.base import BaseCommand

from aist.utils.snyk_title_migration import migrate_snyk_finding_titles


class Command(BaseCommand):
    help = "Migrate legacy Snyk Code finding titles to the new short humanized format."

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
            help="Limit amount of Snyk Code findings to process",
        )
        parser.add_argument(
            "--engagement-id",
            type=int,
            default=None,
            help="Restrict migration to a single engagement id",
        )

    def handle(self, *args, **options):
        result = migrate_snyk_finding_titles(
            dry_run=options["dry_run"],
            limit=options["limit"],
            engagement_id=options["engagement_id"],
        )
        stats = result.as_dict()
        self.stdout.write(
            "migrate_snyk_code_titles: "
            f"dry_run={stats['dry_run']} "
            f"processed={stats['processed']} "
            f"changed={stats['changed']} "
            f"skipped={stats['skipped']}",
        )
