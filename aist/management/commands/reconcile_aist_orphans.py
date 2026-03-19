from __future__ import annotations

from django.core.management.base import BaseCommand

from aist.tasks.reconciliation import reconcile_recent_orphans_task


class Command(BaseCommand):
    help = "Reconcile orphan AIST tests/findings/sourcefile links for recent pipelines."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24, help="How many recent hours to scan (default: 24)")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Maximum number of recent pipelines to inspect (default: 200)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only report violations; do not persist any fixes",
        )

    def handle(self, *args, **options):
        result = reconcile_recent_orphans_task.run(
            hours=options["hours"],
            batch_size=options["batch_size"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            "reconcile_aist_orphans: "
            f"dry_run={result['dry_run']} "
            f"hours={result['hours']} "
            f"processed={result['processed']} "
            f"pipelines_with_remaining_violations={result['pipelines_with_remaining_violations']} "
            f"remaining_violations={result['remaining_violations']} "
            f"recovered_stuck_dedup={result.get('recovered_stuck_dedup', 0)} "
            f"recovered_stuck_enrich={result.get('recovered_stuck_enrich', 0)}",
        )
