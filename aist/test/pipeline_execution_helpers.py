from aist.models import AISTStatus, PipelineLaunchRequest, PipelineLaunchRequestState
from aist.tasks.pipeline import run_pipeline_execution


def run_persisted_sast_pipeline(pipeline, params, *, async_user=None):
    """Exercise the generic worker boundary with a persisted SAST request."""
    PipelineLaunchRequest.objects.update_or_create(
        pipeline=pipeline,
        defaults={
            "project": pipeline.project,
            "requester": async_user,
            "params_snapshot": dict(params),
            "state": PipelineLaunchRequestState.PUBLISHED,
            "task_name": run_pipeline_execution.name,
            "task_args_snapshot": [],
        },
    )
    type(pipeline).objects.filter(pk=pipeline.pk).update(status=AISTStatus.EXECUTING)
    pipeline.status = AISTStatus.EXECUTING
    return run_pipeline_execution.run(pipeline.pk)
