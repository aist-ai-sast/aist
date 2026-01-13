from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from django.conf import settings

from dojo.aist.ai_filter import get_required_ai_filter_for_start, validate_and_normalize_filter
from dojo.aist.models import AISTProject, AISTProjectVersion
from dojo.aist.utils.pipeline_imports import _load_analyzers_config

# Error messages (for TRY003/EM101/EM102)
MSG_PROJECT_NOT_FOUND_TPL = "AISTProject with id={} not found"
MSG_INCORRECT_SCRIPT_PATH = "Incorrect script path for AIST pipeline."
MSG_DOCKERFILE_NOT_FOUND = "Dockerfile does not exist"


@dataclass
class PipelineArguments:
    project: AISTProject
    project_version: dict = field(default_factory=dict)
    selected_analyzers: list[str] = field(default_factory=list)
    selected_languages: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    rebuild_images: bool = False
    ai_mode: str = "MANUAL"  # MANUAL | AUTO_DEFAULT
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

        # Always pin project_id here (so run_sast_pipeline can reconstruct args)
        normalized["project_id"] = project.id

        # ---- project_version ----
        pv = normalized.get("project_version")
        if pv is None:
            # allow omission: means "latest project version" if exists
            latest = (
                AISTProjectVersion.objects
                .filter(project=project)
                .order_by("-created")
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
        normalized["env"] = env

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

        # AUTO_DEFAULT: snapshot must be a resolved effective filter (or explicit provided snapshot)
        snap = normalized.get("ai_filter_snapshot")
        if snap is not None:
            normalized["ai_filter_snapshot"] = validate_and_normalize_filter(snap)
        else:
            _scope, resolved = get_required_ai_filter_for_start(
                project=project,
                provided_filter=None,
            )
            normalized["ai_filter_snapshot"] = resolved

        return normalized

    @classmethod
    def from_dict(cls, data: dict) -> PipelineArguments:
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
        names = cfg.get_names(filtered)
        profile = self.project.profile
        if not profile:
            # Just default list, by language
            return names

        analyzer_profile = profile.get("analyzers", {})
        if analyzer_profile:
            if analyzer_profile.get("exclude"):
                for name in analyzer_profile.get("exclude"):
                    names.remove(name)
            if analyzer_profile.get("include", None):
                for name in analyzer_profile.get("include"):
                    names.add(name)

        return names

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

    @property
    def script_path(self) -> str:
        script_path = self.pipeline_path / self.project.script_path
        if not script_path.is_file():
            msg = MSG_INCORRECT_SCRIPT_PATH
            raise RuntimeError(msg)
        return str(script_path)

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
