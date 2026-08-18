"""Public read view over the persisted DAST run metadata."""

from __future__ import annotations

from typing import Any

from aist.models import AISTPipeline, DastRunMetadata

# ``thinking`` is deliberately excluded: it is already counted inside ``output``, so adding it
# would double-count. See docs/integrations/dast.md.
_TOKEN_TOTAL_COLUMNS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")
_TOKEN_COUNTER_COLUMNS = (*_TOKEN_TOTAL_COLUMNS, "thinking_tokens")


def _row(pipeline: AISTPipeline) -> DastRunMetadata | None:
    """The one guard for the reverse relation, absent on every pipeline without an accepted report."""
    try:
        return pipeline.dast_run_metadata
    except DastRunMetadata.DoesNotExist:
        return None


def _sum_reported(reported: list[int | None]) -> int | None:
    """
    Sum only when every component was reported.

    A partial sum would read as an authoritative total while being wrong, so an incomplete
    set reports no total at all.
    """
    if any(value is None for value in reported):
        return None
    return sum(reported)


def _beyond_plan(row: DastRunMetadata) -> int | None:
    """How much the run exceeded its own plan by — from the names when present, else the counts."""
    if row.beyond_plan_names is not None:
        return len(row.beyond_plan_names)
    if row.analysed is not None and row.planned is not None:
        return max(row.analysed - row.planned, 0)
    return None


def _duration_seconds(row: DastRunMetadata) -> int | None:
    if row.scan_started is None or row.scan_finished is None:
        return None
    seconds = int((row.scan_finished - row.scan_started).total_seconds())
    return seconds if seconds >= 0 else None


def _buckets_view(buckets: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Each bucket gains the total its segment is sized by, so the UI only divides."""
    if buckets is None:
        return None
    return [
        {**bucket, "total_tokens": _sum_reported([bucket.get(column) for column in _TOKEN_TOTAL_COLUMNS])}
        for bucket in buckets
    ]


def _agents_total(buckets: list[dict[str, Any]] | None) -> int | None:
    if not buckets:
        return None
    return _sum_reported([bucket.get("agents") for bucket in buckets])


def _summary(row: DastRunMetadata) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "run_type": row.run_type,
        "coverage_unit": row.coverage_unit,
        "discovered": row.discovered,
        "reachable": row.reachable,
        "analysed": row.analysed,
        "planned": row.planned,
        "beyond_plan": _beyond_plan(row),
        "total_tokens": _sum_reported([getattr(row, column) for column in _TOKEN_TOTAL_COLUMNS]),
        "model_calls": row.model_calls,
    }


def dast_run_summary(pipeline: AISTPipeline) -> dict[str, Any] | None:
    """Counters only — cheap enough to carry on every row of the pipeline list."""
    row = _row(pipeline)
    return None if row is None else _summary(row)


def reported_dast_run_preview(metadata) -> dict[str, Any] | None:
    """
    The same counters, for a validated report that has not been imported yet.

    Runs the derivations against an unsaved row so the import preview cannot drift from what
    the pipeline list shows once the report is in.
    """
    if metadata is None:
        return None
    return _summary(DastRunMetadata.objects.build_from_report(metadata))


def dast_run_detail(pipeline: AISTPipeline) -> dict[str, Any] | None:
    """Everything the expanded pipeline card renders, including the analysed inventory."""
    row = _row(pipeline)
    if row is None:
        return None
    return {
        **_summary(row),
        "target_id": row.target_id,
        "stand_id": row.stand_id,
        "product_family": row.product_family,
        "tier": row.tier,
        "target_host": row.target_host,
        "scan_started": row.scan_started,
        "scan_finished": row.scan_finished,
        "duration_seconds": _duration_seconds(row),
        "analysed_names": row.analysed_names,
        "beyond_plan_names": row.beyond_plan_names,
        "tokens": {column: getattr(row, column) for column in _TOKEN_COUNTER_COLUMNS},
        "token_by_phase": _buckets_view(row.token_by_phase),
        "token_by_agent_type": _buckets_view(row.token_by_agent_type),
        "agents": _agents_total(row.token_by_agent_type),
        "token_accounting_consistent": row.token_accounting_consistent,
    }
