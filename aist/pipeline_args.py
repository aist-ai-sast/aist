from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

if TYPE_CHECKING:
    from collections.abc import Generator

from aist.integrations.dast_config import DastBindingParameters
from aist.launch_data import LaunchAISettings
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectScript,
    AISTProjectVersion,
    DastProjectBinding,
    PipelineExecutionType,
    VersionType,
)
from aist.profile import ProjectProfile
from aist.services.pipeline_lifecycle import TERMINAL_PIPELINE_STATUSES
from aist.utils.pipeline_imports import _import_sast_pipeline_package, _load_analyzers_config

_import_sast_pipeline_package()

# These imports intentionally come AFTER the call above; suppress E402 explicitly.
from pipeline.config_utils import AnalyzerSelection, TimeClass  # noqa: E402

_logger = logging.getLogger(__name__)

# Error messages (for TRY003/EM101/EM102)
MSG_PROJECT_NOT_FOUND_TPL = "AISTProject with id={} not found"
MSG_DOCKERFILE_NOT_FOUND = "Dockerfile does not exist"
BUILD_DIR_WARNING = "AIST_PROJECTS_BUILD_DIR is not set"

# Launch environment variables the platform computes itself from the project and its SCM
# integration. REPO_URL embeds the integration credential (PAT / HTTP password), so neither
# key may be accepted from a caller nor written to a persisted launch config or a frozen
# launch request snapshot. They are injected for one run only by
# SastPipelineArguments.runtime_env.
SYSTEM_ENV_KEYS = frozenset({"REPO_URL", "PROJECT_NAME"})


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


DEFAULT_LOG_LEVEL = "INFO"


class SastLaunchParamsSerializer(serializers.Serializer):

    """
    The shape of SAST launch params and their defaults; a null param takes its default.

    The AI params are read by :meth:`LaunchAISettings.from_params`.
    """

    rebuild_images = serializers.BooleanField(default=False)
    log_level = serializers.ChoiceField(choices=LOG_LEVELS, default=DEFAULT_LOG_LEVEL)
    analyzers = serializers.ListField(child=serializers.CharField(), default=list)
    selected_languages = serializers.ListField(child=serializers.CharField(), default=list)
    time_class_level = serializers.ChoiceField(choices=list(TimeClass), default=TimeClass.SLOW)
    env = serializers.DictField(default=dict)


def _caller_env(env: dict) -> dict:
    """A launch's environment without the keys the platform computes itself."""
    return {key: value for key, value in env.items() if key not in SYSTEM_ENV_KEYS}


def _resolve_project_version(project: AISTProject, requested: object) -> dict:
    """
    Return the descriptor of the launch's project version, read from the database.

    ``requested`` is a version id, a descriptor with an ``id``, a descriptor without one (kept as given), or
    ``None`` for the project's active version. Raises ``ValueError`` for a version the project does not have.
    """
    if requested is None:
        active = project.active_version
        return active.as_dict() if active else {}
    if isinstance(requested, dict) and not requested.get("id"):
        return dict(requested)
    version_id = requested.get("id") if isinstance(requested, dict) else requested
    if not isinstance(version_id, int) or isinstance(version_id, bool):
        msg = "project_version must be an object (dict) or integer id or null"
        raise ValueError(msg)  # noqa: TRY004 -- an invalid launch param, reported to the caller as such
    version = AISTProjectVersion.objects.filter(pk=version_id, project=project).first()
    if version is None:
        msg = "project_version: no such version in this project"
        raise ValueError(msg)
    return version.as_dict()


def launch_analyzers(
    project: AISTProject | None,
    languages: list[str],
    max_time_class: object,
    requested: list[str] | None = None,
) -> list[str]:
    """The analyzers a SAST launch of ``project`` runs, as sast-pipeline selects them for the project's profile."""
    cfg = _load_analyzers_config()
    if not cfg:
        return list(requested or [])
    profile = ProjectProfile.from_dict(project.profile if project else None).analyzers
    return cfg.select_analyzers(
        AnalyzerSelection(
            languages=tuple(languages),
            max_time_class=TimeClass.of(max_time_class),
            compilable=project.compilable if project else True,
            requested=tuple(requested or ()),
            include=profile.include,
            exclude=profile.exclude,
        ),
    )


