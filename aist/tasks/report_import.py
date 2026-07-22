"""Import a stored report and attach its results to an AIST pipeline."""
from __future__ import annotations

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import transaction

from aist.internal_upload import ensure_engagement, import_scan_via_default_importer
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging, uninstall_pipeline_file_logging
from aist.models import AISTPipeline, AISTStatus
from aist.tasks.pipeline import attach_findings_and_finish
from aist.utils.pipeline import finish_pipeline, is_terminal_pipeline_status, set_pipeline_status
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.report_import import discard_uploaded_report, resolve_import_version

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import RepoParams  # noqa: E402


class _ReportAlreadyImported(Exception):
    pass


def _resolve_version_and_mark_uploading(
    pipeline_id: str,
    project_id: int,
    commit_hash: str,
    sha256: str,
):
    """Lock the pipeline, resolve its GIT_HASH version, and persist it."""
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update(of=("self",))
            .select_related("project", "project__repository")
            .get(id=pipeline_id)
        )
        project = pipeline.project
        if project.id != project_id:
            msg = f"Pipeline {pipeline_id} project mismatch: expected {project_id}, got {project.id}"
            raise ValueError(msg)
        if not is_terminal_pipeline_status(pipeline.status):
            return None
        launch_data = pipeline.launch_data or {}
        if launch_data.get("source") == "manual_import" and launch_data.get("sha256") == sha256:
            raise _ReportAlreadyImported
        version = resolve_import_version(project, commit_hash)
        pipeline.project_version = version
        set_pipeline_status(pipeline, AISTStatus.UPLOADING_RESULTS, update_fields_extra=["project_version"])
    return project, version


def _import_report_as_test(project, version, scan_type: str, storage_name: str, uploader_id: int):
    repository = project.repository
    repo_params = RepoParams(
        repo_url=repository.clone_url if repository else "",
        branch_tag=None,
        commit_hash=version.version,
        scm_type=(repository.type.lower() if repository else "generic"),
        local_path=None,
    )
    eng_name = f"{scan_type} {version.version[:12]}"
    engagement = ensure_engagement(project.product, eng_name, repo_params, status="In Progress")

    user_model = get_user_model()
    uploader = user_model.objects.filter(id=uploader_id).first()

    return import_scan_via_default_importer(
        engagement=engagement,
        scan_type=scan_type,
        report_path=default_storage.path(storage_name),
        test_title=f"import {scan_type}",
        repo_params=repo_params,
        minimum_severity="Info",
        lead=uploader,
    )


def _record_provenance(
    pipeline_id: str, test_obj, scan_type: str, uploader_id: int, filename: str, sha256: str,
) -> None:
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        pipeline.tests.add(test_obj)
        ld = PipelineLaunchData(pipeline.launch_data)
        ld.merge({
            "source": "manual_import",
            "scan_type": scan_type,
            "uploader_id": uploader_id,
            "filename": filename,
            "sha256": sha256,
            "imported_test_ids": [test_obj.id],
        })
        pipeline.launch_data = ld.as_dict()
        pipeline.save(update_fields=["launch_data"])


@shared_task(bind=True)
def import_report(
    self,
    pipeline_id: str,
    storage_name: str,
    project_id: int,
    uploader_id: int,
    scan_type: str,
    commit_hash: str,
    filename: str,
    sha256: str,
    log_level: str = "INFO",
) -> None:
    logger = install_pipeline_logging(pipeline_id, log_level)
    cleanup_upload = True

    try:
        resolved = _resolve_version_and_mark_uploading(pipeline_id, project_id, commit_hash, sha256)
        if resolved is None:
            cleanup_upload = False
            logger.info("Report import is already active; skipping duplicate delivery.")
            uninstall_pipeline_file_logging(pipeline_id)
            return
        project, version = resolved
        test_obj, findings = _import_report_as_test(project, version, scan_type, storage_name, uploader_id)
        _record_provenance(pipeline_id, test_obj, scan_type, uploader_id, filename, sha256)
        attach_findings_and_finish(
            pipeline_id=pipeline_id,
            project_version_id=version.id,
            finding_ids=[f.id for f in findings],
            log_level=log_level,
            logger=logger,
        )
    except _ReportAlreadyImported:
        logger.info("Report with sha256=%s is already attached to pipeline %s.", sha256, pipeline_id)
        uninstall_pipeline_file_logging(pipeline_id)
    except Exception:
        logger.exception("Exception while importing report (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        raise
    finally:
        if cleanup_upload:
            discard_uploaded_report(storage_name)
