from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from aist.launch_data import PipelineLaunchData
from aist.models import AISTAIResponse, AISTPipeline
from aist.utils.ai_response_artifact import apply_ai_response_artifact

log = logging.getLogger(__name__)

_AI_RESPONSE_ARTIFACT_FORMAT = "aist_ai_finding_response_v1"


@dataclass(frozen=True)
class ConsumeAnalyzerOutcomesResult:
    degraded_reasons: int
    ai_artifacts_applied: int


def _result_by_analyzer(import_results: list[Any] | None) -> dict[str, Any]:
    return {
        str(result.analyzer_name): result
        for result in (import_results or [])
        if getattr(result, "analyzer_name", "")
    }


def _normalize_degraded_reasons(outcomes: list[dict]) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict) or not outcome.get("degraded"):
            continue
        analyzer_name = str(outcome.get("name") or "unknown")
        messages = outcome.get("messages")
        if not isinstance(messages, list) or not messages:
            messages = [
                {
                    "code": outcome.get("status") or "degraded",
                    "text": "Analyzer reported degraded outcome",
                },
            ]
        for message in messages:
            if not isinstance(message, dict):
                continue
            reasons.append({
                "source": f"analyzer:{analyzer_name}",
                "code": str(message.get("code") or outcome.get("status") or "degraded"),
                "message": str(message.get("text") or ""),
            })
    return reasons


def _ai_response_artifact(outcome: dict) -> dict[str, Any] | None:
    artifacts = outcome.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    artifact = artifacts.get("ai_response")
    if not isinstance(artifact, dict):
        return None
    if artifact.get("format") != _AI_RESPONSE_ARTIFACT_FORMAT:
        return None
    if not artifact.get("path"):
        return None
    return artifact


def consume_analyzer_outcomes(
    *,
    pipeline_id: str,
    outcomes: list[dict],
    import_results: list[Any] | None,
    output_dir: str,
    user=None,
    logger=None,
) -> ConsumeAnalyzerOutcomesResult:
    """
    Persist generic analyzer outcomes and consume supported artifacts.

    This is the AIST boundary for ``sast-pipeline`` analyzer metadata. It does
    not know analyzer names or runner implementations; it only understands the
    normalized outcome schema and supported artifact formats.
    """
    logger = logger or log
    if not outcomes:
        return ConsumeAnalyzerOutcomesResult(degraded_reasons=0, ai_artifacts_applied=0)

    reasons = _normalize_degraded_reasons(outcomes)
    if reasons:
        with transaction.atomic():
            pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
            launch_data = PipelineLaunchData(pipeline.launch_data)
            launch_data.add_analyzer_degraded_reasons(reasons)
            pipeline.launch_data = launch_data.as_dict()
            pipeline.save(update_fields=["launch_data", "updated"])

    results_by_name = _result_by_analyzer(import_results)
    ai_artifacts_applied = 0
    pipeline = AISTPipeline.objects.select_related("project").get(id=pipeline_id)
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        artifact = _ai_response_artifact(outcome)
        if not artifact:
            continue
        analyzer_name = str(outcome.get("name") or "")
        import_result = results_by_name.get(analyzer_name)
        if not import_result or not getattr(import_result, "test_id", None):
            logger.warning(
                "AI response artifact for analyzer=%s has no imported Test; skipping.",
                analyzer_name,
            )
            continue
        try:
            result = apply_ai_response_artifact(
                pipeline=pipeline,
                output_dir=output_dir,
                artifact_path=str(artifact["path"]),
                test_id=int(import_result.test_id),
                match_key=str(artifact.get("match_key") or "unique_id_from_tool"),
                source=AISTAIResponse.Source.AGENT_ANALYZER,
                user=user,
            )
        except Exception:
            logger.exception(
                "Failed to apply AI response artifact for analyzer=%s path=%s; continuing pipeline.",
                analyzer_name,
                artifact.get("path"),
            )
            continue
        if result is not None:
            ai_artifacts_applied += 1

    return ConsumeAnalyzerOutcomesResult(
        degraded_reasons=len(reasons),
        ai_artifacts_applied=ai_artifacts_applied,
    )