def analyzer_selection_signature(project_id: object, languages: list[str], max_time_class: object) -> str:
    """Identifies the inputs of the default :func:`launch_analyzers`, so a form knows when its defaults are stale."""
    return f"{project_id or ''}::{TimeClass.of(max_time_class)}::{','.join(sorted(set(languages or [])))}"


@dataclass
class SastPipelineArguments:
    project: AISTProject
    project_version: dict = field(default_factory=dict)
    selected_analyzers: list[str] = field(default_factory=list)
    selected_languages: list[str] = field(default_factory=list)
    log_level: str = DEFAULT_LOG_LEVEL
    rebuild_images: bool = False
    ai: LaunchAISettings = field(default_factory=LaunchAISettings)
    time_class_level: TimeClass = TimeClass.SLOW
    is_initialized: bool = False
    additional_environments: dict = field(default_factory=dict)

    def __post_init__(self):
        configured_pipeline = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
        self.pipeline_path: Path | None = Path(configured_pipeline) if configured_pipeline else None

    def resolve_effective_project_version(
        self,
        *,
        resolved_commit: str,
    ) -> AISTProjectVersion | None:
        """
        Resolve and persist effective project version for pipeline execution.
        Keeps self.project_version as a single source of truth.
        """
        commit = (resolved_commit or "").strip()
        effective = None
        pv_id = (self.project_version or {}).get("id")
        if pv_id:
            effective = (
                AISTProjectVersion.objects
                .select_for_update()
                .filter(pk=pv_id, project=self.project)
                .first()
            )

        if commit and effective and effective.version_type == VersionType.GIT_BRANCH:
            resolved_version, created = AISTProjectVersion.objects.get_or_create(
                project_id=effective.project_id,
                version=commit,
                version_type=VersionType.GIT_HASH,
                defaults={"resolved_from_branch": effective, "script": effective.script},
            )
            update_fields = []
            if resolved_version.resolved_from_branch_id is None:
                resolved_version.resolved_from_branch = effective
                update_fields.append("resolved_from_branch")
            if not created and resolved_version.script_id is None and effective.script_id:
                resolved_version.script = effective.script
                update_fields.append("script")
            if update_fields:
                resolved_version.save(update_fields=[*update_fields, "updated"])

            effective.last_resolved_commit = commit
            effective.last_resolved_at = timezone.now()
            effective.save(update_fields=["last_resolved_commit", "last_resolved_at", "updated"])
            effective = resolved_version

        if effective is not None:
            self.project_version = effective.as_dict()

        return effective

    @classmethod
    def normalize_project_name(cls, project: AISTProject) -> str:
        if not project.product.name:
            return ""
        return project.product.name.replace(" ", "_").replace("/", "_").lower()

    @classmethod
    def normalize_params(cls, *, project: AISTProject, raw_params: dict) -> dict:
        """
        Admit SAST launch params: validate them, fill their defaults and fix the triage type that handles the
        launch's findings. The result is what :meth:`from_dict` reads.

        Raises ``TypeError`` when the params are not a dict and ``ValueError`` (``AIFilterError`` for the AI
        filter) naming the first invalid param. Keys this serializer does not know are kept unchanged.
        """
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            msg = "params must be a JSON object (dict)"
            raise TypeError(msg)

        serializer = SastLaunchParamsSerializer(data={key: value for key, value in raw_params.items() if value is not None})
        if not serializer.is_valid():
            field_name, errors = next(iter(serializer.errors.items()))
            msg = f"{field_name}: {errors[0]}"
            raise ValueError(msg)
        params = serializer.validated_data

        ai = LaunchAISettings.from_params(raw_params)
        ai = replace(ai, triage_type=ProjectProfile.from_dict(project.profile).triage_type_for(ai))
        ai.check(ai.triage_type)

        return {
            **raw_params,
            **params,
            "project_id": project.id,
            "project_version": _resolve_project_version(project, raw_params.get("project_version")),
            # REPO_URL/PROJECT_NAME are system-computed at launch by runtime_env and never persisted; a
            # caller-supplied value is dropped, not rejected, so a snapshot of an older release stays valid.
            "env": _caller_env(params["env"]),
            **ai.as_params(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SastPipelineArguments:
        """
        Build SAST launch arguments from params :meth:`normalize_params` admitted, as they were stored.

        The params are not validated again: a stored launch runs as it was admitted.
        """
        try:
            project = AISTProject.objects.get(id=data["project_id"])
        except AISTProject.DoesNotExist:
            msg = MSG_PROJECT_NOT_FOUND_TPL.format(data["project_id"])
            raise ValueError(msg)

        return cls(
            project=project,
            project_version=data["project_version"],
            selected_analyzers=data["analyzers"],
            selected_languages=data["selected_languages"],
            log_level=data["log_level"],
            rebuild_images=data["rebuild_images"],
            ai=LaunchAISettings.from_normalized_params(data),
            time_class_level=TimeClass.of(data["time_class_level"]),
            additional_environments=data["env"],
        )

    @property
    def analyzers(self) -> list[str]:
        return launch_analyzers(self.project, self.languages, self.time_class_level, self.selected_analyzers)

    @property
    def languages(self) -> list[str]:
        seen = set()
        out: list[str] = []
        for lang in chain(self.selected_languages or [], self.project.supported_languages or []):
            if lang not in seen:
                seen.add(lang)
                out.append(lang)
        return out

    @property
    def runtime_env(self) -> dict:
        """
        Builder-container environment for one launch.

        The system-computed values are resolved here, at launch time, and never persisted:
        REPO_URL is read from the project's SCM integration on every run, so rotating that
        credential applies to the next launch without rewriting any saved launch config or
        frozen launch request snapshot.
        """
        env = _caller_env(self.additional_environments)
        if self.project.repository:
            env["REPO_URL"] = self.project.repository.clone_url
        env["PROJECT_NAME"] = self.normalize_project_name(self.project)
        return env

    @property
    def project_name(self) -> str:
        return self.project.product.name

    @property
    def output_version(self) -> str:
        return self.project_version.get("version", "default")

    @contextmanager
    def script_path_context(self) -> Generator[str]:
        """
        Write the version's script to a temp file and yield its path.
        The temp file is removed when the context exits.

        The script is ``AISTProjectVersion.effective_script`` — the same rule the
        version-script API shows — so what the UI reports is what runs.
        """
        pv_id = (self.project_version or {}).get("id")
        pv = None
        if pv_id:
            pv = (
                AISTProjectVersion.objects
                .select_related("script")
                .filter(pk=pv_id, project=self.project)
                .first()
            )
        if pv is None or pv.script_id is None:
            _logger.warning(
                "Project version for project %s has no script; falling back to shared default.",
                self.project.id,
            )
        script = pv.effective_script if pv is not None else AISTProjectScript.get_shared_default()
        script_path = self._write_script_to_temp(script)
        try:
            yield script_path
        finally:
            Path(script_path).unlink(missing_ok=True)

    def _write_script_to_temp(self, script) -> str:
        """Write script content to an executable temp file and return its path."""
        with tempfile.NamedTemporaryFile(
            suffix=".sh", mode="w", encoding="utf-8", delete=False,
        ) as tmp:
            tmp.write(script.content)
            path = tmp.name
        Path(path).chmod(0o700)
        return path

    @property
    def pipeline_src_path(self):
        return self.pipeline_path

    @property
    def dockerfile_path(self) -> str:
        dockerfile_path = self.pipeline_path / "Dockerfiles" / "builder" / "Dockerfile"
        if not dockerfile_path.is_file():
            msg = MSG_DOCKERFILE_NOT_FOUND
            raise RuntimeError(msg)
        return str(dockerfile_path)


@dataclass(frozen=True, slots=True)
class DastPipelineArguments:
    project: AISTProject
    binding: DastProjectBinding
    trigger_project_version: AISTProjectVersion | None
    parameters: dict
    capability: dict

    @property
    def output_version(self) -> str:
        if self.trigger_project_version is None:
            return "default"
        return self.trigger_project_version.version

    @classmethod
    def build(
        cls,
        *,
        project: AISTProject,
        binding: DastProjectBinding,
        trigger_project_version: AISTProjectVersion | None,
        raw_parameters: dict,
    ) -> DastPipelineArguments:
        violations = binding.launch_violations(project_id=project.pk, trigger_version=trigger_project_version)
        if violations:
            raise ValueError(violations[0].label)
        if not binding.enabled:
            msg = "DAST binding must be enabled."
            raise ValueError(msg)
        target = binding.target.get_snapshot()
        # The binding is where an operator configured this target, so its saved parameters are
        # what a launch runs unless the launch overrides them explicitly. Validating the raw
        # input alone froze an empty set and silently ran the provider's own defaults instead.
        effective_parameters = dict(binding.parameter_snapshot or {})
        effective_parameters.update(raw_parameters or {})
        parameters = DastBindingParameters.from_snapshot(
            effective_parameters,
            target=target,
        ).to_snapshot()
        return cls(
            project=project,
            binding=binding,
            trigger_project_version=trigger_project_version,
            parameters=parameters,
            capability=target.to_snapshot(),
        )


@dataclass(frozen=True, slots=True)
class PipelineArguments:

    """
    Execution-neutral, in-memory launch envelope.

    Exactly one discriminated payload is present. Saved presets and ephemeral
    launches both become this value before the durable request is written.
    """

    project: AISTProject
    payload: SastPipelineArguments | DastPipelineArguments

    def __post_init__(self) -> None:
        if self.project.pk is None or self.payload.project.pk != self.project.pk:
            msg = "Pipeline arguments payload must belong to the launch project."
            raise ValueError(msg)

    @property
    def execution_type(self) -> PipelineExecutionType:
        if isinstance(self.payload, SastPipelineArguments):
            return PipelineExecutionType.SAST
        return PipelineExecutionType.DAST

    def prepare_execution(self, pipeline_id: str) -> tuple[Path, Path]:
        """Prepare the common workspace and durable result root for one pipeline run."""
        workspace = self._workspace_path(pipeline_id)
        self._cleanup_terminal_workspaces(keep_pipeline_id=pipeline_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace, self._output_path(pipeline_id)

    def cleanup_workspace(self, pipeline_id: str) -> None:
        """Remove one pipeline workspace without touching its durable results."""
        if not getattr(settings, "AIST_PROJECTS_BUILD_DIR", None):
            return
        try:
            workspace = self._workspace_path(pipeline_id)
            if workspace.exists():
                shutil.rmtree(workspace)
                _logger.info("Cleaned up pipeline workspace: %s", workspace)
        except Exception:
            _logger.exception("Failed to clean up pipeline workspace (pipeline_id=%s)", pipeline_id)

    def _workspace_path(self, pipeline_id: str) -> Path:
        configured = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
        if not configured:
            raise RuntimeError(BUILD_DIR_WARNING)
        return self._path_within(
            Path(configured),
            self.project.product.name or "project",
            self.payload.output_version or "default",
            "runs",
            pipeline_id,
        )

    def _output_path(self, pipeline_id: str) -> Path:
        configured = getattr(settings, "AIST_OUTPUT_PATH", None)
        root = Path(configured) if configured else Path(tempfile.gettempdir()) / "aist" / "output"
        return self._path_within(
            root,
            self.project.product.name or "project",
            self.payload.output_version or "default",
            pipeline_id,
        )

    @staticmethod
    def _path_within(root: Path, *parts: str) -> Path:
        resolved_root = root.resolve()
        resolved = resolved_root.joinpath(*parts).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            msg = f"Pipeline path escapes configured root: {resolved}"
            raise ValueError(msg)
        return resolved

    def _cleanup_terminal_workspaces(self, *, keep_pipeline_id: str) -> None:
        with transaction.atomic():
            terminal_pipeline_ids = list(
                AISTPipeline.objects.select_for_update(skip_locked=True)
                .filter(project_id=self.project.pk, status__in=TERMINAL_PIPELINE_STATUSES)
                .exclude(id=keep_pipeline_id)
                .values_list("id", flat=True),
            )
        for pipeline_id in terminal_pipeline_ids:
            self.cleanup_workspace(pipeline_id)

    @property
    def sast(self) -> SastPipelineArguments:
        if not isinstance(self.payload, SastPipelineArguments):
            msg = "DAST pipeline arguments do not contain a SAST payload."
            raise TypeError(msg)
        return self.payload

    @property
    def dast(self) -> DastPipelineArguments:
        if not isinstance(self.payload, DastPipelineArguments):
            msg = "SAST pipeline arguments do not contain a DAST payload."
            raise TypeError(msg)
        return self.payload

    @property
    def effective_project_version(self) -> AISTProjectVersion | None:
        if isinstance(self.payload, DastPipelineArguments):
            return self.payload.trigger_project_version
        version_id = (self.payload.project_version or {}).get("id")
        if not version_id:
            return None
        return AISTProjectVersion.objects.filter(pk=version_id, project=self.project).first()

    @property
    def params_snapshot(self) -> dict:
        if isinstance(self.payload, DastPipelineArguments):
            return dict(self.payload.parameters)
        return self.normalize_params(
            project=self.project,
            raw_params={
                "project_version": dict(self.payload.project_version),
                "analyzers": list(self.payload.selected_analyzers),
                "selected_languages": list(self.payload.selected_languages),
                "log_level": self.payload.log_level,
                "rebuild_images": self.payload.rebuild_images,
                **self.payload.ai.as_params(),
                "time_class_level": self.payload.time_class_level,
                "env": dict(self.payload.additional_environments),
            },
        )

    @property
    def capability_snapshot(self) -> dict:
        if isinstance(self.payload, DastPipelineArguments):
            return dict(self.payload.capability)
        return {}

    @classmethod
    def for_sast(cls, *, project: AISTProject, raw_params: dict) -> PipelineArguments:
        """Admit a SAST launch of ``project`` with ``raw_params``."""
        normalized = SastPipelineArguments.normalize_params(project=project, raw_params=raw_params)
        return cls(project=project, payload=SastPipelineArguments.from_dict(normalized))

    @classmethod
    def for_dast(
        cls,
        *,
        project: AISTProject,
        binding: DastProjectBinding,
        trigger_project_version: AISTProjectVersion | None,
        raw_params: dict,
    ) -> PipelineArguments:
        return cls(
            project=project,
            payload=DastPipelineArguments.build(
                project=project,
                binding=binding,
                trigger_project_version=trigger_project_version,
                raw_parameters=raw_params,
            ),
        )

    @classmethod
    def from_launch_config(cls, config: AISTProjectLaunchConfig) -> PipelineArguments:
        if config.execution_type == PipelineExecutionType.SAST:
            return cls.for_sast(project=config.project, raw_params=dict(config.params or {}))
        if config.dast_binding_id is None:
            msg = "DAST launch config requires a binding."
            raise ValueError(msg)
        return cls.for_dast(
            project=config.project,
            binding=config.dast_binding,
            trigger_project_version=config.trigger_project_version,
            raw_params=dict(config.params or {}),
        )

    @classmethod
    def from_dict(cls, data: dict) -> PipelineArguments:
        """The SAST launch whose admitted params ``data`` are, as they were stored."""
        payload = SastPipelineArguments.from_dict(data)
        return cls(project=payload.project, payload=payload)

    @classmethod
    def normalize_project_name(cls, project: AISTProject) -> str:
        return SastPipelineArguments.normalize_project_name(project)

    @classmethod
    def normalize_params(cls, *, project: AISTProject, raw_params: dict) -> dict:
        return SastPipelineArguments.normalize_params(project=project, raw_params=raw_params)
