"""One SAST execution: accept the start, prepare its runtime, run its analyzers, persist, import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from dojo.models import Finding, Test

from aist.celery_signals import ActionRunSource, ActionRunStatus, record_action_run
from aist.integrations.claude import claude_subprocess_env
from aist.integrations.egress import resolve_source_vpn
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging
from aist.models import (
    AISTPipeline,
    AISTStatus,
    PipelineExecutionType,
    PipelineLaunchRequest,
)
from aist.pipeline_args import PipelineArguments
from aist.profile import ProjectProfile
from aist.services.pipeline_results import attach_findings_and_finish, record_imported_tests
from aist.utils.agent_runtime import build_agent_runtime_env
from aist.utils.analyzer_outcomes import consume_analyzer_outcomes
from aist.utils.bridge_client_factory import build_bridge_client_from_settings
from aist.utils.pipeline import (
    finish_pipeline,
    set_pipeline_status,
)
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.vpn import vpn_sidecar_context

# --------------------------------------------------------------------
# Ensure external "pipeline" package is importable before importing it
# --------------------------------------------------------------------
_import_sast_pipeline_package()

from celery.exceptions import Ignore  # noqa: E402
from pipeline.config_utils import AnalyzersConfigHelper  # type: ignore[import-not-found]  # noqa: E402
from pipeline.execution import execute_pipeline  # type: ignore[import-not-found]  # noqa: E402
from pipeline.sast_execution import SastExecutionInput  # type: ignore[import-not-found]  # noqa: E402

from aist.internal_upload import FindingExclusion, upload_results_internal  # noqa: E402

if TYPE_CHECKING:
    from aist.integrations.resolver import ResolvedIntegration


def run_sast_execution(pipeline_id: str) -> None:
    """Run one SAST pipeline from its launch request; any failure finishes it degraded and is re-raised."""
    request = PipelineLaunchRequest.objects.select_related("requester").get(pipeline_id=pipeline_id)
    params = request.get_snapshots().params_snapshot()
    log_level = params.get("log_level", "INFO")
    run = _SastRun(
        pipeline_id=pipeline_id,
        log_level=log_level,
        logger=install_pipeline_logging(pipeline_id, log_level),
        launch_config_id=request.launch_config_id,
        requester=request.requester,
    )
    try:
        _execute_sast_pipeline(run, params)
    except Ignore:
        raise
    except Exception:
        run.logger.exception("Exception while running SAST pipeline (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        raise


@dataclass(frozen=True, slots=True)
class _SastRun:

    """Who and what one SAST execution runs for, fixed when it starts."""

    pipeline_id: str
    log_level: str
    logger: object
    launch_config_id: int | None
    requester: object


@dataclass(frozen=True, slots=True)
class _SastRuntime:

    """What one SAST execution runs with, prepared before its analyzers start."""

    project_build_path: str
    output_dir: str
    agent_runtime_env: dict
    bridge_client: object
    vpn: ResolvedIntegration | None


def _execute_sast_pipeline(run: _SastRun, params: dict) -> None:
    arguments = _accept_sast_start(run, params)
    if arguments is None:
        return
    pipeline = AISTPipeline.objects.select_related("project").get(id=run.pipeline_id)
    _check_and_extract_version(run, pipeline, arguments)
    runtime = _prepare_sast_runtime(run, pipeline, arguments)
    ld = _run_sast(run, arguments.sast, runtime)
    ld = _persist_sast_launch_data(run, arguments.sast, ld)
    _import_sast_results(run, arguments.sast, ld, runtime)


def _accept_sast_start(run: _SastRun, params: dict) -> PipelineArguments | None:
    """Return the launch's arguments, or None when the pipeline is not the one worker acceptance moved to EXECUTING."""
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=run.pipeline_id)
        # Worker acceptance is the only transition into EXECUTING. Any other
        # state is either a duplicate delivery or an invalid lifecycle entry.
        if pipeline.status != AISTStatus.EXECUTING:
            run.logger.info("Pipeline already in progress; skipping duplicate start.")
            return None
        return PipelineArguments.from_dict(params)


def _check_and_extract_version(run: _SastRun, pipeline: AISTPipeline, arguments: PipelineArguments) -> None:
    version = arguments.effective_project_version
    run.logger.info(f"Project version: {arguments.sast.project_version}")
    if version is None:
        return
    if pipeline.project_version_id and pipeline.project_version_id != version.id:
        msg = f"Pipeline project_version mismatch: pipeline={pipeline.project_version_id} params={version.id}"
        raise ValueError(msg)
    version.ensure_extracted()


