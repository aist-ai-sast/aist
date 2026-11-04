"""Standalone DAST pipeline outcome classification and durable public narrative."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from aist.execution.contracts import DastPipelineOutcomeCode
from aist.launch_data import PipelineLaunchData
from aist.models import AISTPipeline, PipelineExecutionType
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.contracts import (  # type: ignore[import-not-found]  # noqa: E402
    DastConnectorOutcomeState,
    DastRunState,
)


@dataclass(frozen=True, slots=True)
class ClassifiedDastOutcome:
    code: DastPipelineOutcomeCode
    degraded: bool


_PROVIDER_REASON_CODES = {
    "NO_ELIGIBLE_STAND": DastPipelineOutcomeCode.POLICY_NO_ELIGIBLE_STAND,
    "SOURCE_DRIFT": DastPipelineOutcomeCode.SOURCE_DRIFT,
    "REPORT_MISSING": DastPipelineOutcomeCode.INVALID_RESULT,
    "REPORT_INVALID": DastPipelineOutcomeCode.INVALID_RESULT,
    "AUDIT_INCOMPLETE": DastPipelineOutcomeCode.INVALID_RESULT,
    "PROVIDER_CREDENTIALS_EXPIRED": DastPipelineOutcomeCode.PROVIDER_CREDENTIALS_EXPIRED,
}

_DEGRADED_STATUS_CODES = {
    DastRunState.COMPLETED_WITH_DEGRADATION: DastPipelineOutcomeCode.COMPLETED_WITH_DEGRADATION,
    DastRunState.FAILED_WITH_PARTIAL_RESULTS: DastPipelineOutcomeCode.FAILED_WITH_PARTIAL_RESULTS,
    DastRunState.STOPPED: DastPipelineOutcomeCode.CANCELLED,
}


def classify_dast_execution_result(result) -> ClassifiedDastOutcome | None:
    """
    Map a typed connector result without exposing provider-controlled reason text.

    Returns None while the provider run is not settled, which the caller retries.
    """
    state = result.outcome.state
    provider_reason = getattr(result.outcome, "reason_code", None)
    if state in {DastConnectorOutcomeState.STOP_PENDING, DastConnectorOutcomeState.UNREACHABLE}:
        return None
    if state is DastConnectorOutcomeState.CANCELLED_BEFORE_START:
        return ClassifiedDastOutcome(code=DastPipelineOutcomeCode.CANCELLED, degraded=True)

    terminal_result = result.terminal_result
    if state is not DastConnectorOutcomeState.TERMINAL or terminal_result is None:
        return ClassifiedDastOutcome(code=DastPipelineOutcomeCode.INVALID_RESULT, degraded=True)
    terminal_status = terminal_result.status
    if terminal_status in _DEGRADED_STATUS_CODES:
        return ClassifiedDastOutcome(code=_DEGRADED_STATUS_CODES[terminal_status], degraded=True)
    if terminal_status is DastRunState.SUCCEEDED:
        report = getattr(terminal_result, "report", None)
        findings = report.get("findings") if isinstance(report, dict) else None
        code = (
            DastPipelineOutcomeCode.SUCCESS_WITH_FINDINGS
            if isinstance(findings, list) and findings
            else DastPipelineOutcomeCode.SUCCESS_CLEAN
        )
        return ClassifiedDastOutcome(code=code, degraded=False)
    if terminal_status is DastRunState.FAILED:
        return ClassifiedDastOutcome(
            code=_PROVIDER_REASON_CODES.get(provider_reason, DastPipelineOutcomeCode.PROVIDER_FAILED),
            degraded=True,
        )
    return ClassifiedDastOutcome(code=DastPipelineOutcomeCode.INVALID_RESULT, degraded=True)


def record_dast_pipeline_outcome(pipeline_id: str, code: DastPipelineOutcomeCode) -> None:
    """Persist one normalized outcome on exactly one standalone DAST pipeline."""
    code = DastPipelineOutcomeCode(code)
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(pk=pipeline_id)
        if pipeline.execution_type != PipelineExecutionType.DAST:
            msg = "DAST outcomes can be recorded only on standalone DAST pipelines."
            raise ValueError(msg)
        launch_data = PipelineLaunchData(pipeline.launch_data)
        launch_data.merge({
            "dast_outcome": {
                "version": "1",
                "code": code.value,
            },
        })
        pipeline.launch_data = launch_data.as_dict()
        pipeline.save(update_fields=["launch_data", "updated"])


def public_dast_outcome_code(pipeline: AISTPipeline) -> str | None:
    """Return a validated public code from persisted pipeline data."""
    if pipeline.execution_type != PipelineExecutionType.DAST:
        return None
    marker = (pipeline.launch_data or {}).get("dast_outcome")
    if not isinstance(marker, dict) or set(marker) != {"version", "code"} or marker.get("version") != "1":
        return None
    try:
        return DastPipelineOutcomeCode(marker.get("code")).value
    except (TypeError, ValueError):
        return None
