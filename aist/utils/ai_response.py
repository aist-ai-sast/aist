from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.utils import timezone
from dojo.finding.helper import close_finding
from dojo.models import Finding

from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline

_VALID_FIX_TYPES: frozenset[str] = frozenset(
    choice[0] for choice in AISTAIFindingResponse.FixType.choices
)

VERDICT_KEYS: dict[str, tuple[str, ...]] = {
    AISTAIFindingResponse.Verdict.TRUE_POSITIVE: ("true_positives",),
    AISTAIFindingResponse.Verdict.FALSE_POSITIVE: ("false_positives",),
    AISTAIFindingResponse.Verdict.UNCERTAIN: ("uncertainly", "uncertain"),
}


@dataclass(frozen=True)
class SyncAIFindingResponsesResult:
    saved: int
    dropped: int
    deleted: int


@dataclass(frozen=True)
class AiFixData:
    fix_summary: str
    fix_type: str
    diff: str | None
    diff_available: bool
    code_after: str | None
    step_by_step: list[str]
    testing_hint: str | None
    secrets_management: str | None
    suppression_annotation: str | None

    def to_json(self) -> dict:
        return {
            "fixSummary": self.fix_summary,
            "fixType": self.fix_type,
            "diff": self.diff,
            "diffAvailable": self.diff_available,
            "codeAfter": self.code_after,
            "stepByStep": self.step_by_step,
            "testingHint": self.testing_hint,
            "secretsManagement": self.secrets_management,
            "suppressionAnnotation": self.suppression_annotation,
        }


def _needs_false_positive_close(finding: Finding) -> bool:
    return not (finding.false_p and finding.is_mitigated and not finding.active)


def _normalize_optional_str(value: object, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:max_len] if stripped else None


def _normalize_fix(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    fix_type = value.get("fixType")
    if fix_type not in _VALID_FIX_TYPES:
        return None
    fix_summary = _normalize_optional_str(value.get("fixSummary"), max_len=1024)
    if not fix_summary:
        return None
    raw_steps = value.get("stepByStep")
    steps = (
        [s for s in raw_steps if isinstance(s, str)][:20]
        if isinstance(raw_steps, list)
        else []
    )
    return AiFixData(
        fix_summary=fix_summary,
        fix_type=fix_type,
        diff=_normalize_optional_str(value.get("diff"), max_len=20_000),
        diff_available=bool(value.get("diffAvailable")),
        code_after=_normalize_optional_str(value.get("codeAfter"), max_len=20_000),
        step_by_step=steps,
        testing_hint=_normalize_optional_str(value.get("testingHint"), max_len=2_000),
        secrets_management=_normalize_optional_str(value.get("secretsManagement"), max_len=2_000),
        suppression_annotation=_normalize_optional_str(value.get("suppressionAnnotation"), max_len=512),
    ).to_json()


def _normalize_references(value) -> list[str]:
    if not isinstance(value, list):
        return []

    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if not ref:
            continue
        parsed = urlparse(ref)
        if parsed.scheme not in {"http", "https"}:
            continue
        out.append(ref)
    return out


def _extract_entry_finding_id(entry: dict) -> int | None:
    original = entry.get("originalFinding")
    if not isinstance(original, dict):
        return None
    raw_id = original.get("id")
    try:
        finding_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return finding_id if finding_id > 0 else None


def iter_ai_payload_entries(payload: dict) -> list[tuple[str, dict]]:
    results = payload.get("results")
    if not isinstance(results, dict):
        return []

    entries: list[tuple[str, dict]] = []
    for verdict, keys in VERDICT_KEYS.items():
        for key in keys:
            items = results.get(key)
            if not isinstance(items, list):
                continue
            entries.extend(
                (verdict, item) for item in items if isinstance(item, dict)
            )
    return entries


def sync_ai_finding_responses(
    *,
    pipeline: AISTPipeline,
    ai_response: AISTAIResponse,
    user=None,
) -> SyncAIFindingResponsesResult:
    payload = ai_response.payload or {}
    parsed_entries = iter_ai_payload_entries(payload)
    if not parsed_entries:
        deleted, _ = pipeline.ai_finding_responses.all().delete()
        return SyncAIFindingResponsesResult(saved=0, dropped=0, deleted=deleted)

    candidate_ids: list[int] = []
    for _, entry in parsed_entries:
        finding_id = _extract_entry_finding_id(entry)
        if finding_id:
            candidate_ids.append(finding_id)

    if not candidate_ids:
        deleted, _ = pipeline.ai_finding_responses.all().delete()
        return SyncAIFindingResponsesResult(saved=0, dropped=len(parsed_entries), deleted=deleted)

    # select_for_update() prevents TOCTOU: between ID validation and the subsequent
    # close_finding/upsert writes, a concurrent transaction could delete or reassign
    # a finding. Callers must invoke this function within transaction.atomic().
    valid_findings = {
        finding.id: finding for finding in Finding.objects.select_for_update().filter(
            id__in=set(candidate_ids),
            test__engagement__product_id=pipeline.project.product_id,
        )
    }
    valid_ids = set(valid_findings.keys())

    seen_finding_ids: set[int] = set()
    saved = 0
    dropped = 0
    for verdict, entry in parsed_entries:
        finding_id = _extract_entry_finding_id(entry)
        if not finding_id or finding_id not in valid_ids:
            dropped += 1
            continue
        if finding_id in seen_finding_ids:
            continue
        seen_finding_ids.add(finding_id)

        if verdict == AISTAIFindingResponse.Verdict.FALSE_POSITIVE and user:
            finding = valid_findings.get(finding_id)
            if finding and _needs_false_positive_close(finding):
                close_finding(
                    finding=finding,
                    user=user,
                    is_mitigated=True,
                    mitigated=timezone.now(),
                    mitigated_by=user,
                    false_p=True,
                    out_of_scope=False,
                    duplicate=False,
                    note_entry=f"AI mitigated: automatically marked as False Positive by pipeline {pipeline.id}.",
                    note_type=None,
                )

        AISTAIFindingResponse.objects.update_or_create(
            pipeline=pipeline,
            finding_id=finding_id,
            defaults={
                "source_response": ai_response,
                "verdict": verdict,
                "title": (entry.get("title") or "")[:512],
                "summary": entry.get("reasoning") or "",
                "references": _normalize_references(entry.get("references")),
                "epss_score": entry.get("epssScore"),
                "impact_score": entry.get("impactScore"),
                "exploitability_score": entry.get("exploitabilityScore"),
                "uncertainty_level": entry.get("uncertaintyLevel"),
                "uncertainty_spread": entry.get("uncertaintySpread"),
                "exploit_code_maturity": (entry.get("exploitCodeMaturity") or "")[:64],
                "fix": _normalize_fix(entry.get("fix")),
            },
        )
        saved += 1

    stale_qs = pipeline.ai_finding_responses.exclude(finding_id__in=seen_finding_ids)
    deleted, _ = stale_qs.delete()
    return SyncAIFindingResponsesResult(saved=saved, dropped=dropped, deleted=deleted)
