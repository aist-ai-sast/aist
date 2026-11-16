import json
from collections.abc import Iterable
from enum import Enum
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet

from aist.ai_filter import AIFilterError, select_by_ai_filter
from aist.launch_data import AITriageType, PipelineLaunchData
from aist.logging_transport import install_pipeline_logging
from aist.models import AISTPipeline, AISTStatus
from aist.services.ai_triage import PushGuard, push_guard, resolve_triage_type, triage_candidates
from aist.services.pipeline_lifecycle import is_terminal_pipeline_status, lock_pipeline
from aist.tasks.local_triage import push_request_to_local_triage
from aist.utils.pipeline import finish_pipeline, send_or_finish_degraded, set_pipeline_status
from aist.utils.urls import build_callback_url


def _csv(items: Iterable[Any]) -> str:
    seen = set()
    result: list[str] = []
    for it in items or []:
        s = str(it).strip()
        if not s:
            continue
        if s not in seen:
            seen.add(s)
            result.append(s)
    return ", ".join(result)


def _gather_ai_push_payload(pipeline_id: str, log) -> tuple[PushGuard, Any, str, str, str]:
    with transaction.atomic():
        pipeline = lock_pipeline(pipeline_id)
        guard = push_guard(pipeline, pipeline_id, AISTStatus.PUSH_TO_AI, log)
        if guard is not PushGuard.READY:
            return guard, None, "", "", ""
        project = pipeline.project
        product = getattr(project, "product", None)
        project_name = getattr(product, "name", None) or getattr(project, "project_name", "")
        languages = _csv(PipelineLaunchData(pipeline.launch_data).languages)
        callback_url = build_callback_url(pipeline_id)
        return PushGuard.READY, project, project_name, languages, callback_url


