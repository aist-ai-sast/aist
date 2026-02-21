from __future__ import annotations

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from dojo.models import Finding, Test

from aist.logging_transport import get_redis, install_pipeline_logging
from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, VersionType
from aist.pipeline_args import PipelineArguments
from aist.tasks.enrich import make_enrich_chord
from aist.utils.pipeline import (
    finish_pipeline,
    get_project_build_path,
    is_terminal_pipeline_status,
    set_pipeline_status,
)
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.reconciliation import safe_attach_findings_to_version

# --------------------------------------------------------------------
# Ensure external "pipeline" package is importable before importing it
# --------------------------------------------------------------------
_import_sast_pipeline_package()

from celery.exceptions import Ignore  # noqa: E402
from pipeline.config_utils import AnalyzersConfigHelper  # type: ignore[import-not-found]  # noqa: E402
from pipeline.project_builder import configure_project_run_analyses  # type: ignore[import-not-found]  # noqa: E402

from aist.internal_upload import upload_results_internal  # noqa: E402

# -------------------------
# Error messages/constants
# -------------------------
MSG_PROJECT_BUILD_PATH_NOT_SET = "Project build path for AIST is not setup"


def postprocess_findings(
    pipeline_id,
    finding_ids,
    trim_path,
    test_ids,
    log_level,
    project_version_descriptor,
):
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        set_pipeline_status(pipeline, AISTStatus.FINDING_POSTPROCESSING)

    redis = get_redis()
    redis.hset(f"aist:progress:{pipeline_id}:enrich", mapping={"total": len(finding_ids), "done": 0})

    return make_enrich_chord(
        finding_ids=finding_ids,
        trim_path=trim_path,
        pipeline_id=pipeline_id,
        test_ids=test_ids,
        log_level=log_level,
        project_version_descriptor=project_version_descriptor,
    )


