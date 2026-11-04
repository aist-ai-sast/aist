from __future__ import annotations

from dataclasses import asdict, dataclass, field

from dojo.models import SEVERITIES
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from aist.launch_data import AITriageType, LaunchAISettings


def _section(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _patterns(raw: object) -> tuple[str, ...]:
    """Read a stored exclusion list; a bare string is one pattern, and non-string or empty entries are dropped."""
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list | tuple):
        return tuple(item for item in raw if isinstance(item, str) and item)
    return ()


@dataclass(frozen=True)
class PathsConfig:
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeverityConfig:
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalyzersConfig:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class AiTriageConfig:
    type: AITriageType = AITriageType.N8N


FULL_SECURITY_LIMIT_FIELDS: tuple[str, ...] = (
    "max_files",
    "max_bytes",
    "max_file_bytes",
    "max_findings",
)

_NON_EMPTY_STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1}}

# The shape a profile must have to be saved. Unknown keys are rejected so a typo never silently disables a setting.
PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "paths": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"exclude": _NON_EMPTY_STRINGS},
        },
        "severity": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"exclude": {"type": "array", "items": {"enum": list(SEVERITIES)}}},
        },
        "analyzers": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"include": _NON_EMPTY_STRINGS, "exclude": _NON_EMPTY_STRINGS},
        },
        "ai_triage": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {"type": {"enum": [*AITriageType.values, None]}},
        },
        "agent_analyzers": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "full_security": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {name: {"type": "integer", "minimum": 1} for name in FULL_SECURITY_LIMIT_FIELDS},
                },
            },
        },
    },
}
_PROFILE_VALIDATOR = Draft202012Validator(PROFILE_SCHEMA)


@dataclass(frozen=True)
class FullSecurityLimits:
    # ``None`` means "fall back to the Django setting default" — concrete
    # integers come from AGENT_FULL_MAX_* env on the AIST side and are merged
    # by ``build_agent_runtime_env`` (Task 3).
    max_files: int | None = None
    max_bytes: int | None = None
    max_file_bytes: int | None = None
    max_findings: int | None = None


@dataclass(frozen=True)
class AgentAnalyzersConfig:
    full_security: FullSecurityLimits = field(default_factory=FullSecurityLimits)


@dataclass(frozen=True)
class ProjectProfile:

    """
    Typed view over the AISTProject.profile JSON field.

    Use :meth:`from_dict` to deserialise the raw dict stored in the DB, and
    :meth:`validate_dict` to reject invalid payloads before saving.
    """

    paths: PathsConfig = field(default_factory=PathsConfig)
    severity: SeverityConfig = field(default_factory=SeverityConfig)
    analyzers: AnalyzersConfig = field(default_factory=AnalyzersConfig)
    ai_triage: AiTriageConfig = field(default_factory=AiTriageConfig)
    agent_analyzers: AgentAnalyzersConfig = field(default_factory=AgentAnalyzersConfig)

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: dict | None) -> ProjectProfile:
        """Build a :class:`ProjectProfile` from a raw profile dict (or ``None``)."""
        if not isinstance(data, dict) or not data:
            return cls()
        paths_raw = _section(data, "paths")
        severity_raw = _section(data, "severity")
        analyzers_raw = _section(data, "analyzers")
        triage_type = _section(data, "ai_triage").get("type")
        return cls(
            paths=PathsConfig(exclude=_patterns(paths_raw.get("exclude"))),
            severity=SeverityConfig(
                exclude=tuple(name for name in _patterns(severity_raw.get("exclude")) if name in SEVERITIES),
            ),
            analyzers=AnalyzersConfig(
                include=_patterns(analyzers_raw.get("include")),
                exclude=_patterns(analyzers_raw.get("exclude")),
            ),
            ai_triage=AiTriageConfig(
                type=AITriageType(triage_type) if triage_type in AITriageType.values else AITriageType.N8N,
            ),
            agent_analyzers=cls._parse_agent_analyzers(data.get("agent_analyzers")),
        )

    @staticmethod
    def _parse_agent_analyzers(raw: object) -> AgentAnalyzersConfig:
        # Tolerant parse: unknown keys are ignored at read time so old DB rows
        # survive a schema bump. Validation lives in ``validate_dict``.
        if not isinstance(raw, dict):
            return AgentAnalyzersConfig()
        full_raw = raw.get("full_security")
        if not isinstance(full_raw, dict):
            return AgentAnalyzersConfig()
        kwargs: dict[str, int] = {}
        for name in FULL_SECURITY_LIMIT_FIELDS:
            value = full_raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                kwargs[name] = value
        return AgentAnalyzersConfig(full_security=FullSecurityLimits(**kwargs))

    def as_dict(self) -> dict:
        """The profile in the shape it is saved in, holding only the settings it carries."""
        sections = {
            "paths": {"exclude": list(self.paths.exclude)},
            "severity": {"exclude": list(self.severity.exclude)},
            "analyzers": {"include": list(self.analyzers.include), "exclude": list(self.analyzers.exclude)},
            "agent_analyzers": {
                "full_security": {
                    name: limit for name, limit in asdict(self.agent_analyzers.full_security).items() if limit is not None
                },
            },
        }
        saved = {name: {key: value for key, value in section.items() if value} for name, section in sections.items()}
        return {
            **{name: section for name, section in saved.items() if section},
            "ai_triage": {"type": self.ai_triage.type.value},
        }

    # ------------------------------------------------------------------ #
    # Validation (call before persisting user input)                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate_dict(data: object) -> None:
        """Raise :exc:`ValueError` naming the first place where *data* does not match :data:`PROFILE_SCHEMA`."""
        error = best_match(_PROFILE_VALIDATOR.iter_errors(data))
        if error is not None:
            msg = f"{error.json_path}: {error.message}"
            raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    def get_excluded_paths(self) -> list[str]:
        return list(self.paths.exclude)

    def excludes_finding(self, path: str, severity: str) -> bool:
        """
        Whether a finding is kept out of the platform: its `severity` is excluded, or an excluded path pattern is a
        substring of its `path`, written from the scanned checkout's root with a leading "/" (``/tests/x.py``).
        """
        return severity in self.severity.exclude or any(pattern in path for pattern in self.paths.exclude)

    def triage_type_for(self, launch: LaunchAISettings) -> AITriageType:
        """The triage that handles a launch's findings: the launch's own choice, else this project's."""
        return launch.triage_type or self.ai_triage.type

    def get_full_security_limits(self) -> FullSecurityLimits:
        return self.agent_analyzers.full_security