@shared_task(bind=True)
def push_request_to_ai(self, pipeline_id: str, finding_ids, filters, log_level="INFO", async_user_id=None) -> None:
    log = install_pipeline_logging(pipeline_id, log_level)
    webhook_url = getattr(
        settings,
        "AIST_AI_TRIAGE_WEBHOOK_URL",
        "",
    )
    webhook_timeout = getattr(settings, "AIST_AI_TRIAGE_REQUEST_TIMEOUT", 10)
    triage_secret = getattr(settings, "AIST_AI_TRIAGE_SECRET", None)

    # ── 1. Validate state and gather payload data (short transaction, no I/O) ────
    guard, project, project_name, languages, callback_url = _gather_ai_push_payload(pipeline_id, log)
    if guard is PushGuard.UNEXPECTED_STATUS:
        finish_pipeline(pipeline_id, degraded=True)
        return
    if guard is not PushGuard.READY:
        return

    if not webhook_url:
        log.error("Remote AI triage requires AIST_AI_TRIAGE_WEBHOOK_URL")
        finish_pipeline(pipeline_id, degraded=True)
        return

    # ── 2. HTTP call is OUTSIDE the transaction — no DB locks held during network I/O ──
    payload: dict[str, Any] = {
        "project": {
            "name": project_name,
            "description": getattr(project, "description", "") or "",
            "languages": languages,
            "cwe": getattr(filters, "cwe", "") or "",
            "findings": finding_ids,
        },
        "pipeline_id": str(pipeline_id),
        "callback_url": callback_url,
    }
    headers = {"Content-Type": "application/json"}
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if triage_secret:
        headers["X-AIST-Signature"] = triage_secret

    try:
        log.info("Sending AI triage request: url=%s payload=%s", webhook_url, payload)
        resp = requests.post(webhook_url, data=body_bytes, headers=headers, timeout=webhook_timeout)
        resp.raise_for_status()
    except Exception:
        log.exception("AI triage POST failed (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        return

    log.info("AI triage request accepted: status=%s body=%s", resp.status_code, resp.text[:500])

    # ── 3. Confirm success in a fresh transaction ──────────────────────────────
    with transaction.atomic():
        pipeline = lock_pipeline(pipeline_id)
        if pipeline is None or is_terminal_pipeline_status(pipeline.status):
            log.warning(
                "Pipeline already in terminal state %s after AI call (pipeline_id=%s); skipping.",
                pipeline.status if pipeline else None,
                pipeline_id,
            )
            return
        set_pipeline_status(pipeline, AISTStatus.WAITING_RESULT_FROM_AI)


def confirm_triage_push(pipeline: AISTPipeline, finding_ids: list[int], filters: dict, log) -> None:
    """
    Move a pipeline locked while it waits for confirmation to PUSH_TO_AI, and push ``finding_ids`` after commit.

    A push the broker does not accept finishes the pipeline with warnings.
    """
    set_pipeline_status(pipeline, AISTStatus.PUSH_TO_AI)
    triage_type = resolve_triage_type(pipeline)
    pipeline_id = pipeline.id
    transaction.on_commit(lambda: send_or_finish_degraded(
        pipeline_id, lambda: push_to_triage(triage_type, pipeline_id, finding_ids, filters), log,
    ))


def push_to_triage(triage_type: AITriageType, pipeline_id: str, finding_ids: list[int], filters: dict) -> None:
    """Queue the push of a pipeline's ``finding_ids`` to the triage of ``triage_type``."""
    match triage_type:
        case AITriageType.LOCAL:
            push_request_to_local_triage.delay(pipeline_id, finding_ids)
        case AITriageType.N8N:
            push_request_to_ai.delay(pipeline_id, finding_ids, filters)
        case _:
            msg = f"No triage push for {triage_type!r}"
            raise ValueError(msg)


class AutoPushOutcome(Enum):
    DISPATCHED = "dispatched"
    ALREADY_HANDLED = "already_handled"
    NOTHING_TO_PUSH = "nothing_to_push"
    DEGRADED = "degraded"


def _local_selection(findings: QuerySet, ai_filter: dict | None, pipeline_id: str, logger) -> list[int]:
    """Local triage takes the findings the filter selects, or every finding when there is no filter."""
    finding_ids = list((select_by_ai_filter(findings, ai_filter) if ai_filter else findings).values_list("id", flat=True))
    if not finding_ids:
        logger.warning("AUTO_DEFAULT (local): 0 active findings (pipeline_id=%s)", pipeline_id)
    return finding_ids


def _n8n_selection(findings: QuerySet, ai_filter: dict | None, pipeline_id: str, logger) -> list[int]:
    """n8n triage takes only the findings a filter selects."""
    if not ai_filter:
        logger.warning("AUTO_DEFAULT: Filter snapshot not configured (pipeline_id=%s)", pipeline_id)
        return []
    finding_ids = list(select_by_ai_filter(findings, ai_filter).values_list("id", flat=True))
    if not finding_ids:
        logger.warning("AUTO_DEFAULT: filter matched 0 findings (pipeline_id=%s)", pipeline_id)
    return finding_ids


_AUTO_PUSH_SELECTION = {
    AITriageType.LOCAL: _local_selection,
    AITriageType.N8N: _n8n_selection,
}


def _prepare_auto_push(pipeline_id: str, logger) -> AutoPushOutcome:
    """
    Lock the pipeline and schedule the AI push of its findings via on_commit.

    The caller finishes the pipeline outside this transaction unless the push was dispatched, so the
    AISTPipeline lock held here is never held while reconciliation takes the AISTProjectVersion lock.
    """
    with transaction.atomic():
        pipeline = lock_pipeline(pipeline_id)
        guard = push_guard(pipeline, pipeline_id, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI, logger)
        if guard in {PushGuard.ALREADY_PUSHED, PushGuard.FINISHED, PushGuard.NOT_FOUND}:
            return AutoPushOutcome.ALREADY_HANDLED
        if guard is PushGuard.UNEXPECTED_STATUS:
            return AutoPushOutcome.DEGRADED

        triage_type = resolve_triage_type(pipeline)
        ai_filter = PipelineLaunchData(pipeline.launch_data).ai.filter_snapshot

        try:
            finding_ids = _AUTO_PUSH_SELECTION[triage_type](triage_candidates(pipeline), ai_filter, pipeline_id, logger)
        except AIFilterError as exc:
            logger.warning("AUTO_DEFAULT: invalid AI filter %s (pipeline_id=%s)", exc, pipeline_id)
            return AutoPushOutcome.DEGRADED
        if not finding_ids:
            return AutoPushOutcome.NOTHING_TO_PUSH
        confirm_triage_push(pipeline, finding_ids, {"filter": ai_filter}, logger)
        return AutoPushOutcome.DISPATCHED


@shared_task(name="aist.auto_push_to_ai_if_configured")
def auto_push_to_ai_if_configured(pipeline_id: str, async_user_id=None):
    logger = install_pipeline_logging(pipeline_id)
    outcome = _prepare_auto_push(pipeline_id, logger)
    if outcome in {AutoPushOutcome.NOTHING_TO_PUSH, AutoPushOutcome.DEGRADED}:
        finish_pipeline(pipeline_id, degraded=outcome == AutoPushOutcome.DEGRADED)
