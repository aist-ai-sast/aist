import logging
from dataclasses import dataclass
from datetime import datetime

from celery import shared_task
from croniter import croniter
from django.db import transaction
from django.utils import timezone

from aist.execution.enqueue import LaunchEnqueueError, LaunchPrincipal, enqueue_pipeline_launch
from aist.models import (
    LaunchSchedule,
)
from aist.pipeline_args import PipelineArguments

logger = logging.getLogger("aist")
_SCHEDULE_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class _DueScheduleClaim:
    schedule_id: int
    due_time: datetime


def _claim_due_schedule(*, now: datetime, exclude_ids: set[int]) -> _DueScheduleClaim | None:
    with transaction.atomic():
        sched = (
            LaunchSchedule.objects
            .select_for_update(skip_locked=True)
            .select_related("launch_config")
            .filter(enabled=True, next_run_at__isnull=False, next_run_at__lte=now)
            .exclude(pk__in=exclude_ids)
            .order_by("next_run_at", "pk")
            .first()
        )
        if sched is None:
            return None
        due_time = sched.next_run_at
        try:
            next_run_at = croniter(sched.cron_expression, due_time).get_next(datetime)
            if timezone.is_naive(next_run_at):
                next_run_at = timezone.make_aware(next_run_at, timezone.get_default_timezone())
        except Exception:
            sched.next_run_at = None
            sched.last_attempt_at = now
            sched.last_error_code = "INVALID_CRON"
            sched.last_error_detail = "The stored cron expression is invalid; update the schedule."
            sched.save(update_fields=[
                "next_run_at",
                "last_attempt_at",
                "last_error_code",
                "last_error_detail",
            ])
            return _DueScheduleClaim(schedule_id=0, due_time=due_time)
        # Reserving the next tick makes this due slot invisible to another beat
        # worker while admission runs. Failure restores the canonical due tick.
        sched.next_run_at = next_run_at
        sched.last_attempt_at = now
        sched.save(update_fields=["next_run_at", "last_attempt_at"])
        return _DueScheduleClaim(schedule_id=sched.pk, due_time=due_time)


@shared_task(name="aist.tasks.launch_schedule.process_launch_schedules")
def process_launch_schedules(async_user=None):
    del async_user
    now = timezone.now()
    processed = 0
    attempted_ids: set[int] = set()
    while processed < _SCHEDULE_BATCH_SIZE:
        claim = _claim_due_schedule(now=now, exclude_ids=attempted_ids)
        if claim is None:
            break
        processed += 1
        if claim.schedule_id == 0:
            continue
        attempted_ids.add(claim.schedule_id)
        sched = (
            LaunchSchedule.objects
            .select_related(
                "launch_config__project__product__prod_type__aist_organization",
                "launch_config__trigger_project_version",
                "launch_config__dast_binding__target__integration__dast_state",
                "launch_config__dast_binding__target__integration__vpn_integration__vpn_secret",
            )
            .get(pk=claim.schedule_id)
        )
        config = sched.launch_config
        project = config.project
        try:
            enqueue_pipeline_launch(
                arguments=PipelineArguments.from_launch_config(config),
                principal=LaunchPrincipal.for_schedule(organization=project.organization),
                schedule=sched,
                launch_config=config,
                client_request_key=f"schedule:{sched.pk}:{claim.due_time.isoformat()}",
            )
        except LaunchEnqueueError as exc:
            LaunchSchedule.objects.filter(pk=sched.pk).update(
                next_run_at=claim.due_time,
                last_attempt_at=now,
                last_error_code=exc.code,
                last_error_detail=exc.safe_detail,
            )
            logger.warning(
                "LaunchSchedule[%s] admission rejected for due_time=%s code=%s.",
                sched.id,
                claim.due_time,
                exc.code,
            )
            continue
        LaunchSchedule.objects.filter(pk=sched.pk).update(
            last_run_at=claim.due_time,
            last_attempt_at=now,
            last_error_code="",
            last_error_detail="",
        )
