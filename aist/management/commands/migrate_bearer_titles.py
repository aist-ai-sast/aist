from __future__ import annotations

from django.core.management.base import BaseCommand

from aist.utils.bearer_title_migration import migrate_bearer_finding_titles


class Command(BaseCommand):
    help = "Migrate Bearer CLI finding titles by removing trailing 'in <file>:<line>' location suffix."

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
            help="Limit amount of Bearer findings to process",
        )
        parser.add_argument(
            "--engagement-id",
            type=int,
            default=None,
            help="Restrict migration to a single engagement id",
        )

    def handle(self, *args, **options):
        result = migrate_bearer_finding_titles(
            dry_run=options["dry_run"],
            limit=options["limit"],
            engagement_id=options["engagement_id"],
        )
        stats = result.as_dict()
        self.stdout.write(
            "migrate_bearer_titles: "
            f"dry_run={stats['dry_run']} "
            f"processed={stats['processed']} "
            f"changed={stats['changed']} "
            f"skipped={stats['skipped']}",
        )
