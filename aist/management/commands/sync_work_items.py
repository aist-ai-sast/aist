from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from aist.models import WorkItemProvider
from aist.work_items.sync import sync_all_active_providers, sync_provider


class Command(BaseCommand):
    help = "Sync work-item statuses from external trackers (Jira, GitHub Issues, GitLab Issues, …)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider-id",
            type=int,
            default=None,
            help="Sync only the given WorkItemProvider ID. Omit to sync all active providers.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would be synced without persisting any changes.",
        )

    def handle(self, *args, **options):
        provider_id: int | None = options["provider_id"]
        dry_run: bool = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved"))

        if provider_id is not None:
            try:
                provider = WorkItemProvider.objects.get(pk=provider_id)
            except WorkItemProvider.DoesNotExist:
                msg = f"WorkItemProvider with id={provider_id} does not exist"
                raise CommandError(msg)

            if not provider.is_active:
                msg = f"Provider[{provider_id}] is inactive. Use --provider-id of an active provider."
                raise CommandError(msg)

            if dry_run:
                links_count = provider.work_item_links.count()
                self.stdout.write(
                    f"Would sync {links_count} link(s) for provider[{provider_id}] "
                    f"({provider.get_provider_type_display()} — {provider.name})",
                )
                return

            result = sync_provider(provider)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Provider[{result.provider_id}]: "
                    f"synced={result.synced} failed={result.failed} skipped={result.skipped}",
                ),
            )
            for err in result.errors:
                self.stderr.write(f"  ERROR: {err}")
        else:
            if dry_run:
                active = WorkItemProvider.objects.filter(sync_enabled=True, is_active=True)
                total_links = sum(p.work_item_links.count() for p in active)
                self.stdout.write(
                    f"Would sync {total_links} link(s) across {active.count()} active provider(s)",
                )
                return

            results = sync_all_active_providers()
            total_synced = sum(r.synced for r in results)
            total_failed = sum(r.failed for r in results)
            total_skipped = sum(r.skipped for r in results)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sync complete: providers={len(results)} "
                    f"synced={total_synced} failed={total_failed} skipped={total_skipped}",
                ),
            )
            for result in results:
                for err in result.errors:
                    self.stderr.write(f"  provider[{result.provider_id}] ERROR: {err}")
