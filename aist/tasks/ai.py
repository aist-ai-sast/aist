import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from dojo.models import Finding

from aist.ai_filter import apply_ai_filter
from aist.integrations.claude import claude_auth_env
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging
from aist.models import AISTAIResponse, AISTPipeline, AISTStatus
from aist.profile import ProjectProfile
from aist.utils.bridge_client_factory import build_bridge_client_from_settings
from aist.utils.pipeline import (
    finish_pipeline,
    is_terminal_pipeline_status,
    set_pipeline_status,
)
from aist.utils.urls import build_callback_url, build_local_triage_callback_url


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


def _gather_ai_push_payload(pipeline_id: str, log) -> tuple[bool, Any, str, str, str]:
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .select_related("project__product")
            .get(id=pipeline_id)
        )
        if pipeline.status != AISTStatus.PUSH_TO_AI:
            log.error(
                "Unexpected pipeline status %s for AI push (pipeline_id=%s)",
                pipeline.status,
                pipeline_id,
            )
            return False, None, "", "", ""
        project = pipeline.project
        product = getattr(project, "product", None)
        project_name = getattr(product, "name", None) or getattr(project, "project_name", "")
        languages = _csv(PipelineLaunchData(pipeline.launch_data).languages)
        callback_url = build_callback_url(pipeline_id)
        return True, project, project_name, languages, callback_url


@shared_task(bind=True)
def push_request_to_ai(self, pipeline_id: str, finding_ids, filters, log_level="INFO", async_user=None) -> None:
    log = install_pipeline_logging(pipeline_id, log_level)
    webhook_url = getattr(
        settings,
        "AIST_AI_TRIAGE_WEBHOOK_URL",
        "https://flaming.app.n8n.cloud/webhook-test/triage-sast",
    )
    webhook_timeout = getattr(settings, "AIST_AI_TRIAGE_REQUEST_TIMEOUT", 10)
    triage_secret = getattr(settings, "AIST_AI_TRIAGE_SECRET", None)

    # ── 1. Validate state and gather payload data (short transaction, no I/O) ────
    try:
        status_ok, project, project_name, languages, callback_url = _gather_ai_push_payload(pipeline_id, log)
    except AISTPipeline.DoesNotExist:
        log.error("Pipeline not found (pipeline_id=%s)", pipeline_id)
        return

    if not status_ok:
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
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        if is_terminal_pipeline_status(pipeline.status):
            log.warning(
                "Pipeline already in terminal state %s after AI call (pipeline_id=%s); skipping.",
                pipeline.status,
                pipeline_id,
            )
            return
        set_pipeline_status(pipeline, AISTStatus.WAITING_RESULT_FROM_AI)


def _resolve_triage_type(pipeline: AISTPipeline) -> str:
    """
    Resolve the effective triage type for a pipeline.

    Priority: launch_data.ai.triage_type > project.profile.ai_triage.type > "n8n".
    """
    ld = PipelineLaunchData(pipeline.launch_data)
    per_launch = ld.ai_triage_type
    if per_launch in {"n8n", "local"}:
        return per_launch
    profile = ProjectProfile.from_dict(pipeline.project.profile)
    return profile.get_ai_triage_type()


def _resolve_effective_filter(snap: dict | None, triage_type: str) -> dict | None:
    """
    Resolve per-type filter from the snapshot.

    1. If snap has ``per_type[triage_type]`` → use it.
    2. Otherwise → use the root-level filter (backward compat).
    """
    if not snap:
        return None
    per_type = snap.get("per_type")
    if isinstance(per_type, dict) and triage_type in per_type:
        return per_type[triage_type]
    return snap


