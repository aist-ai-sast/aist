from __future__ import annotations

import logging
from dataclasses import dataclass

from prometheus_client import Counter, Histogram

_logger = logging.getLogger("aist.execution.audit")

_EXECUTION_TYPES = frozenset({"sast", "dast", "all"})
_QUEUE_EVENTS = frozenset({"claimed", "coalesced", "expired", "capacity_wait"})
_PROVIDER_OPERATIONS = frozenset({"ping", "catalog", "execute", "reconcile"})
_DAST_OUTCOMES = frozenset({
    "SUCCESS_WITH_FINDINGS",
    "SUCCESS_CLEAN",
    "POLICY_NO_ELIGIBLE_STAND",
    "SOURCE_DRIFT",
    "PROVIDER_FAILED",
    "INVALID_RESULT",
    "CANCELLED",
    "TIMEOUT",
    "pending",
    "error",
})
_RELATIONS = frozenset({"exact", "ancestor", "descendant", "divergent", "none"})
_AUDIT_EVENTS = frozenset({
    "dast_integration_imported",
    "dast_integration_updated",
    "dast_integration_disabled",
    "dast_token_rotated",
    "dast_binding_saved",
    "pipeline_launch_enqueued",
    "dast_cancel_requested",
})
_ALERT_CODES = frozenset({"lease_stale", "validation_failed", "queue_slo_exceeded"})

QUEUE_AGE_SECONDS = Histogram(
    "aist_execution_queue_age_seconds",
    "Age of a durable launch request when claimed.",
    ("execution_type",),
    buckets=(1, 5, 15, 30, 60, 300, 900, 3600, 21600, 86400),
)
QUEUE_EVENTS_TOTAL = Counter(
    "aist_execution_queue_events_total",
    "Bounded durable launch queue transitions.",
    ("execution_type", "event"),
)
LEASE_UTILIZATION_RATIO = Histogram(
    "aist_execution_lease_utilization_ratio",
    "Observed execution resource utilization after a lease decision.",
    ("execution_type", "decision"),
    buckets=(0, 0.25, 0.5, 0.75, 1.0),
)
PROVIDER_SECONDS = Histogram(
    "aist_dast_provider_seconds",
    "DAST provider operation latency observed by AIST.",
    ("operation", "result"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 3600),
)
PROVIDER_ERRORS_TOTAL = Counter(
    "aist_dast_provider_errors_total",
    "DAST provider failures grouped by bounded operation and public error class.",
    ("operation", "error_code"),
)
DAST_OUTCOMES_TOTAL = Counter(
    "aist_dast_outcomes_total",
    "Normalized standalone DAST execution outcomes.",
    ("outcome",),
)
DAST_LOG_CURSOR = Histogram(
    "aist_dast_log_cursor_events",
    "Provider log events delivered through the bounded cursor protocol.",
    buckets=(0, 1, 10, 100, 1000, 10000, 100000),
)
DAST_LOG_LAG_SECONDS = Histogram(
    "aist_dast_log_lag_seconds",
    "Maximum provider log-event delivery lag observed by the connector.",
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 3600),
)
DAST_SELECTION_DISTANCE = Histogram(
    "aist_dast_selection_distance_commits",
    "Selected stand distance from the requested revision.",
    ("relation",),
    buckets=(0, 1, 2, 5, 10, 25, 50, 100, 500, 1000),
)
DAST_FINALIZE_TOTAL = Counter(
    "aist_dast_finalize_total",
    "Strict DAST report finalization results.",
    ("result",),
)


def _bounded(value: str, allowed: frozenset[str], *, fallback: str) -> str:
    normalized = str(value or "")
    return normalized if normalized in allowed else fallback


def observe_queue_claim(*, execution_type: str, age_seconds: float) -> None:
    execution_type = _bounded(execution_type, _EXECUTION_TYPES, fallback="sast")
    age_seconds = max(0.0, float(age_seconds))
    QUEUE_AGE_SECONDS.labels(execution_type=execution_type).observe(age_seconds)
    QUEUE_EVENTS_TOTAL.labels(execution_type=execution_type, event="claimed").inc()
    if age_seconds > 900:
        operational_alert(code="queue_slo_exceeded", execution_type=execution_type, count=1)


