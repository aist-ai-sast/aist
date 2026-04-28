"""
Post-import sync for analyzer-produced AI response artifacts.

Agent analyzers may emit a Generic Findings Import result plus a sibling
AI response artifact. The result file is imported first, creating vendor
``Finding`` rows. This module then resolves artifact entries back to those
findings and delegates to the existing ``sync_ai_finding_responses`` path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db import transaction
from dojo.models import Finding

from aist.models import AISTAIResponse, AISTPipeline
from aist.utils.ai_response import sync_ai_finding_responses

log = logging.getLogger(__name__)

# Verdict bucket keys in the AI response file. Matches the buckets
# `iter_ai_payload_entries` already understands in aist/utils/ai_response.py.
_VERDICT_BUCKETS: tuple[str, ...] = ("true_positives", "false_positives", "uncertainly")
_SUPPORTED_MATCH_KEYS = {"unique_id_from_tool": "uniqueIdFromTool"}


@dataclass(frozen=True)
class ApplyAiResponseArtifactResult:
    saved: int
    dropped: int
    deleted: int


def _load_ai_response_file(output_dir: str, artifact_path: str) -> dict | None:
    base = Path(output_dir).resolve()
    path = (base / artifact_path).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        log.warning("AI response artifact path escapes output_dir: %s", artifact_path)
        return None
    if not path.exists():
        log.info("AI response artifact not found at %s; skipping sync.", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read AI response artifact %s: %s", path, exc)
        return None


def _build_unique_id_index(test_id: int, *, product_id: int) -> dict[str, int]:
    """
    Map ``Finding.unique_id_from_tool -> Finding.id`` for one imported Test.

    Defense-in-depth: filter by both ``test_id`` and the pipeline's
    ``project.product_id`` so a caller passing an arbitrary ``test_id``
    cannot read findings outside the pipeline's product.
    """
    index: dict[str, int] = {}
    for finding_id, uid in (
        Finding.objects
        .filter(test_id=test_id, test__engagement__product_id=product_id)
        .exclude(unique_id_from_tool__isnull=True)
        .exclude(unique_id_from_tool="")
        .values_list("id", "unique_id_from_tool")
    ):
        index[str(uid)] = finding_id
    return index


def _translate_payload(
    raw_payload: dict,
    match_value_to_finding: dict[str, int],
    *,
    artifact_match_key: str,
) -> tuple[dict, int]:
    """
    Inject ``originalFinding.id`` into entries whose artifact key resolves.

    Returns the transformed payload (verdict buckets only) and the count of
    dropped entries (unresolvable id) so the caller can log a single warning.
    """
    raw_results = raw_payload.get("results") or {}
    if not isinstance(raw_results, dict):
        return {"results": {bucket: [] for bucket in _VERDICT_BUCKETS}}, 0

    out: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in _VERDICT_BUCKETS}
    dropped = 0
    for bucket in _VERDICT_BUCKETS:
        raw_entries = raw_results.get(bucket)
        if not isinstance(raw_entries, list):
            continue
        for entry in raw_entries:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            match_value = entry.get(artifact_match_key)
            if not isinstance(match_value, str) or match_value not in match_value_to_finding:
                dropped += 1
                continue
            translated = dict(entry)
            translated["originalFinding"] = {"id": match_value_to_finding[match_value]}
            out[bucket].append(translated)
    return {"results": out}, dropped


def apply_ai_response_artifact(
    *,
    pipeline: AISTPipeline,
    output_dir: str,
    artifact_path: str,
    test_id: int,
    match_key: str = "unique_id_from_tool",
    source: str = AISTAIResponse.Source.AGENT_ANALYZER,
    user=None,
) -> ApplyAiResponseArtifactResult | None:
    """
    Read an AI response artifact and upsert AISTAIFindingResponse rows.

    Returns ``None`` when the file is missing or unreadable (no rows changed).
    Otherwise returns the SyncAIFindingResponsesResult-shaped counts adapted
    to this helper's tuple type.
    """
    artifact_match_key = _SUPPORTED_MATCH_KEYS.get(match_key)
    if not artifact_match_key:
        log.warning("Unsupported AI response artifact match_key=%s; skipping sync.", match_key)
        return None

    raw_payload = _load_ai_response_file(output_dir, artifact_path)
    if raw_payload is None:
        return None

    match_value_to_finding = _build_unique_id_index(
        test_id, product_id=pipeline.project.product_id,
    )
    payload, dropped = _translate_payload(
        raw_payload,
        match_value_to_finding,
        artifact_match_key=artifact_match_key,
    )
    if dropped:
        log.warning(
            "AI response artifact: dropped %d entries with unresolvable %s for pipeline=%s",
            dropped, match_key, pipeline.id,
        )

    with transaction.atomic():
        ai_response = AISTAIResponse.objects.create(
            pipeline=pipeline,
            payload=payload,
            source=source,
        )
        stats = sync_ai_finding_responses(
            pipeline=pipeline,
            ai_response=ai_response,
            user=user,
        )
    return ApplyAiResponseArtifactResult(
        saved=stats.saved,
        dropped=stats.dropped + dropped,
        deleted=stats.deleted,
    )