@shared_task(bind=True)
def run_sast_pipeline(self, pipeline_id: str, params: dict, async_user=None) -> None:
    """
    Execute a SAST pipeline asynchronously.

    This task coordinates the SAST pipeline by invoking the configure
    and upload functions provided by the external ``sast-pipeline``
    package. All progress is recorded in the database so that
    connected clients can observe status changes and log output in real time.

    :param pipeline_id: Primary key of the :class:`AISTPipeline` instance.
    :param params: Dictionary of parameters collected from the form.
    """
    log_level = params.get("log_level", "INFO")
    launch_config_id = params.get("launch_config_id")
    logger = install_pipeline_logging(pipeline_id, log_level)
    pipeline = None  # ensure defined for exception path

    try:
        with transaction.atomic():
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .select_related("project")
                .get(id=pipeline_id)
            )

            # protection from secondary launch
            if not is_terminal_pipeline_status(pipeline.status):
                logger.info("Pipeline already in progress; skipping duplicate start.")
                return

            pipeline.started = timezone.now()
            set_pipeline_status(pipeline, AISTStatus.SAST_LAUNCHED, update_fields_extra=["started"])

            params = PipelineArguments.from_dict(params)

        logger.info(f"Project version: {params.project_version}")
        param_project_version_id = (params.project_version or {}).get("id")
        if pipeline and pipeline.project_version_id and param_project_version_id:
            if int(pipeline.project_version_id) != int(param_project_version_id):
                msg = (
                    "Pipeline project_version mismatch: "
                    f"pipeline={pipeline.project_version_id} params={param_project_version_id}"
                )
                raise ValueError(msg)
        if params.project_version and "id" in params.project_version:
            project_version = AISTProjectVersion.objects.get(pk=params.project_version["id"])
            project_version.ensure_extracted()

        analyzers_helper = AnalyzersConfigHelper()
        project_name = params.project_name
        languages = params.languages
        project_version = params.project_version
        project_version_descriptor = params.build_project_version_descriptor()
        output_dir = params.output_dir
        rebuild_images = params.rebuild_images
        analyzers = params.analyzers
        time_class_level = params.time_class_level
        script_path = params.script_path
        dockerfile_path = params.dockerfile_path

        project_build_path = get_project_build_path(project_name or "project",
                                                    params.project_version.get("version", "default"))

        logger.info("Starting configure_project_run_analyses")
        launch_data = configure_project_run_analyses(
            script_path=script_path,
            output_dir=output_dir,
            languages=languages,
            analyzer_config=analyzers_helper,
            dockerfile_path=dockerfile_path,
            context_dir=params.pipeline_src_path,
            image_name=f"project-{project_name}-builder" if project_name else "project-builder",
            project_path=project_build_path,
            force_rebuild=False,
            rebuild_images=rebuild_images,
            version=project_version,
            log_level=log_level,
            min_time_class=time_class_level or "",
            analyzers=analyzers,
            pipeline_id=pipeline_id,
            additional_env=params.additional_environments,
        )

        launch_data["languages"] = languages
        launch_data["ai"] = {
            "mode": getattr(params, "ai_mode", "MANUAL"),
            "filter_snapshot": getattr(params, "ai_filter_snapshot", None),
        }
        if launch_config_id:
            launch_data["launch_config_id"] = launch_config_id

        git_meta = (launch_data or {}).get("git") or {}
        resolved_commit = (git_meta.get("resolved_commit") or "").strip()

        if resolved_commit:
            with transaction.atomic():
                p2 = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
                pipeline_update_fields = ["updated"]

                pv_id = p2.project_version_id
                if pv_id:
                    resolved_version = params.resolve_effective_project_version(
                        resolved_commit=resolved_commit,
                    )
                    if resolved_version and p2.project_version_id != resolved_version.id:
                        p2.project_version = resolved_version
                        pipeline_update_fields.append("project_version")
                    if resolved_version:
                        project_version_descriptor = params.build_project_version_descriptor()

                p2.save(update_fields=pipeline_update_fields)

        with transaction.atomic():
            pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
            pipeline.launch_data = launch_data
            set_pipeline_status(
                pipeline,
                AISTStatus.UPLOADING_RESULTS,
                update_fields_extra=["launch_data"],
            )
        logger.info("Upload step starting")

        repo_path = launch_data.get("project_path", project_build_path)
        trim_path = launch_data.get("trim_path", "")

        results = upload_results_internal(
            output_dir=launch_data.get("output_dir", output_dir),
            analyzers_cfg_path=launch_data.get("tmp_analyzer_config_path"),
            product_name=project_name,
            repo_path=repo_path,
            trim_path=trim_path,
            pipeline_id=pipeline_id,
            log_level=log_level,
        )

        tests: list[Test] = []
        test_ids = []
        for res in results or []:
            tid = getattr(res, "test_id", None)
            if tid:
                test = Test.objects.filter(id=int(tid)).first()
                test_ids.append(tid)
                if test:
                    tests.append(test)

        finding_ids: list[int] = list(
            Finding.objects.filter(test_id__in=test_ids).values_list("id", flat=True),
        )

        test_ids = [t.id for t in tests]
        with transaction.atomic():
            pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
            pipeline.tests.set(tests, clear=True)
            launch_data = pipeline.launch_data or {}
            launch_data["imported_test_ids"] = test_ids
            pipeline.launch_data = launch_data
            pipeline.save(update_fields=["launch_data", "updated"])

        if test_ids and pipeline and pipeline.project_version_id:
            with transaction.atomic():
                pv = AISTProjectVersion.objects.select_for_update().get(id=pipeline.project_version_id)
                valid_finding_ids = list(Finding.objects.filter(id__in=finding_ids).values_list("id", flat=True))
                finding_ids = valid_finding_ids
                if finding_ids:
                    pv_stats = safe_attach_findings_to_version(
                        pv=pv,
                        finding_ids=finding_ids,
                        logger=logger,
                    )
                    pv_stats.log(logger=logger, pv_id=pv.id, label="PV")
                    if pv.version_type == VersionType.GIT_HASH and pv.resolved_from_branch_id:
                        parent = AISTProjectVersion.objects.select_for_update().get(id=pv.resolved_from_branch_id)
                        parent_stats = safe_attach_findings_to_version(
                            pv=parent,
                            finding_ids=finding_ids,
                            logger=logger,
                        )
                        parent_stats.log(logger=logger, pv_id=parent.id, label="Parent PV")

        if not finding_ids:
            with transaction.atomic():
                pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
                logger.info("No findings to enrich; Finishing pipeline")
                finish_pipeline(pipeline)
        else:
            raise self.replace(
                postprocess_findings(
                    pipeline_id, finding_ids, trim_path, test_ids, log_level, project_version_descriptor,
                ),
            )
    except Ignore:
        raise
    except Exception:
        logger.exception("Exception while running SAST pipeline (pipeline_id=%s)", pipeline_id)
        if pipeline is not None:
            try:
                with transaction.atomic():
                    finish_pipeline(pipeline, degraded=True)
            except Exception:
                logger.exception("Failed to mark pipeline as FINISHED after exception.")
        raise
