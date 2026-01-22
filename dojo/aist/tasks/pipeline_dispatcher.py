import logging
from operator import itemgetter

from celery import current_app, shared_task
from django.utils import timezone

from dojo.aist.models import AISTProjectVersion, PipelineLaunchQueue
from dojo.aist.pipeline_args import PipelineArguments
from dojo.aist.tasks.pipeline import run_sast_pipeline
from dojo.aist.utils.pipeline import create_pipeline_object

logger = logging.getLogger("dojo.aist")


@shared_task(name="dojo.aist.tasks.pipeline_dispatcher.dispatch_queued_pipelines")
def dispatch_queued_pipelines():
    """
    Dispatch queued pipeline launches while respecting per-worker concurrency limits.

    It inspects active tasks on all workers to determine how many pipelines are
    currently running per worker. For each queued launch request, it checks the
    associated schedule's max_concurrent_per_worker setting. If the limit is 0, the
    pipeline is launched immediately. Otherwise, the dispatcher ensures that at least
    one worker has fewer than `max_concurrent_per_worker` pipelines running before
    starting a new pipeline. When a pipeline is dispatched, the queue item is marked
    as dispatched and linked to the created AISTPipeline object.
    """
    app = current_app
    inspect = app.control.inspect()
    active = inspect.active() or {}
    # Count currently running pipelines per worker
    running_per_worker = {}
    for worker, tasks in active.items():
        count = 0
        for t in tasks or []:
            # Only count AIST pipeline tasks
            if t.get("name") == "dojo.aist.tasks.pipeline.run_sast_pipeline":
                count += 1
        running_per_worker[worker] = count

    logger.info(
        "Dispatcher: active workers=%s running_per_worker=%s queued_count=%s",
        list((active or {}).keys()),
        running_per_worker,
        PipelineLaunchQueue.objects.filter(dispatched=False).count(),
    )

    # Iterate over queued items in FIFO order
    queued = (
        PipelineLaunchQueue.objects
        .filter(dispatched=False)
        .select_related("schedule", "project")
        .order_by("created")
    )
    for item in queued:
        sched = item.schedule
        if not sched or not sched.enabled:
            continue
        limit = int(sched.max_concurrent_per_worker)
        # By contract, max_concurrent_per_worker must be >= 1.
        if limit < 1:
            logger.error(
                "Invalid max_concurrent_per_worker=%s for schedule id=%s; expected >= 1",
                sched.max_concurrent_per_worker,
                sched.id,
            )
            continue
        # If there is a concurrency limit, ensure a worker is available
        if not running_per_worker:
            # Can't inspect workers -> don't block dispatching, just log.
            logger.warning(
                "Dispatcher: can't inspect active tasks (running_per_worker empty) but limit=%s set. "
                "Proceeding to dispatch without capacity check (schedule_id=%s queue_id=%s).",
                limit,
                getattr(sched, "id", None),
                item.id,
            )
        else:
            available_worker = None
            for worker, count in sorted(running_per_worker.items(), key=itemgetter(1)):
                if count < limit:
                    available_worker = worker
                    break
            if available_worker is None:
                logger.info(
                    "Dispatcher: all workers at capacity (limit=%s). Stop cycle. queue_id=%s schedule_id=%s",
                    limit, item.id, getattr(sched, "id", None),
                )
                break
        # Create pipeline and dispatch Celery task
        project = item.project

        try:
            params = PipelineArguments.normalize_params(project=project, raw_params=item.launch_config.params)
            project_version = AISTProjectVersion.objects.get(id=params["project_version"]["id"])
        except Exception:
            logger.exception(
                "Dispatcher: failed to build params for queue_id=%s project=%s schedule_id=%s launch_config_id=%s. Skipping.",
                item.id,
                getattr(project, "id", None),
                getattr(sched, "id", None),
                getattr(item.launch_config, "id", None) if item.launch_config else None,
            )
            continue

        params["launch_config_id"] = item.launch_config_id
        pipeline = create_pipeline_object(project, project_version, None)
        async_result = run_sast_pipeline.delay(pipeline.id, params)
        logger.info(
            "Dispatcher: dispatch pipeline=%s queue_id=%s project=%s project_version=%s schedule_id=%s launch_config=%s",
            pipeline.id,
            item.id,
            project.id,
            project_version.id if project_version else None,
            getattr(sched, "id", None),
            item.launch_config_id,
        )
        pipeline.run_task_id = async_result.id
        pipeline.save(update_fields=["run_task_id"])
        # Mark queue item as dispatched
        item.pipeline = pipeline
        item.dispatched = True
        item.dispatched_at = timezone.now()
        item.save(update_fields=["pipeline", "dispatched", "dispatched_at"])
        # Update running count for selected worker
        if limit > 0 and running_per_worker:
            # increment count on first available worker
            for worker, count in running_per_worker.items():
                if count < limit:
                    running_per_worker[worker] += 1
                    break
