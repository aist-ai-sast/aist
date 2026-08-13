from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterator

from aist.ai_filter import validate_and_normalize_filter
from aist.integrations.dast_config import DastBindingParameters
from aist.models import (
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectScript,
    AISTProjectVersion,
    DastProjectBinding,
    PipelineExecutionType,
    VersionType,
)
from aist.utils.pipeline_imports import _load_analyzers_config

_logger = logging.getLogger(__name__)

# Error messages (for TRY003/EM101/EM102)
MSG_PROJECT_NOT_FOUND_TPL = "AISTProject with id={} not found"
MSG_DOCKERFILE_NOT_FOUND = "Dockerfile does not exist"


@dataclass
class SastPipelineArguments:
    project: AISTProject
    project_version: dict = field(default_factory=dict)
    selected_analyzers: list[str] = field(default_factory=list)
    selected_languages: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    rebuild_images: bool = False
    ai_mode: str = "MANUAL"  # MANUAL | AUTO_DEFAULT
    ai_triage_type: str | None = None  # None = use project default; "n8n" | "local"
    ai_filter_snapshot: dict | None = None  # resolved effective default at launch time
    time_class_level: str = "slow"  # TODO: change to enum
    is_initialized: bool = False
    additional_environments: dict = field(default_factory=dict)

    def __post_init__(self):
        default_out = Path(tempfile.gettempdir()) / "aist" / "output"
        configured_out = getattr(settings, "AIST_OUTPUT_PATH", None)
        self.aist_path: Path = Path(configured_out) if configured_out else default_out

        configured_pipeline = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
        self.pipeline_path: Path | None = Path(configured_pipeline) if configured_pipeline else None
        self.project_version["excluded_paths"] = self.project.get_excluded_paths()
        self.project_version["excluded_severities"] = self.project.get_excluded_severities()

    def build_project_version_descriptor(self) -> dict:
        """
        Build a runtime descriptor for LinkBuilder/enrich.
        Keeps policy fields (like excluded paths) owned by PipelineArguments.
        """
        base = dict(self.project_version or {})
        base["excluded_paths"] = self.project.get_excluded_paths()
        base["excluded_severities"] = self.project.get_excluded_severities()
        return base

    def enrich_config(self) -> dict:
        """
        Fields to persist in launch_data for the enrichment stage.

        Single source of truth: adding a new enrichment parameter means
        updating only this method (and the reader in enrich.py).
        Must be called after resolve_effective_project_version() so that
        project_version reflects the final resolved state.
        """
        return {
            "project_version_descriptor": self.build_project_version_descriptor(),
            "log_level": self.log_level,
        }

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
        Single source of truth:
        - validates incoming params
        - fills defaults
        - guarantees schema compatible with PipelineArguments.from_dict()
        - ensures project_version is present as dict (or {}), not passed separately
        """
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            msg = "params must be a JSON object (dict)"
            raise TypeError(msg)

        normalized = dict(raw_params)

        # Always pin project_id so the generic execution worker can reconstruct arguments.
        normalized["project_id"] = project.id

        # ---- project_version ----
        pv = normalized.get("project_version")
        if pv is None:
            # allow omission: means "latest project version" if exists
            latest = (
                AISTProjectVersion.objects
                .filter(project=project, version_type=VersionType.GIT_BRANCH)
                .order_by("-updated", "-created")
                .first()
            )
            if latest is None:
                latest = (
                    AISTProjectVersion.objects
                    .filter(project=project)
                    .order_by("-updated", "-created")
                    .first()
                )
            normalized["project_version"] = latest.as_dict() if latest else {}
        elif isinstance(pv, int):
            obj = AISTProjectVersion.objects.get(pk=pv, project=project)
            normalized["project_version"] = obj.as_dict()
        elif isinstance(pv, dict):
            # if dict has id -> resolve to authoritative dict (prevents stale data)
            pv_id = pv.get("id")
            if pv_id:
                obj = AISTProjectVersion.objects.get(pk=pv_id, project=project)
                normalized["project_version"] = obj.as_dict()
            else:
                normalized["project_version"] = dict(pv)
        else:
            msg = "project_version must be an object (dict) or integer id or null"
            raise ValueError(msg)

        # ---- simple fields + defaults ----
        normalized["rebuild_images"] = bool(normalized.get("rebuild_images"))

        log_level = normalized.get("log_level", "INFO")
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            msg = "Unsupported log_level"
            raise ValueError(msg)
        normalized["log_level"] = log_level

        analyzers = normalized.get("analyzers", [])
        if analyzers is None:
            analyzers = []
        # TODO: add proper validation of analyzers
        if not isinstance(analyzers, list) or not all(isinstance(x, str) for x in analyzers):
            msg = "analyzers must be list[str]"
            raise ValueError(msg)
        normalized["analyzers"] = analyzers

        langs = normalized.get("selected_languages", [])
        if langs is None:
            langs = []
        if not isinstance(langs, list) or not all(isinstance(x, str) for x in langs):
            msg = "selected_languages must be list[str]"
            raise ValueError(msg)
        normalized["selected_languages"] = langs

        tcl = normalized.get("time_class_level", "slow")
        # keep current behavior; if you later convert to enum - do it here only
        if tcl is None:
            tcl = "slow"
        normalized["time_class_level"] = tcl

        env = normalized.get("env", {})
        if env is None:
            env = {}
        if not isinstance(env, dict):
            msg = "env must be a JSON object (dict)"
            raise TypeError(msg)

        if project.repository:
            env["REPO_URL"] = project.repository.clone_url
        env["PROJECT_NAME"] = SastPipelineArguments.normalize_project_name(project)
        normalized["env"] = env

        # ---- AI triage type (optional per-launch override) ----
        ai_triage_type = normalized.get("ai_triage_type")
        if ai_triage_type is not None and ai_triage_type not in {"n8n", "local"}:
            msg = "Unsupported ai_triage_type; allowed: n8n, local"
            raise ValueError(msg)
        normalized["ai_triage_type"] = ai_triage_type

        # ---- AI mode + snapshot rules ----
        ai_mode = normalized.get("ai_mode", "MANUAL") or "MANUAL"
        if ai_mode not in {"MANUAL", "AUTO_DEFAULT"}:
            msg = "Unsupported ai_mode"
            raise ValueError(msg)
        normalized["ai_mode"] = ai_mode

        if ai_mode == "MANUAL":
            # keep schema stable: snapshot is meaningless in MANUAL
            normalized["ai_filter_snapshot"] = None
            return normalized

        # AUTO_DEFAULT: snapshot is required for n8n, optional for local.
        snap = normalized.get("ai_filter_snapshot")
        resolved_triage = ai_triage_type  # may be None (resolved later from project profile)
        if snap is None and resolved_triage != "local":
            msg = "ai_filter_snapshot is required for AUTO_DEFAULT (n8n triage)"
            raise ValueError(msg)
        if snap is not None:
            normalized["ai_filter_snapshot"] = validate_and_normalize_filter(snap)
        else:
            normalized["ai_filter_snapshot"] = None

        return normalized

    @classmethod
    def from_dict(cls, data: dict) -> SastPipelineArguments:
        """
        Build PipelineArguments instance from dictionary.
        The dictionary must contain `project_id` instead of `project`.
        """
        try:
            project = AISTProject.objects.get(id=data["project_id"])
        except AISTProject.DoesNotExist:
            msg = MSG_PROJECT_NOT_FOUND_TPL.format(data["project_id"])
            raise ValueError(msg)

        normalized = cls.normalize_params(project=project, raw_params=data)

        return cls(
            project=project,
            project_version=normalized.get("project_version") or {},
            selected_analyzers=normalized.get("analyzers") or [],
            selected_languages=normalized.get("selected_languages") or [],
            log_level=normalized.get("log_level") or "INFO",
            rebuild_images=normalized.get("rebuild_images") or False,
            ai_mode=(normalized.get("ai_mode") or "MANUAL"),
            ai_triage_type=normalized.get("ai_triage_type"),
            ai_filter_snapshot=normalized.get("ai_filter_snapshot"),
            time_class_level=normalized.get("time_class_level") or "slow",
            additional_environments=normalized.get("env") or {},
        )

    @property
    def analyzers(self) -> list[str]:
        if self.selected_analyzers:
            return self.selected_analyzers

        cfg = _load_analyzers_config()
        if not cfg:
            return self.selected_analyzers

        filtered = cfg.get_filtered_analyzers(
            analyzers_to_run=None,
            max_time_class=self.time_class_level,
            non_compile_project=not self.project.compilable,
            target_languages=self.languages,
            show_only_parent=True,
        )
        names = set(cfg.get_names(filtered))
        profile = self.project.profile
        if not profile:
            # Just default list, by language
            return list(names)

        analyzer_profile = profile.get("analyzers", {})
        if analyzer_profile:
            if analyzer_profile.get("exclude"):
                names.difference_update(analyzer_profile.get("exclude"))
            if analyzer_profile.get("include", None):
                names.update(analyzer_profile.get("include"))

        return list(names)

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
    def project_name(self) -> str:
        return self.project.product.name

    @contextmanager
    def script_path_context(self) -> Iterator[str]:
        """
        Write the version's script to a temp file and yield its path.
        The temp file is removed when the context exits.

        Every AISTProjectVersion always has a script set (enforced by data migration
        and creation logic). Falls back to shared default only as a last-resort guard.
        """
        pv_id = (self.project_version or {}).get("id")
        script = None
        if pv_id:
            pv = (
                AISTProjectVersion.objects
                .select_related("script")
                .filter(pk=pv_id, project=self.project)
                .first()
            )
            if pv:
                script = pv.script
        if script is None:
            _logger.warning(
                "Project version for project %s has no script; falling back to shared default.",
                self.project.id,
            )
            script = AISTProjectScript.get_shared_default()
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
    def output_dir(self) -> str:
        return str(
            self.aist_path
            / (self.project_name or "project")
            / (self.project_version.get("version", "default")),
        )

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

    @classmethod
    def build(
        cls,
        *,
        project: AISTProject,
        binding: DastProjectBinding,
        trigger_project_version: AISTProjectVersion | None,
        raw_parameters: dict,
    ) -> DastPipelineArguments:
        if binding.project_id != project.pk:
            msg = "DAST binding must belong to the launch project."
            raise ValueError(msg)
        if not binding.enabled:
            msg = "DAST binding must be enabled."
            raise ValueError(msg)
        if binding.requires_source_repository:
            if trigger_project_version is None:
                msg = "DAST trigger version is required for this target."
                raise ValueError(msg)
            if trigger_project_version.project_id != project.pk:
                msg = "DAST trigger version must belong to the launch project."
                raise ValueError(msg)
            if trigger_project_version.version_type not in {VersionType.GIT_BRANCH, VersionType.GIT_HASH}:
                msg = "DAST trigger version must be a Git branch or Git hash."
                raise ValueError(msg)
        elif trigger_project_version is not None:
            msg = "DAST trigger version is not accepted for a target with no repository requirement."
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
                "ai_mode": self.payload.ai_mode,
                "ai_triage_type": self.payload.ai_triage_type,
                "ai_filter_snapshot": self.payload.ai_filter_snapshot,
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
        if config.dast_binding.requires_source_repository and config.trigger_project_version_id is None:
            msg = "DAST launch config requires a trigger version for this target."
            raise ValueError(msg)
        return cls.for_dast(
            project=config.project,
            binding=config.dast_binding,
            trigger_project_version=config.trigger_project_version,
            raw_params=dict(config.params or {}),
        )

    @classmethod
    def from_dict(cls, data: dict) -> PipelineArguments:
        project = AISTProject.objects.get(pk=data["project_id"])
        return cls.for_sast(project=project, raw_params=data)

    @classmethod
    def normalize_project_name(cls, project: AISTProject) -> str:
        return SastPipelineArguments.normalize_project_name(project)

    @classmethod
    def normalize_params(cls, *, project: AISTProject, raw_params: dict) -> dict:
        return SastPipelineArguments.normalize_params(project=project, raw_params=raw_params)
