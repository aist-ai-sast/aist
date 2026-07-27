"""Canonical finalization service for validated autonomous DAST reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import partial

from django.db import transaction

from aist.integrations.dast_report import ValidatedDastReport
from aist.internal_upload import ensure_engagement, import_scan_file_via_default_importer
from aist.launch_data import PipelineLaunchData
from aist.models import (
    AISTPipeline,
    AISTProjectVersion,
    AISTStatus,
    DastProjectBinding,
    PipelineExecutionType,
    PipelineLaunchRequest,
    VersionType,
)
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.services.dast_source_versions import resolve_dast_source_version
from aist.services.pipeline_results import (
    attach_findings_to_project_version,
    finish_or_schedule_pipeline_results,
)
from aist.utils.pipeline import set_pipeline_status
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import RepoParams  # type: ignore[import-not-found]  # noqa: E402

_FINALIZATION_MARKER_VERSION = "1"
_FINALIZATION_MARKER_FIELDS = {
    "version",
    "binding_id",
    "run_id",
    "correlation_id",
    "report_sha256",
    "project_version_id",
    "test_id",
    "finding_ids",
}


class DastFinalizationError(ValueError):

    """A validated DAST report cannot be finalized onto the selected pipeline."""


@dataclass(frozen=True, slots=True)
class FinalizeDastResult:
    pipeline_id: str
    project_version_id: int
    test_id: int
    finding_ids: tuple[int, ...]
    already_finalized: bool


def _existing_result(
    *,
    pipeline: AISTPipeline,
    marker: object,
    binding_id: int,
    report: ValidatedDastReport,
    report_sha256: str,
) -> FinalizeDastResult | None:
    if marker is None:
        return None
    if not isinstance(marker, dict) or set(marker) != _FINALIZATION_MARKER_FIELDS:
        msg = "Persisted DAST finalization marker is invalid."
        raise DastFinalizationError(msg)
    expected_identity = {
        "version": _FINALIZATION_MARKER_VERSION,
        "binding_id": binding_id,
        "run_id": report.run_id,
        "correlation_id": report.correlation_id,
        "report_sha256": report_sha256,
    }
    if any(marker.get(key) != value for key, value in expected_identity.items()):
        msg = "Pipeline was already finalized with a different DAST report."
        raise DastFinalizationError(msg)
    project_version_id = marker.get("project_version_id")
    test_id = marker.get("test_id")
    finding_ids = marker.get("finding_ids")
    if (
        isinstance(project_version_id, bool)
        or not isinstance(project_version_id, int)
        or isinstance(test_id, bool)
        or not isinstance(test_id, int)
        or not isinstance(finding_ids, list)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in finding_ids)
    ):
        msg = "Persisted DAST finalization references are invalid."
        raise DastFinalizationError(msg)
    if not AISTProjectVersion.objects.filter(pk=project_version_id, project_id=pipeline.project_id).exists():
        msg = "Persisted DAST finalization project version is missing."
        raise DastFinalizationError(msg)
    if not pipeline.tests.filter(pk=test_id).exists():
        msg = "Persisted DAST finalization test is missing."
        raise DastFinalizationError(msg)
    return FinalizeDastResult(
        pipeline_id=pipeline.id,
        project_version_id=project_version_id,
        test_id=test_id,
        finding_ids=tuple(finding_ids),
        already_finalized=True,
    )


def _verify_pipeline_binding(
    *,
    pipeline: AISTPipeline,
    binding: DastProjectBinding,
    report: ValidatedDastReport,
) -> None:
    if pipeline.project_id != binding.project_id:
        msg = "DAST binding and pipeline must belong to the same project."
        raise DastFinalizationError(msg)
    if pipeline.execution_type not in {PipelineExecutionType.DAST, PipelineExecutionType.MANUAL_IMPORT}:
        msg = "Only DAST and explicit DAST manual-import pipelines can finalize a DAST report."
        raise DastFinalizationError(msg)
    if pipeline.execution_type == PipelineExecutionType.DAST:
        request_binding_id = PipelineLaunchRequest.objects.filter(pipeline_id=pipeline.id).values_list(
            "dast_binding_id",
            flat=True,
        ).first()
        if request_binding_id != binding.pk:
            msg = "DAST pipeline launch binding does not match the finalization binding."
            raise DastFinalizationError(msg)
        if report.correlation_id != pipeline.id:
            msg = "DAST report correlation does not match the autonomous pipeline."
            raise DastFinalizationError(msg)
        if pipeline.external_run_id and pipeline.external_run_id != report.run_id:
            msg = "DAST report run does not match the autonomous pipeline."
            raise DastFinalizationError(msg)


def finalize_dast_report(
    *,
    pipeline_id: str,
    report: ValidatedDastReport,
    binding: DastProjectBinding,
    logger,
    log_level: str = "INFO",
    lead=None,
) -> FinalizeDastResult:
    """Import one validated report exactly once and hand results to shared deduplication."""
    if not isinstance(report, ValidatedDastReport):
        msg = "DAST finalization requires a validated report."
        raise DastFinalizationError(msg)
    if binding.pk is None:
        msg = "DAST finalization requires a persisted binding."
        raise DastFinalizationError(msg)

    report_sha256 = hashlib.sha256(report.canonical_json).hexdigest()
    with transaction.atomic():
        version = resolve_dast_source_version(report, binding)
        persisted_binding = DastProjectBinding.objects.select_for_update().get(pk=binding.pk)
        pipeline = (
            AISTPipeline.objects
            .select_for_update(of=("self",))
            .select_related("project__repository", "trigger_project_version")
            .get(pk=pipeline_id)
        )
        _verify_pipeline_binding(pipeline=pipeline, binding=persisted_binding, report=report)

        launch_data = PipelineLaunchData(pipeline.launch_data)
        existing = _existing_result(
            pipeline=pipeline,
            marker=(pipeline.launch_data or {}).get("dast_finalization"),
            binding_id=persisted_binding.pk,
            report=report,
            report_sha256=report_sha256,
        )
        if existing is not None:
            return existing

        repository = pipeline.project.repository
        branch_tag = None
        if (
            pipeline.trigger_project_version_id
            and pipeline.trigger_project_version.version_type == VersionType.GIT_BRANCH
        ):
            branch_tag = pipeline.trigger_project_version.version
        repo_params = RepoParams(
            repo_url=repository.clone_url if repository else "",
            branch_tag=branch_tag,
            commit_hash=version.version,
            scm_type=repository.type.lower() if repository else "generic",
            local_path=None,
        )
        engagement = ensure_engagement(
            pipeline.project.product,
            f"{DAST_SCAN_TYPE} {version.version[:12]}",
            repo_params,
            status="In Progress",
        )
        test_obj, findings = import_scan_file_via_default_importer(
            engagement=engagement,
            scan_type=DAST_SCAN_TYPE,
            report_file=report.open_report(),
            test_title=f"DAST {report.target_id}",
            repo_params=repo_params,
            minimum_severity="Info",
            lead=lead,
        )
        finding_ids = attach_findings_to_project_version(
            project_version_id=version.pk,
            finding_ids=[finding.pk for finding in findings],
            logger=logger,
        )
        pipeline.tests.add(test_obj)
        launch_data.merge({
            "dast_finalization": {
                "version": _FINALIZATION_MARKER_VERSION,
                "binding_id": persisted_binding.pk,
                "run_id": report.run_id,
                "correlation_id": report.correlation_id,
                "report_sha256": report_sha256,
                "project_version_id": version.pk,
                "test_id": test_obj.pk,
                "finding_ids": finding_ids,
            },
            "imported_test_ids": [test_obj.pk],
        })
        pipeline.project_version = version
        pipeline.launch_data = launch_data.as_dict()
        set_pipeline_status(
            pipeline,
            AISTStatus.UPLOADING_RESULTS,
            update_fields_extra=["project_version", "launch_data"],
        )
        transaction.on_commit(partial(
            finish_or_schedule_pipeline_results,
            pipeline_id=pipeline.id,
            finding_ids=finding_ids,
            log_level=log_level,
            logger=logger,
        ))
        return FinalizeDastResult(
            pipeline_id=pipeline.id,
            project_version_id=version.pk,
            test_id=test_obj.pk,
            finding_ids=tuple(finding_ids),
            already_finalized=False,
        )
