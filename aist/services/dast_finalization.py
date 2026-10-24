"""Canonical finalization service for validated autonomous DAST reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import partial

from django.db import transaction
from dojo.celery_dispatch import dojo_dispatch_task
from dojo.finding import helper as finding_helper

from aist.authz import Action
from aist.dedupe.canonical import DAST_SCAN_TYPE
from aist.integrations.dast_report import ValidatedDastReport
from aist.internal_upload import ensure_engagement, import_scan_file_via_default_importer
from aist.launch_data import PipelineLaunchData
from aist.models import (
    SOURCELESS_VERSION_TYPES,
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    DastProjectBinding,
    DastRunMetadata,
    DastTestBinding,
    PipelineExecutionType,
    VersionType,
)
from aist.services.dast_import_receipt import DastImportReceipt
from aist.services.dast_report_routing import check_dast_route_permissions, resolve_dast_report_routes
from aist.services.dast_result_versions import resolve_dast_result_version
from aist.services.pipeline_lifecycle import transition_pipeline_status
from aist.services.pipeline_results import (
    attach_findings_to_project_version,
    finish_or_schedule_pipeline_results,
)
from aist.utils.pipeline import set_pipeline_status
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import RepoParams  # type: ignore[import-not-found]  # noqa: E402


class DastFinalizationError(ValueError):

    """A validated DAST report cannot be finalized onto the selected pipeline."""


@dataclass(frozen=True, slots=True)
class FinalizedDastSection:
    repository: str | None
    binding_id: int | None
    project_version_id: int
    test_id: int
    finding_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FinalizeDastResult:
    pipeline_id: str
    project_version_id: int
    test_id: int
    finding_ids: tuple[int, ...]
    already_finalized: bool
    sections: tuple[FinalizedDastSection, ...] = ()


def _existing_result(
    *,
    pipeline: AISTPipeline,
    marker: object,
    binding_id: int,
    report: ValidatedDastReport,
    report_sha256: str,
    correlation_id: str | None,
) -> FinalizeDastResult | None:
    if marker is None:
        return None
    try:
        receipt = DastImportReceipt.parse(marker)
    except ValueError as exc:
        raise DastFinalizationError(str(exc)) from exc
    expected = DastImportReceipt(binding_id, report.run_id, correlation_id, report_sha256)
    if receipt.as_dict() != expected.as_dict():
        msg = "Pipeline was already finalized with a different DAST report."
        raise DastFinalizationError(msg)
    if receipt.legacy_result is not None:
        legacy = receipt.legacy_result
        sections = (FinalizedDastSection(None, binding_id, legacy.project_version_id, legacy.test_id, legacy.finding_ids),)
        project_version_id = legacy.project_version_id
    else:
        destinations = DastTestBinding.objects.filter(test__aist_pipelines=pipeline).select_related(
            "test__engagement", "project_version__project",
        ).prefetch_related("test__finding_set").order_by("test_id")
        sections = tuple(FinalizedDastSection(
            repository=destination.repository,
            binding_id=destination.binding_id,
            project_version_id=destination.project_version_id,
            test_id=destination.test_id,
            finding_ids=tuple(sorted(finding.pk for finding in destination.test.finding_set.all())),
        ) for destination in destinations)
        project_version_id = pipeline.project_version_id
        if tuple(section.repository for section in sections) != tuple(section.repository for section in report.sections):
            msg = "Persisted DAST finalization sections are missing or inconsistent."
            raise DastFinalizationError(msg)
    if not sections or not AISTProjectVersion.objects.filter(
        pk=project_version_id, project_id=pipeline.project_id,
    ).exists():
        msg = "Persisted DAST finalization project version is missing."
        raise DastFinalizationError(msg)
    for section in sections:
        version = AISTProjectVersion.objects.filter(
            pk=section.project_version_id,
            project__product__prod_type_id=pipeline.project.product.prod_type_id,
        ).select_related("project").first()
        if version is None or not pipeline.tests.filter(
            pk=section.test_id, engagement__product_id=version.project.product_id,
        ).exists():
            msg = "Persisted DAST finalization destination is missing."
            raise DastFinalizationError(msg)
    return FinalizeDastResult(
        pipeline_id=pipeline.id,
        project_version_id=project_version_id,
        test_id=sections[0].test_id,
        finding_ids=tuple(finding_id for section in sections for finding_id in section.finding_ids),
        already_finalized=True,
        sections=sections,
    )


def _verify_pipeline_binding(
    *,
    pipeline: AISTPipeline,
    binding: DastProjectBinding,
    report_run_id: str,
) -> None:
    if pipeline.project_id != binding.project_id:
        msg = "DAST binding and pipeline must belong to the same project."
        raise DastFinalizationError(msg)
    if pipeline.execution_type not in {PipelineExecutionType.DAST, PipelineExecutionType.MANUAL_IMPORT}:
        msg = "Only DAST and explicit DAST manual-import pipelines can finalize a DAST report."
        raise DastFinalizationError(msg)
    if pipeline.dast_binding_id != binding.pk:
        msg = "DAST pipeline binding does not match the finalization binding."
        raise DastFinalizationError(msg)
    if pipeline.execution_type == PipelineExecutionType.DAST:
        execution_state = getattr(pipeline, "dast_execution_state", None)
        if execution_state is None or execution_state.run_id != report_run_id:
            msg = "DAST report run id does not match the provider run recorded for the pipeline."
            raise DastFinalizationError(msg)
        launch_request = getattr(pipeline, "launch_request", None)
        request_binding_id = getattr(launch_request, "dast_binding_id", None)
        if request_binding_id != pipeline.dast_binding_id:
            msg = "DAST pipeline launch binding does not match the finalization binding."
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
        # Lock the routing snapshot and then its projects in stable order. Do not lock the
        # target first: binding-update triggers acquire their target lock after the binding.
        locked_bindings = list(DastProjectBinding.objects.select_for_update(of=("self",)).filter(
            target_id=binding.target_id,
        ).order_by("pk"))
        list(AISTProject.objects.select_for_update().filter(
            pk__in=[item.project_id for item in locked_bindings],
        ).order_by("pk"))
        # Read project data after acquiring its lock, so a concurrent repository edit cannot
        # leave the importer using a stale repository while version resolution sees the new one.
        locked_bindings = list(DastProjectBinding.objects.filter(
            pk__in=[item.pk for item in locked_bindings],
        ).select_related("project__product", "project__repository", "target__integration__organization"))
        persisted_binding = next((item for item in locked_bindings if item.pk == binding.pk), None)
        if persisted_binding is None:
            msg = "The DAST launch binding changed while its report was being finalized."
            raise DastFinalizationError(msg)
        pipeline = (
            AISTPipeline.objects
            .select_for_update(of=("self",))
            .select_related(
                "project__repository",
                "trigger_project_version",
                "dast_execution_state",
                "launch_request",
            )
            .get(pk=pipeline_id)
        )
        _verify_pipeline_binding(
            pipeline=pipeline,
            binding=persisted_binding,
            report_run_id=report.run_id,
        )
        correlation_id = pipeline.id if pipeline.execution_type == PipelineExecutionType.DAST else None

        # Ahead of the already-finalized short circuit below, so redelivering a report rewrites
        # identical metadata and a pipeline finalized before this table existed gains its row.
        if report.run_metadata is not None:
            DastRunMetadata.objects.upsert_from_report(
                pipeline_id=pipeline.pk,
                report=report,
            )

        launch_data = PipelineLaunchData(pipeline.launch_data)
        existing = _existing_result(
            pipeline=pipeline,
            marker=(pipeline.launch_data or {}).get("dast_finalization"),
            binding_id=persisted_binding.pk,
            report=report,
            report_sha256=report_sha256,
            correlation_id=correlation_id,
        )
        if existing is not None:
            return existing
        if pipeline.status == AISTStatus.ADMITTED:
            pipeline = transition_pipeline_status(pipeline.pk, AISTStatus.EXECUTING).pipeline

        routes = resolve_dast_report_routes(report, persisted_binding, bindings=locked_bindings)
        check_dast_route_permissions(
            routes, launch_binding=persisted_binding, user=lead, action=Action.PROJECT_OPERATE,
        )
        version = resolve_dast_result_version(report, persisted_binding)
        versions = {persisted_binding.pk: version}
        sections = []
        for route, envelope in zip(routes, report.open_sections(), strict=True):
            destination = route.binding
            if destination.pk not in versions:
                versions[destination.pk] = resolve_dast_result_version(report, destination)
            destination_version = versions[destination.pk]
            sections.append(_import_section(
                pipeline=pipeline, report=report, route=route, version=destination_version,
                logger=logger, lead=lead, envelope=envelope,
            ))
        finding_ids = [finding_id for section in sections for finding_id in section.finding_ids]
        launch_data.merge({
            "dast_finalization": DastImportReceipt(
                binding_id=persisted_binding.pk,
                run_id=report.run_id,
                correlation_id=correlation_id,
                report_sha256=report_sha256,
            ).as_dict(),
            "imported_test_ids": [section.test_id for section in sections],
        })
        launch_data.finding_postprocessing = False
        pipeline.project_version = version
        pipeline.launch_data = launch_data.as_dict()
        set_pipeline_status(
            pipeline,
            AISTStatus.UPLOADING_RESULTS,
            update_fields_extra=["project_version", "launch_data"],
        )
        # The importer dispatches this itself, but it does so with `apply_async` from inside this
        # still-open transaction: the worker picks the task up in milliseconds, reads finding ids
        # that have not been committed yet, finds nothing and returns silently. Deduplication then
        # never runs, no ProcessedFinding row is ever written, and the pipeline waits out the full
        # AIST_DEDUP_MAX_WAIT_S before being force-released — an hour of looking hung. Dispatching
        # it again once the findings are actually visible is what makes post-processing happen at
        # all; the in-transaction attempt is a harmless no-op on an empty batch.
        transaction.on_commit(partial(
            dojo_dispatch_task,
            finding_helper.post_process_findings_batch,
            finding_ids,
            dedupe_option=True,
            rules_option=True,
            product_grading_option=True,
            issue_updater_option=True,
            push_to_jira=False,
        ))
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
            test_id=sections[0].test_id,
            finding_ids=tuple(finding_ids),
            already_finalized=False,
            sections=tuple(sections),
        )


def _import_section(*, pipeline, report, route, version, logger, lead, envelope) -> FinalizedDastSection:
    repository = route.binding.project.repository
    branch_tag = None
    if (
        route.binding.project_id == pipeline.project_id
        and pipeline.trigger_project_version_id
        and pipeline.trigger_project_version.version_type == VersionType.GIT_BRANCH
    ):
        branch_tag = pipeline.trigger_project_version.version
    repo_params = RepoParams(
        repo_url=repository.clone_url if repository else "",
        branch_tag=branch_tag,
        commit_hash=None if version.version_type in SOURCELESS_VERSION_TYPES else version.version,
        scm_type=repository.type.lower() if repository else "generic",
        local_path=None,
    )
    engagement_label = version.version[:12]
    engagement = ensure_engagement(
        route.binding.project.product,
        f"{DAST_SCAN_TYPE} {engagement_label}",
        repo_params,
        status="In Progress",
    )
    test_obj, findings = import_scan_file_via_default_importer(
        engagement=engagement,
        scan_type=DAST_SCAN_TYPE,
        report_file=envelope,
        test_title=f"DAST {report.target_id}",
        repo_params=repo_params,
        minimum_severity="Info",
        lead=lead,
    )
    finding_ids = sorted(finding.pk for finding in findings)
    attach_findings_to_project_version(
        project_version_id=version.pk,
        finding_ids=finding_ids,
        logger=logger,
    )
    pipeline.tests.add(test_obj)
    DastTestBinding.objects.create(
        test=test_obj, binding=route.binding, project_version=version, repository=route.section.repository,
    )
    return FinalizedDastSection(
        repository=route.section.repository,
        binding_id=route.binding.pk,
        project_version_id=version.pk,
        test_id=test_obj.pk,
        finding_ids=tuple(finding_ids),
    )
