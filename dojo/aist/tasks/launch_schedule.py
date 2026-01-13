import logging

from celery import shared_task
from django.utils import timezone

from dojo.aist.models import LaunchSchedule, PipelineLaunchQueue

logger = logging.getLogger("dojo.aist")


@shared_task(name="dojo.aist.tasks.launch_schedule.process_launch_schedules")
def process_launch_schedules():
    now = timezone.now()

    schedules = (
        LaunchSchedule.objects
        .select_related("launch_config")
        .all()
    )

    for sched in schedules:
        if not sched.enabled:
            continue

        try:
            next_time = sched.get_next_run_time(now=now)
        except Exception:
            logger.exception(
                "LaunchSchedule[%s] invalid cron_expression=%r (now=%s). Skipping.",
                sched.id,
                sched.cron_expression,
                now,
            )
            continue

        due_time = next_time  # get_next_run_time now returns the most recent due tick <= now

        last = sched.last_run_at
        if last and timezone.is_naive(last):
            last = timezone.make_aware(last, timezone.get_default_timezone())

        # already processed this tick
        if last and due_time <= last:
            logger.info(
                "LaunchSchedule[%s] not due: due_time=%s last_run_at=%s now=%s cron=%r",
                sched.id,
                due_time,
                last,
                now,
                sched.cron_expression,
            )
            continue

        project = sched.launch_config.project

        # Resolve config
        config = sched.launch_config

        # Enqueue one item per due schedule tick (project-only)
        PipelineLaunchQueue.objects.create(
            project=project,
            schedule=sched,
            launch_config=config,
        )

        logger.info(
            "LaunchSchedule[%s] enqueued PipelineLaunchQueue for project=%s launch_config=%s next_time=%s now=%s",
            sched.id,
            project.id,
            config.id,
            next_time,
            now,
        )

        sched.last_run_at = now
        sched.save(update_fields=["last_run_at"])