def record_queue_event(*, execution_type: str, event: str, amount: int = 1) -> None:
    execution_type = _bounded(execution_type, _EXECUTION_TYPES, fallback="sast")
    event = _bounded(event, _QUEUE_EVENTS, fallback="capacity_wait")
    QUEUE_EVENTS_TOTAL.labels(execution_type=execution_type, event=event).inc(max(0, int(amount)))


def observe_lease_decision(*, execution_type: str, acquired_slot: int | None, capacity: int) -> None:
    execution_type = _bounded(execution_type, _EXECUTION_TYPES, fallback="sast")
    capacity = max(1, int(capacity))
    decision = "busy" if acquired_slot is None else "acquired"
    utilization = 1.0 if acquired_slot is None else min(1.0, (int(acquired_slot) + 1) / capacity)
    LEASE_UTILIZATION_RATIO.labels(execution_type=execution_type, decision=decision).observe(utilization)


def observe_provider_call(*, operation: str, duration_seconds: float, error_code: str = "") -> None:
    operation = _bounded(operation, _PROVIDER_OPERATIONS, fallback="execute")
    result = "error" if error_code else "success"
    PROVIDER_SECONDS.labels(operation=operation, result=result).observe(max(0.0, float(duration_seconds)))
    if error_code:
        error_code = str(error_code)
        safe_code = error_code if error_code.isupper() and len(error_code) <= 64 else "UNKNOWN"
        PROVIDER_ERRORS_TOTAL.labels(operation=operation, error_code=safe_code).inc()


def observe_dast_outcome(
    *,
    outcome: str,
    logs_delivered: int,
    log_lag_seconds: float | None = None,
    relation: str | None = None,
    distance: int | None = None,
) -> None:
    outcome = _bounded(outcome, _DAST_OUTCOMES, fallback="error")
    DAST_OUTCOMES_TOTAL.labels(outcome=outcome).inc()
    delivered = int(logs_delivered) if isinstance(logs_delivered, int) and not isinstance(logs_delivered, bool) else 0
    DAST_LOG_CURSOR.observe(max(0, delivered))
    if isinstance(log_lag_seconds, (int, float)) and not isinstance(log_lag_seconds, bool):
        DAST_LOG_LAG_SECONDS.observe(max(0.0, float(log_lag_seconds)))
    if isinstance(distance, int) and not isinstance(distance, bool):
        relation = _bounded(relation or "none", _RELATIONS, fallback="none")
        DAST_SELECTION_DISTANCE.labels(relation=relation).observe(max(0, distance))


def observe_dast_finalization(*, succeeded: bool) -> None:
    DAST_FINALIZE_TOTAL.labels(result="success" if succeeded else "failure").inc()


@dataclass(frozen=True, slots=True)
class AuditContext:
    organization_id: int | None = None
    project_id: int | None = None
    integration_id: int | None = None
    binding_id: int | None = None
    request_id: int | None = None
    pipeline_id: str | None = None
    actor_id: int | None = None


def audit_event(event: str, *, context: AuditContext) -> None:
    if event not in _AUDIT_EVENTS:
        msg = "Unsupported AIST execution audit event."
        raise ValueError(msg)
    payload = {
        "event": event,
        **{
            field: value
            for field, value in (
                ("organization_id", context.organization_id),
                ("project_id", context.project_id),
                ("integration_id", context.integration_id),
                ("binding_id", context.binding_id),
                ("request_id", context.request_id),
                ("pipeline_id", context.pipeline_id),
                ("actor_id", context.actor_id),
            )
            if value is not None
        },
    }
    _logger.info("AIST execution audit event", extra={"aist_audit_event": payload})


def operational_alert(code: str, *, execution_type: str, count: int) -> None:
    if code not in _ALERT_CODES:
        msg = "Unsupported AIST execution alert code."
        raise ValueError(msg)
    _logger.warning(
        "AIST execution operational alert",
        extra={
            "aist_execution_alert": {
                "code": code,
                "execution_type": _bounded(execution_type, _EXECUTION_TYPES, fallback="sast"),
                "count": max(0, int(count)),
            },
        },
    )
