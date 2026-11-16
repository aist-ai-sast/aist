"""Local AI triage: the Celery chain that triages a pipeline's batches one bridge session at a time."""
from __future__ import annotations

from pathlib import Path

from celery import chain, shared_task
from celery.exceptions import Ignore
from django.conf import settings
from django.db import transaction

from aist.integrations.claude import claude_subprocess_env
from aist.launch_data import AITriageType
from aist.logging_transport import scoped_pipeline_logging
from aist.models import AISTPipeline, AISTStatus, LocalTriageBatch, LocalTriageBatchState
from aist.services.ai_triage import PushGuard, pipeline_findings, push_guard, resolve_triage_type, triage_candidates
from aist.services.local_triage import (
    LocalTriageInProgressError,
    cancel_open_batches,
    end_batch,
    plan_local_triage,
    start_batch,
    unanswered_finding_ids,
    untriaged_finding_ids,
)
from aist.services.pipeline_lifecycle import is_terminal_pipeline_status, lock_pipeline
from aist.utils.bridge_client_factory import (
    build_bridge_client_from_settings,
    format_extra_args,
    prepare_bridge_output_dir,
)
from aist.utils.pipeline import finish_pipeline, send_or_finish_degraded, set_pipeline_status

TRIAGE_RESULT_FILENAME = "triage_done.flag"


class RetriageRefusedError(Exception):

    """Why the findings of a pipeline cannot be retriaged now."""


@shared_task(bind=True, name="aist.tasks.ai.push_request_to_local_triage")
def push_request_to_local_triage(
    self,
    pipeline_id: str,
    finding_ids: list[int],
    log_level: str = "INFO",
    async_user_id=None,
) -> None:
    """
    Queue the selected findings that need triage as bounded batches, move the pipeline to waiting and start them.

    Selected findings of other pipelines are not triaged. A selection with none of the pipeline's own findings finishes
    the pipeline with warnings; one whose findings all have a verdict or are closed finishes it cleanly.
    """
    with scoped_pipeline_logging(pipeline_id, log_level) as log:
        own_ids: set[int] = set()
        batches: list[LocalTriageBatch] = []
        with transaction.atomic():
            pipeline = lock_pipeline(pipeline_id)
            guard = push_guard(pipeline, pipeline_id, AISTStatus.PUSH_TO_AI, log)
            if guard is PushGuard.READY:
                own_ids = set(pipeline_findings(pipeline).filter(id__in=finding_ids).values_list("id", flat=True))
                batches = plan_local_triage(pipeline_id, own_ids)
            if batches:
                set_pipeline_status(pipeline, AISTStatus.WAITING_RESULT_FROM_AI)
                transaction.on_commit(lambda: send_or_finish_degraded(
                    pipeline_id, lambda: start_local_triage(pipeline_id, batches, log_level), log,
                ))

        if guard is PushGuard.UNEXPECTED_STATUS:
            finish_pipeline(pipeline_id, degraded=True)
            return
        if guard is not PushGuard.READY:
            return
        if len(own_ids) < len(set(finding_ids)):
            log.warning(
                "%s of the selected findings are not findings of pipeline %s and are not triaged",
                len(set(finding_ids)) - len(own_ids), pipeline_id,
            )
        if batches:
            log.info(
                "Local triage planned: %s findings in %s batches",
                sum(len(batch.finding_ids) for batch in batches), len(batches),
            )
        elif own_ids:
            log.info("Every selected finding of pipeline %s has a verdict or is closed; nothing to triage", pipeline_id)
            finish_pipeline(pipeline_id)
        else:
            log.warning("None of the selected findings is a finding of pipeline %s", pipeline_id)
            finish_pipeline(pipeline_id, degraded=True)


def start_local_triage(pipeline_id: str, batches: list[LocalTriageBatch], log_level: str = "INFO") -> None:
    """
    Triage ``batches`` one after another, one bridge session each, then finish the pipeline's triage.

    When the chain cannot be sent, the pipeline's open batches are cancelled and the error is raised.
    """
    try:
        chain(
            *(triage_local_batch.si(pipeline_id, batch.pk, log_level) for batch in batches),
            finish_local_triage.si(pipeline_id, log_level),
        ).apply_async()
    except Exception:
        cancel_open_batches(pipeline_id)
        raise


def queue_retriage(pipeline_id: str) -> list[LocalTriageBatch]:
    """
    Triage, through the same batches, the findings of a finished, locally triaged pipeline that still need it.

    The pipeline keeps its status. Raises ``RetriageRefusedError`` when the pipeline cannot be retriaged now.
    """
    pipeline = AISTPipeline.objects.select_related("project").filter(id=pipeline_id).first()
    if pipeline is None:
        msg = f"Pipeline {pipeline_id} does not exist."
        raise RetriageRefusedError(msg)
    if not is_terminal_pipeline_status(pipeline.status):
        msg = f"Pipeline {pipeline_id} is {pipeline.status}; its own run triages its findings."
        raise RetriageRefusedError(msg)
    if resolve_triage_type(pipeline) != AITriageType.LOCAL:
        msg = f"Pipeline {pipeline_id} is not triaged locally."
        raise RetriageRefusedError(msg)
    try:
        batches = plan_local_triage(pipeline.id, triage_candidates(pipeline).values("id"))
    except LocalTriageInProgressError as exc:
        msg = f"Pipeline {pipeline_id} already has triage batches pending or running."
        raise RetriageRefusedError(msg) from exc
    # Checked after planning: the open batches now keep workspace cleanup away from these sources.
    if batches and not (pipeline.source_root and Path(pipeline.source_root).is_dir()):
        cancel_open_batches(pipeline.id)
        msg = f"The sources of pipeline {pipeline_id} are not at {pipeline.source_root!r} in this container."
        raise RetriageRefusedError(msg)
    if batches:
        start_local_triage(pipeline.id, batches)
    return batches


