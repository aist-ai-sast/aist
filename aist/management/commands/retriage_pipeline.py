"""Triage, in batches, the findings of a finished pipeline that still need an AI response for it."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from aist.tasks.local_triage import RetriageRefusedError, queue_retriage


class Command(BaseCommand):
    help = (
        "Queue local AI triage for a finished pipeline's active findings that hold no AI response for it, in "
        "the same bounded batches a running pipeline uses. The pipeline's status is not changed; progress is "
        "written to the pipeline log. Run it in the celeryworker container, which holds the pipeline sources."
    )

    def add_arguments(self, parser):
        parser.add_argument("pipeline_id", help="Full id of a finished pipeline.")

    def handle(self, *args, pipeline_id: str, **options):
        try:
            batches = queue_retriage(pipeline_id)
        except RetriageRefusedError as exc:
            raise CommandError(str(exc)) from exc
        if not batches:
            self.stdout.write(f"Pipeline {pipeline_id}: every candidate finding already has an AI response.")
            return
        self.stdout.write(
            f"Pipeline {pipeline_id}: queued {sum(len(batch.finding_ids) for batch in batches)} findings in "
            f"{len(batches)} batches.",
        )