def _gather_local_triage_payload(pipeline_id: str, log) -> tuple[bool, str, str, dict[str, str]]:
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .select_related("project__product")
            .get(id=pipeline_id)
        )
        if pipeline.status != AISTStatus.PUSH_TO_AI:
            log.error(
                "Unexpected pipeline status %s for local triage push (pipeline_id=%s)",
                pipeline.status,
                pipeline_id,
            )
            return False, "", "", {}
        ld = PipelineLaunchData(pipeline.launch_data)
        product_name = getattr(pipeline.project.product, "name", "") or ""
        source_path = ld.resolve_source_root(product_name)
        callback_url = build_local_triage_callback_url(pipeline_id)
        # Resolve Claude credentials for the pipeline's project. The
        # factory itself stays agent-agnostic (invariant I4) and the
        # Claude-specific env-var mapping lives in
        # aist/integrations/claude.py (invariant I1).
        claude_auth = {
            var: secret.get_secret_value()
            for var, secret in claude_auth_env(pipeline.project).items()
        }
        return True, source_path, callback_url, claude_auth


@shared_task(bind=True)
def push_request_to_local_triage(
    self,
    pipeline_id: str,
    finding_ids: list[int],
    log_level: str = "INFO",
    async_user=None,
) -> None:
    """Send a triage request to the local Claude bridge via Unix domain socket."""
    log = install_pipeline_logging(pipeline_id, log_level)

    # ── 1. Validate state and gather data ──
    try:
        status_ok, source_path, callback_url, claude_auth = _gather_local_triage_payload(pipeline_id, log)
    except AISTPipeline.DoesNotExist:
        log.error("Pipeline not found (pipeline_id=%s)", pipeline_id)
        return

    if not status_ok:
        finish_pipeline(pipeline_id, degraded=True)
        return

    if not claude_auth:
        # Fail-fast — without a Claude credential the bridge can spawn the
        # CLI but it will immediately exit with an auth error. Skip the
        # round-trip and surface a clear pipeline message instead.
        log.warning(
            "No active Claude integration for pipeline %s; skipping local triage",
            pipeline_id,
        )
        finish_pipeline(pipeline_id, degraded=True)
        return

    bridge_client = build_bridge_client_from_settings(auth_env=claude_auth)

    # ── 1b. Prepare result-file directory so the bridge can detect skill
    # completion without waiting for the (3 h) AIST_LOCAL_TRIAGE_TIMEOUT.
    # The skill writes an empty marker file at this path as its last action
    # (see .codex/skills/aist-finding-triage/SKILL.md). Bridge
    # ``_watch_result_file`` polls for it and short-circuits the run.
    # The bridge container runs as an unprivileged user (uid=1000) and
    # celery runs as root, so the directory must be world-writable —
    # same convention as agent_bridge_runner's ``_write_runtime_file``.
    triage_output_dir = Path(
        getattr(settings, "AIST_TRIAGE_OUTPUT_DIR", "/tmp/aist/triage-output"),  # noqa: S108
    ) / str(pipeline_id)
    result_filename = "triage_done.flag"
    try:
        triage_output_dir.mkdir(parents=True, exist_ok=True)
        Path(triage_output_dir).chmod(0o777)
    except OSError as exc:
        log.warning(
            "Could not prepare triage output dir %s: %s — bridge will fall "
            "back to TRIAGE_TIMEOUT for completion detection",
            triage_output_dir, exc,
        )
    extra_args = f"output_path={triage_output_dir} result_filename={result_filename}"

    # ── 2. Bridge call over Unix socket — outside the transaction ──
    log.info("Sending local triage request via bridge_client (UDS)")
    try:
        bridge_client.analyze_async(
            skill_name="aist-finding-triage",
            project_id=str(pipeline_id),
            source_path=source_path,
            callback_url=callback_url,
            extra_args=extra_args,
        )
    except Exception:
        log.exception("Local triage bridge request failed (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        return

    log.info("Local triage request accepted")

    # ── 3. Confirm success ──
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        if is_terminal_pipeline_status(pipeline.status):
            log.warning(
                "Pipeline already in terminal state %s after local triage call (pipeline_id=%s); skipping.",
                pipeline.status,
                pipeline_id,
            )
            return
        set_pipeline_status(pipeline, AISTStatus.WAITING_RESULT_FROM_AI)


def _prepare_auto_push(pipeline_id: str, logger) -> bool | None:
    """
    Lock the pipeline, validate pre-conditions, and schedule the AI push via on_commit.

    Called inside auto_push_to_ai_if_configured. finish_pipeline must be called by
    the caller *outside* any transaction to avoid a deadlock between the AISTPipeline
    lock (held here) and the AISTProjectVersion lock (acquired by reconciliation).

    Returns:
        True  - finish pipeline as FINISHED_WITH_WARNINGS
        False - finish pipeline as FINISHED
        None  - AI task dispatched; no finish needed

    """
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .select_related("project")
            .prefetch_related("tests")
            .get(id=pipeline_id)
        )

        if pipeline.status != AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI:
            logger.error(
                "Unexpected pipeline status %s for auto push (pipeline_id=%s)",
                pipeline.status,
                pipeline_id,
            )
            return True  # degraded

        triage_type = _resolve_triage_type(pipeline)
        snap = PipelineLaunchData(pipeline.launch_data).ai.get("filter_snapshot")
        effective_filter = _resolve_effective_filter(snap, triage_type)

        # Findings already triaged by analyzer-produced AI artifacts carry
        # pre-judged TP/FP verdicts. Re-running them through post-import triage
        # would either waste budget or overwrite the analyzer verdict.
        qs = Finding.objects.filter(test__in=pipeline.tests.all(), active=True).exclude(
            aist_ai_responses__source_response__source=AISTAIResponse.Source.AGENT_ANALYZER,
        )

        if triage_type == "local":
            # Local triage: apply per-type filter if configured, otherwise take all.
            if effective_filter:
                qs = apply_ai_filter(qs, effective_filter)
                raw_limit = effective_filter.get("limit")
                if raw_limit is not None:
                    try:
                        limit = int(raw_limit)
                    except (TypeError, ValueError):
                        limit = None
                    if limit:
                        qs = qs[:limit]
            finding_ids = list(qs.values_list("id", flat=True))

            if not finding_ids:
                logger.warning("AUTO_DEFAULT (local): 0 active findings (pipeline_id=%s)", pipeline_id)
                return False  # finish ok

            set_pipeline_status(pipeline, AISTStatus.PUSH_TO_AI)
            transaction.on_commit(
                lambda: push_request_to_local_triage.delay(pipeline_id, finding_ids),
            )
            return None  # dispatched

        # n8n triage (default)
        if not effective_filter:
            logger.warning("AUTO_DEFAULT: Filter snapshot not configured (pipeline_id=%s)", pipeline_id)
            return False  # finish ok

        raw_limit = effective_filter.get("limit")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            logger.warning(
                "AUTO_DEFAULT: Invalid filter limit %r (pipeline_id=%s)", raw_limit, pipeline_id,
            )
            return True  # degraded

        qs = apply_ai_filter(qs, effective_filter)
        finding_ids = list(qs.values_list("id", flat=True)[:limit])

        if not finding_ids:
            logger.warning("AUTO_DEFAULT: filter matched 0 findings (pipeline_id=%s)", pipeline_id)
            return False  # finish ok

        set_pipeline_status(pipeline, AISTStatus.PUSH_TO_AI)
        transaction.on_commit(
            lambda: push_request_to_ai.delay(pipeline_id, finding_ids, {"filter": effective_filter}),
        )
        return None  # dispatched


@shared_task(name="aist.auto_push_to_ai_if_configured")
def auto_push_to_ai_if_configured(pipeline_id: str, async_user=None):
    logger = install_pipeline_logging(pipeline_id)
    degraded = _prepare_auto_push(pipeline_id, logger)
    if degraded is not None:
        finish_pipeline(pipeline_id, degraded=degraded)