def _prepare_sast_runtime(run: _SastRun, pipeline: AISTPipeline, arguments: PipelineArguments) -> _SastRuntime:
    project_build_path, output_dir = arguments.prepare_execution(run.pipeline_id)
    # Claude credentials, when configured for the project's organization, reach the agent bridge as a
    # generic auth_env; the bridge client factory stays integration-agnostic.
    claude_auth = claude_subprocess_env(pipeline.project)
    return _SastRuntime(
        project_build_path=str(project_build_path),
        output_dir=str(output_dir),
        # Per-pipeline config for agent-bridge analyzers; the bridge reads it from a sidecar file, not from the
        # builder container's environment.
        agent_runtime_env=build_agent_runtime_env(pipeline),
        bridge_client=build_bridge_client_from_settings(auth_env=claude_auth),
        vpn=resolve_source_vpn(pipeline.project),
    )


def _run_sast(run: _SastRun, sast, runtime: _SastRuntime) -> PipelineLaunchData:
    pipeline_id = run.pipeline_id
    project_name = sast.project_name
    run.logger.info("Starting configure_project_run_analyses")
    with vpn_sidecar_context(runtime.vpn, execution_id=pipeline_id) as (vpn_container, _vpn_proxy):
        if vpn_container:
            record_action_run(
                pipeline_id,
                key="vpn_start",
                action_type="vpn",
                trigger_status="active",
                source=ActionRunSource.RUNNER,
                status=ActionRunStatus.STARTED,
            )
        with sast.script_path_context() as script_path:
            execution_result = execute_pipeline(
                PipelineExecutionType.SAST,
                SastExecutionInput(
                    execution_id=pipeline_id,
                    runtime_arguments={
                        "script_path": script_path,
                        "output_dir": runtime.output_dir,
                        "languages": sast.languages,
                        "analyzer_config": AnalyzersConfigHelper(),
                        "dockerfile_path": sast.dockerfile_path,
                        "context_dir": sast.pipeline_src_path,
                        "image_name": f"project-{project_name}-builder" if project_name else "project-builder",
                        "project_path": runtime.project_build_path,
                        "force_rebuild": False,
                        "rebuild_images": sast.rebuild_images,
                        "version": sast.project_version,
                        "log_level": run.log_level,
                        "min_time_class": str(sast.time_class_level),
                        "analyzers": sast.analyzers,
                        "pipeline_id": pipeline_id,
                        "additional_env": sast.runtime_env,
                        "network": f"container:{vpn_container}" if vpn_container else None,
                        "bridge_client": runtime.bridge_client,
                        "agent_bridge_runtime_env": runtime.agent_runtime_env,
                    },
                ),
            )
    return PipelineLaunchData(execution_result.launch_data)


def _persist_sast_launch_data(run: _SastRun, sast, ld: PipelineLaunchData) -> PipelineLaunchData:
    ld.languages = sast.languages
    ld.ai = sast.ai
    if run.launch_config_id:
        ld.launch_config_id = run.launch_config_id

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=run.pipeline_id)
        extra_fields: list[str] = ["launch_data"]

        if ld.resolved_commit and pipeline.project_version_id:
            resolved_version = sast.resolve_effective_project_version(
                resolved_commit=ld.resolved_commit,
            )
            if resolved_version and pipeline.project_version_id != resolved_version.id:
                pipeline.project_version = resolved_version
                extra_fields.append("project_version")

        ld.log_level = sast.log_level
        pipeline.launch_data = ld.as_dict()
        set_pipeline_status(pipeline, AISTStatus.UPLOADING_RESULTS, update_fields_extra=extra_fields)
    return ld


def _import_sast_results(run: _SastRun, sast, ld: PipelineLaunchData, runtime: _SastRuntime) -> None:
    pipeline_id, logger = run.pipeline_id, run.logger
    logger.info("Upload step starting")
    results = upload_results_internal(
        output_dir=ld.output_dir or runtime.output_dir,
        analyzers_cfg_path=ld.tmp_analyzer_config_path,
        product=sast.project.product,
        repo_path=ld.project_path or runtime.project_build_path,
        exclusion=FindingExclusion(ProjectProfile.from_dict(sast.project.profile), ld.trim_path),
        logger=logger,
    )

    tests = list(Test.objects.filter(id__in=[result.test_id for result in results if result.test_id]))
    test_ids = [t.id for t in tests]

    finding_ids: list[int] = list(
        Finding.objects.filter(test_id__in=test_ids).values_list("id", flat=True),
    )
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        ld = PipelineLaunchData(pipeline.launch_data)
        record_imported_tests(pipeline, ld, tests)
        pipeline.launch_data = ld.as_dict()
        pipeline.save(update_fields=["launch_data", "updated"])

    consume_analyzer_outcomes(
        pipeline_id=pipeline_id,
        outcomes=ld.analyzer_outcomes,
        import_results=results,
        output_dir=ld.output_dir or runtime.output_dir,
        user=run.requester,
        logger=logger,
    )

    attach_findings_and_finish(
        pipeline_id=pipeline_id,
        project_version_id=pipeline.project_version_id,
        finding_ids=finding_ids,
        log_level=run.log_level,
        logger=logger,
    )
