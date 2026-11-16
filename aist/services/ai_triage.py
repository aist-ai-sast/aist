"""Which findings of a pipeline go to AI triage, through which backend, and whether a push step may run now."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from dojo.models import Finding

from aist.launch_data import AITriageType, PipelineLaunchData
from aist.models import AISTAIResponse, AISTPipeline
from aist.profile import ProjectProfile
from aist.services.pipeline_lifecycle import is_terminal_pipeline_status, statuses_after

if TYPE_CHECKING:
    from django.db.models import QuerySet


class PushGuard(Enum):
    READY = "ready"
    ALREADY_PUSHED = "already_pushed"
    FINISHED = "finished"
    UNEXPECTED_STATUS = "unexpected_status"
    NOT_FOUND = "not_found"


def push_guard(pipeline: AISTPipeline | None, pipeline_id: str, expected_status: str, log) -> PushGuard:
    """
    Whether a push step that expects ``expected_status`` may run on a pipeline locked by ``lock_pipeline``.

    A finished pipeline takes no push. A pipeline that has not finished but is already past that status in the
    lifecycle graph was pushed before, so a repeated step is a no-op.
    """
    if pipeline is None:
        log.error("Pipeline not found (pipeline_id=%s)", pipeline_id)
        return PushGuard.NOT_FOUND
    if pipeline.status == expected_status:
        return PushGuard.READY
    if is_terminal_pipeline_status(pipeline.status):
        log.info("Pipeline %s has finished (%s); it takes no triage push", pipeline_id, pipeline.status)
        return PushGuard.FINISHED
    if pipeline.status in statuses_after(expected_status):
        log.info("Triage push already handled; pipeline is %s (pipeline_id=%s)", pipeline.status, pipeline_id)
        return PushGuard.ALREADY_PUSHED
    log.error(
        "Unexpected pipeline status %s for AI push expecting %s (pipeline_id=%s)",
        pipeline.status, expected_status, pipeline_id,
    )
    return PushGuard.UNEXPECTED_STATUS


def resolve_triage_type(pipeline: AISTPipeline) -> AITriageType:
    """The triage that handles a pipeline's findings: the launch's own choice, else the project's profile."""
    return ProjectProfile.from_dict(pipeline.project.profile).triage_type_for(PipelineLaunchData(pipeline.launch_data).ai)


def pipeline_findings(pipeline: AISTPipeline) -> QuerySet[Finding]:
    """The findings the pipeline's own tests reported."""
    return Finding.objects.filter(test__in=pipeline.tests.all())


def triage_candidates(pipeline: AISTPipeline) -> QuerySet[Finding]:
    """
    The pipeline's active findings that post-import triage may judge.

    Findings already triaged by analyzer-produced AI artifacts carry pre-judged TP/FP verdicts;
    post-import triage would waste budget on them or overwrite the analyzer verdict.
    """
    return pipeline_findings(pipeline).filter(active=True).exclude(
        aist_ai_responses__source_response__source=AISTAIResponse.Source.AGENT_ANALYZER,
    )
