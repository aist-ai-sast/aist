from __future__ import annotations

from dataclasses import dataclass

from django.db import models

from aist.ai_filter import AIFilterError, validate_and_normalize_filter
from aist.integrations.dast_config import DastExecutionSnapshot


class AIMode(models.TextChoices):
    MANUAL = "MANUAL", "Manual selection of findings for AI"
    AUTO_DEFAULT = "AUTO_DEFAULT", "Send findings to AI automatically with configured filter"


class AITriageType(models.TextChoices):
    N8N = "n8n", "n8n"
    LOCAL = "local", "Local"


AI_FILTER_REQUIRED = "ai_filter is required for AUTO_DEFAULT"

# Launch-param key of each LaunchAISettings field.
_PARAM_KEYS = {"mode": "ai_mode", "triage_type": "ai_triage_type", "filter_snapshot": "ai_filter_snapshot"}


@dataclass(frozen=True)
class LaunchAISettings:

    """How a pipeline's findings reach AI triage, fixed when it is launched."""

    mode: AIMode = AIMode.MANUAL
    triage_type: AITriageType | None = None
    filter_snapshot: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict | None) -> LaunchAISettings:
        """Read stored ``launch_data["ai"]``; unknown values fall back to the defaults."""
        raw = raw or {}
        triage_type = raw.get("triage_type")
        return cls(
            mode=AIMode(raw["mode"]) if raw.get("mode") in AIMode.values else AIMode.MANUAL,
            triage_type=AITriageType(triage_type) if triage_type in AITriageType.values else None,
            filter_snapshot=raw.get("filter_snapshot"),
        )

    @classmethod
    def from_params(cls, params: dict) -> LaunchAISettings:
        """
        Read the launch params' ``ai_mode`` / ``ai_triage_type`` / ``ai_filter_snapshot``.

        Raises ``ValueError`` for an unknown mode or triage type and ``AIFilterError`` for an invalid filter.
        A MANUAL launch carries no filter.
        """
        mode = params.get("ai_mode") or AIMode.MANUAL
        if mode not in AIMode.values:
            msg = "Unsupported ai_mode"
            raise ValueError(msg)
        triage_type = params.get("ai_triage_type")
        if triage_type is not None and triage_type not in AITriageType.values:
            msg = f"Unsupported ai_triage_type; allowed: {', '.join(AITriageType.values)}"
            raise ValueError(msg)
        snapshot = params.get("ai_filter_snapshot")
        return cls(
            mode=AIMode(mode),
            triage_type=AITriageType(triage_type) if triage_type else None,
            filter_snapshot=(
                validate_and_normalize_filter(snapshot) if mode == AIMode.AUTO_DEFAULT and snapshot is not None else None
            ),
        )

    @classmethod
    def from_normalized_params(cls, params: dict) -> LaunchAISettings:
        """Read the AI settings of launch params :meth:`from_params` already accepted, without validating them again."""
        return cls.from_dict({name: params.get(key) for name, key in _PARAM_KEYS.items()})

    def as_dict(self) -> dict:
        return {
            "mode": str(self.mode),
            "triage_type": str(self.triage_type) if self.triage_type else None,
            "filter_snapshot": self.filter_snapshot,
        }

    def as_params(self) -> dict:
        stored = self.as_dict()
        return {key: stored[name] for name, key in _PARAM_KEYS.items()}

    def pushes_automatically(self, triage_type: AITriageType) -> bool:
        """Whether findings go to ``triage_type`` on their own: AUTO_DEFAULT with a filter, or with local triage."""
        return self.mode == AIMode.AUTO_DEFAULT and (bool(self.filter_snapshot) or triage_type == AITriageType.LOCAL)

    def check(self, triage_type: AITriageType) -> None:
        """Raise ``AIFilterError`` when an AUTO_DEFAULT launch would not push to ``triage_type`` for lack of a filter."""
        if self.mode == AIMode.AUTO_DEFAULT and not self.pushes_automatically(triage_type):
            raise AIFilterError(AI_FILTER_REQUIRED)