@shared_task(name="aist.triage_local_batch", acks_late=True)
def triage_local_batch(pipeline_id: str, batch_id: int, log_level: str = "INFO", async_user_id=None) -> None:
    """
    Triage one open batch in one bridge session that this task waits for.

    The message is acknowledged only when the task ends, so a batch whose worker is lost is triaged again once
    the broker redelivers it, after the abandoned session's timeout. A batch that may not start, or that a stop or
    another delivery of this task ended meanwhile, ends this copy of the chain (``Ignore``): the batches after it
    are left to the stop or to the copy that ran them.
    """
    with scoped_pipeline_logging(pipeline_id, log_level) as log:
        batch = start_batch(pipeline_id, batch_id)
        if batch is None:
            log.info("Local triage batch id %s is not open; the rest of its chain is dropped", batch_id)
            raise Ignore
        try:
            _run_triage_session(pipeline_id, batch, log)
        except Exception:
            # The batch still completes, so the chain reaches finish_local_triage, which reports its findings.
            log.exception("Local triage batch %s failed (pipeline_id=%s)", batch.sequence, pipeline_id)
        ended = end_batch(pipeline_id, batch.pk)
        if ended is None or ended.state != LocalTriageBatchState.COMPLETED:
            log.info("Local triage batch %s was stopped or ended elsewhere while it ran", batch.sequence)
            raise Ignore
        untriaged = unanswered_finding_ids(pipeline_id, batch.finding_ids)
        if untriaged:
            log.error(
                "Local triage batch %s left %s findings without an AI response: %s",
                batch.sequence, len(untriaged), untriaged,
            )


def _run_triage_session(pipeline_id: str, batch: LocalTriageBatch, log) -> None:
    pipeline = AISTPipeline.objects.select_related("project").get(id=pipeline_id)
    finding_ids = unanswered_finding_ids(pipeline_id, batch.finding_ids)
    if not finding_ids:
        log.info("Every finding of local triage batch %s already has an AI response", batch.sequence)
        return
    # The bridge client factory stays agent-agnostic (invariant I4); the Claude-specific env-var
    # mapping lives in aist/integrations/claude.py (invariant I1).
    claude_auth = claude_subprocess_env(pipeline.project)
    if not claude_auth:
        log.warning("No active Claude integration for pipeline %s; local triage batch %s not sent", pipeline_id, batch.sequence)
        return
    # The skill writes this marker as its last action so the bridge sees completion without waiting for
    # AIST_LOCAL_TRIAGE_TIMEOUT; one directory per batch keeps an earlier marker from ending a later session.
    output_dir = Path(settings.AIST_TRIAGE_OUTPUT_DIR) / str(pipeline_id) / f"batch-{batch.pk}"
    prepare_bridge_output_dir(output_dir)
    (output_dir / TRIAGE_RESULT_FILENAME).unlink(missing_ok=True)
    log.info("Local triage batch %s (%s findings) sent to the bridge", batch.sequence, len(finding_ids))
    result = build_bridge_client_from_settings(auth_env=claude_auth).analyze_sync(
        skill_name="aist-finding-triage",
        project_id=str(pipeline_id),
        source_path=pipeline.source_root,
        extra_args=format_extra_args({
            "output_path": output_dir,
            "result_filename": TRIAGE_RESULT_FILENAME,
            "finding_ids": ",".join(str(finding_id) for finding_id in finding_ids),
        }),
    )
    log.info(
        "Local triage batch %s session ended: %s%s",
        batch.sequence, result.status, f" ({result.detail[:500]})" if result.detail else "",
    )


@shared_task(name="aist.finish_local_triage")
def finish_local_triage(pipeline_id: str, log_level: str = "INFO", async_user_id=None) -> None:
    """
    Finish a pipeline waiting for its local triage, degraded when a finding of its batches has no AI response.

    A finished pipeline being retriaged, or one stopped meanwhile, keeps its status; the outcome is only logged.
    """
    with scoped_pipeline_logging(pipeline_id, log_level) as log:
        untriaged = untriaged_finding_ids(pipeline_id)
        status = AISTPipeline.objects.filter(pk=pipeline_id).values_list("status", flat=True).first()
        if status != AISTStatus.WAITING_RESULT_FROM_AI:
            log.info(
                "Local triage of pipeline %s (%s) ended; %s findings have no AI response",
                pipeline_id, status, len(untriaged),
            )
            return
        if untriaged:
            log.error("Local triage finished with %s findings without an AI response: %s", len(untriaged), untriaged)
        finish_pipeline(pipeline_id, degraded=bool(untriaged))
