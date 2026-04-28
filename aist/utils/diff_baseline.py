"""
DB-only baseline helpers for diff-aware agent analyzers.

Computes BASE_COMMIT (L1 in the 3-level fallback chain) by looking up the
previous terminal-success pipeline on the same resolved branch. The other
two fallback levels (14-day window and first-ever commit) require the
cloned repo on disk and are handled by the skill itself.

The runtime env shape is built by ``aist.utils.agent_runtime``; this module
keeps the diff-specific baseline logic and re-exposes ``build_diff_env`` as
a thin wrapper for callers that historically imported from here.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q

from aist.models import AISTPipeline, AISTStatus, VersionType

_TERMINAL_SUCCESS_STATUSES = (AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS)


def _positive_int_setting(name: str) -> int:
    value = getattr(settings, name)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value


def _branch_pv_id_for(pipeline: AISTPipeline) -> int | None:
    """
    Return the parent GIT_BRANCH version id that anchors this pipeline.

    For GIT_BRANCH pipelines that's the version itself; for GIT_HASH
    pipelines it's `resolved_from_branch_id` of the version. FILE_HASH
    has no branch context.
    """
    pv = pipeline.project_version
    if pv is None:
        return None
    if pv.version_type == VersionType.GIT_BRANCH:
        return pv.id
    if pv.version_type == VersionType.GIT_HASH:
        return pv.resolved_from_branch_id
    return None


def get_prior_successful_commit(pipeline: AISTPipeline) -> str | None:
    """
    Return the head commit of the most recent terminal-success pipeline
    on the same project + same resolved branch as ``pipeline``, or ``None``
    if no such pipeline exists.
    """
    branch_pv_id = _branch_pv_id_for(pipeline)
    if branch_pv_id is None:
        return None

    prior = (
        AISTPipeline.objects
        .filter(
            project_id=pipeline.project_id,
            status__in=_TERMINAL_SUCCESS_STATUSES,
        )
        .exclude(id=pipeline.id)
        .filter(
            Q(project_version_id=branch_pv_id)
            | Q(project_version__resolved_from_branch_id=branch_pv_id),
        )
        .select_related("project_version")
        .order_by("-created")
        .first()
    )
    if prior is None or prior.project_version is None:
        return None

    pv = prior.project_version
    commit = pv.last_resolved_commit
    if not commit and pv.version_type == VersionType.GIT_HASH:
        commit = pv.version
    return commit or None


def build_diff_env(pipeline: AISTPipeline) -> dict[str, str]:
    """
    Return the runtime sidecar dict for agent-bridge analyzers.

    Backward-compatible alias for :func:`aist.utils.agent_runtime.build_agent_runtime_env`
    — kept for callers that historically imported this name. The diff skill
    reads only its own keys (``BASE_COMMIT``, ``EXCLUDED_PATHS_JSON``,
    ``CLAUDE_DIFF_MAX_*``); the extra full-scan keys are inert for it.
    """
    # Local import to avoid a module-level cycle: agent_runtime imports
    # _positive_int_setting and get_prior_successful_commit from here.
    from .agent_runtime import build_agent_runtime_env  # noqa: PLC0415
    return build_agent_runtime_env(pipeline)