class PipelineLaunchData:

    """
    Typed facade over the ``launch_data`` JSON field of :class:`AISTPipeline`.

    ``launch_data`` is a mix of:
    - fields returned by the external *sast-combinator* package (we do not own
      these keys and cannot guarantee their presence);
    - fields we write ourselves (log_level, enrich config, etc.).

    This class provides typed, named access to all known fields and preserves
    any unknown keys so that round-tripping through the constructor and ``as_dict``
    never silently drops data from the external package.

    Usage::

        ld = PipelineLaunchData(pipeline.launch_data)
        ld.languages = ["python"]
        pipeline.launch_data = ld.as_dict()
    """

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict = dict(data or {})

    @property
    def trim_path(self) -> str:
        return self._data.get("trim_path") or ""

    @property
    def project_path(self) -> str:
        return self._data.get("project_path") or ""

    @property
    def output_dir(self) -> str:
        return self._data.get("output_dir") or ""

    @property
    def tmp_analyzer_config_path(self) -> str | None:
        return self._data.get("tmp_analyzer_config_path")

    @property
    def resolved_commit(self) -> str:
        """Convenience accessor for the nested ``git.resolved_commit`` field."""
        return ((self._data.get("git") or {}).get("resolved_commit") or "").strip()

    @property
    def languages(self) -> list[str]:
        return self._data.get("languages") or []

    @languages.setter
    def languages(self, value: list[str]) -> None:
        self._data["languages"] = value

    @property
    def ai(self) -> LaunchAISettings:
        return LaunchAISettings.from_dict(self._data.get("ai"))

    @ai.setter
    def ai(self, value: LaunchAISettings) -> None:
        self._data["ai"] = value.as_dict()

    @property
    def finding_postprocessing(self) -> bool:
        """
        Whether this pipeline's findings go through enrichment and AI triage after dedup.

        Set to False by an import whose findings describe a running system rather than a source
        tree: enrichment trims file paths and links findings to source, and AIST cannot triage a
        finding it has no code for. Absent means True, so every existing pipeline keeps the
        source-code tail.
        """
        return self._data.get("finding_postprocessing", True) is not False

    @finding_postprocessing.setter
    def finding_postprocessing(self, value: bool) -> None:
        self._data["finding_postprocessing"] = bool(value)

    @property
    def launch_config_id(self) -> str | None:
        return self._data.get("launch_config_id")

    @launch_config_id.setter
    def launch_config_id(self, value: str) -> None:
        self._data["launch_config_id"] = value

    @property
    def log_level(self) -> str:
        return self._data.get("log_level") or "INFO"

    @log_level.setter
    def log_level(self, value: str) -> None:
        self._data["log_level"] = value

    @property
    def imported_test_ids(self) -> list[int]:
        return self._data.get("imported_test_ids") or []

    @imported_test_ids.setter
    def imported_test_ids(self, value: list[int]) -> None:
        self._data["imported_test_ids"] = value

    @property
    def analyzer_outcomes(self) -> list[dict]:
        return self._data.get("analyzer_outcomes") or []

    @property
    def analyzer_versions(self) -> list[dict]:
        """Which build of each analyzer produced this run, as sast-combinator recorded it."""
        return self._data.get("analyzer_versions") or []

    @property
    def analyzer_degraded_reasons(self) -> list[dict]:
        return self._data.get("analyzer_degraded_reasons") or []

    @analyzer_degraded_reasons.setter
    def analyzer_degraded_reasons(self, value: list[dict]) -> None:
        self._data["analyzer_degraded_reasons"] = value

    @property
    def has_analyzer_degraded_reasons(self) -> bool:
        return bool(self.analyzer_degraded_reasons)

    def add_analyzer_degraded_reasons(self, reasons: list[dict]) -> None:
        if not reasons:
            return
        self.analyzer_degraded_reasons = [*self.analyzer_degraded_reasons, *reasons]

    @property
    def action_runs(self) -> list[dict]:
        return self._data.get("action_runs") or []

    @action_runs.setter
    def action_runs(self, value: list[dict]) -> None:
        self._data["action_runs"] = value

    @property
    def launch_config_actions(self) -> list[dict] | None:
        """Frozen launch-config actions, or ``None`` for a legacy pipeline."""
        value = self._data.get("launch_config_actions")
        return value if isinstance(value, list) else None

    @launch_config_actions.setter
    def launch_config_actions(self, value: list[dict]) -> None:
        self._data["launch_config_actions"] = value

    @property
    def one_off_actions(self) -> list[dict]:
        return self._data.get("one_off_actions") or []

    @one_off_actions.setter
    def one_off_actions(self, value: list[dict]) -> None:
        self._data["one_off_actions"] = value

    @property
    def one_off_actions_done(self) -> list[str]:
        return self._data.get("one_off_actions_done") or []

    @one_off_actions_done.setter
    def one_off_actions_done(self, value: list[str]) -> None:
        self._data["one_off_actions_done"] = value

    @property
    def dast_execution(self) -> DastExecutionSnapshot:
        """The DAST plan frozen at launch; raises ``DastConfigError`` when absent or malformed."""
        return DastExecutionSnapshot.from_snapshot(self._data.get("dast_execution"))

    @dast_execution.setter
    def dast_execution(self, value: DastExecutionSnapshot) -> None:
        self._data["dast_execution"] = value.to_snapshot()

    def merge(self, fields: dict) -> None:
        """Merge a dict of fields into launch data."""
        self._data.update(fields)

    def as_dict(self) -> dict:
        """Return the underlying dict for storage in ``pipeline.launch_data``."""
        return self._data
