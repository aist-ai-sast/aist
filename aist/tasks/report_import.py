"""Import a stored report and attach its results to an AIST pipeline."""
from __future__ import annotations

import hashlib
import hmac

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import transaction

from aist.integrations.dast_report import validate_dast_report_bytes
from aist.internal_upload import ensure_engagement, import_scan_via_default_importer
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging, uninstall_pipeline_file_logging
from aist.models import AISTPipeline, AISTStatus, DastProjectBinding
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.services.dast_finalization import finalize_dast_report
from aist.services.pipeline_lifecycle import transition_pipeline_status
from aist.tasks.pipeline import attach_findings_and_finish
from aist.utils.pipeline import finish_pipeline, set_pipeline_status
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.report_import import discard_uploaded_report, resolve_import_version

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import RepoParams  # noqa: E402


class _ReportAlreadyImportedError(Exception):
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
        if pipeline.status != AISTStatus.ADMITTED:
            return None
        launch_data = pipeline.launch_data or {}
        if launch_data.get("source") == "manual_import" and launch_data.get("sha256") == sha256:
            raise _ReportAlreadyImportedError
        version = resolve_import_version(project, commit_hash)
        pipeline = transition_pipeline_status(pipeline.pk, AISTStatus.EXECUTING).pipeline
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


def _import_and_finish(
    pipeline_id: str,
    project,
    version,
    scan_type: str,
    storage_name: str,
    uploader_id: int,
    filename: str,
    sha256: str,
    log_level: str,
    logger,
) -> None:
    test_obj, findings = _import_report_as_test(project, version, scan_type, storage_name, uploader_id)
    _record_provenance(pipeline_id, test_obj, scan_type, uploader_id, filename, sha256)
    attach_findings_and_finish(
        pipeline_id=pipeline_id,
        project_version_id=version.id,
        finding_ids=[finding.id for finding in findings],
        log_level=log_level,
        logger=logger,
    )


def _process_report_import(
    pipeline_id: str,
    project_id: int,
    commit_hash: str,
    sha256: str,
    scan_type: str,
    storage_name: str,
    uploader_id: int,
    filename: str,
    log_level: str,
    logger,
) -> bool:
    resolved = _resolve_version_and_mark_uploading(pipeline_id, project_id, commit_hash, sha256)
    if resolved is None:
        logger.info("Report import is already active; skipping duplicate delivery.")
        uninstall_pipeline_file_logging(pipeline_id)
        return False
    project, version = resolved
    _import_and_finish(
        pipeline_id, project, version, scan_type, storage_name, uploader_id, filename, sha256, log_level, logger,
    )
    return True


def _process_dast_report_import(
    *,
    pipeline_id: str,
    project_id: int,
    binding_id: int | None,
    storage_name: str,
    uploader_id: int,
    sha256: str,
    log_level: str,
    logger,
) -> None:
    if binding_id is None:
        msg = "An explicit DAST project binding is required."
        raise ValueError(msg)
    binding = (
        DastProjectBinding.objects
        .select_related("project", "target")
        .get(pk=binding_id, project_id=project_id, enabled=True)
    )
    with default_storage.open(storage_name, "rb") as stored_report:
        raw = stored_report.read(settings.PIPELINE_IMPORT_MAX_SIZE_BYTES + 1)
    if len(raw) > settings.PIPELINE_IMPORT_MAX_SIZE_BYTES:
        msg = "DAST report exceeds its size limit."
        raise ValueError(msg)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), sha256):
        msg = "Persisted DAST report checksum does not match the accepted upload."
        raise ValueError(msg)
    report = validate_dast_report_bytes(
        raw,
        target_id=binding.target.provider_id,
        allowed_repository_keys=frozenset(binding.target.repository_keys),
        maximum_report_bytes=settings.PIPELINE_IMPORT_MAX_SIZE_BYTES,
    )
    uploader = get_user_model().objects.filter(pk=uploader_id).first()
    finalize_dast_report(
        pipeline_id=pipeline_id,
        report=report,
        binding=binding,
        logger=logger,
        log_level=log_level,
        lead=uploader,
    )


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
    binding_id: int | None = None,
    log_level: str = "INFO",
    async_user=None,
) -> None:
    del async_user  # unused; accepted only for the DojoAsyncTask contract
    logger = install_pipeline_logging(pipeline_id, log_level)
    cleanup_upload = True

    try:
        if scan_type == DAST_SCAN_TYPE:
            _process_dast_report_import(
                pipeline_id=pipeline_id,
                project_id=project_id,
                binding_id=binding_id,
                storage_name=storage_name,
                uploader_id=uploader_id,
                sha256=sha256,
                log_level=log_level,
                logger=logger,
            )
        else:
            cleanup_upload = _process_report_import(
                pipeline_id,
                project_id,
                commit_hash,
                sha256,
                scan_type,
                storage_name,
                uploader_id,
                filename,
                log_level,
                logger,
            )
    except _ReportAlreadyImportedError:
        logger.info("Report with sha256=%s is already attached to pipeline %s.", sha256, pipeline_id)
        uninstall_pipeline_file_logging(pipeline_id)
    except Exception:
        logger.exception("Exception while importing report (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        raise
    finally:
        if cleanup_upload:
            discard_uploaded_report(storage_name)
