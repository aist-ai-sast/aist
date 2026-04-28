from __future__ import annotations

from dataclasses import dataclass, field

from dojo.models import SEVERITY_CHOICES

# Single source of truth: DefectDojo's canonical severity list.
ALLOWED_SEVERITIES: frozenset[str] = frozenset(value for value, _ in SEVERITY_CHOICES)


@dataclass(frozen=True)
class PathsConfig:
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeverityConfig:
    exclude: tuple[str, ...] = ()


ALLOWED_AI_TRIAGE_TYPES: frozenset[str] = frozenset({"n8n", "local"})


@dataclass(frozen=True)
class AiTriageConfig:
    type: str = "n8n"  # "n8n" | "local"


# Keys recognised under ``agent_analyzers.full_security``. Unknown keys are
# rejected by ``validate_dict`` so a typo never silently disables an override.
FULL_SECURITY_LIMIT_FIELDS: tuple[str, ...] = (
    "max_files",
    "max_bytes",
    "max_file_bytes",
    "max_findings",
)


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
    ai_triage: AiTriageConfig = field(default_factory=AiTriageConfig)
    agent_analyzers: AgentAnalyzersConfig = field(default_factory=AgentAnalyzersConfig)

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: dict | None) -> ProjectProfile:
        """Build a :class:`ProjectProfile` from a raw profile dict (or ``None``)."""
        if not data:
            return cls()
        paths_raw = data.get("paths") or {}
        severity_raw = data.get("severity") or {}
        ai_triage_raw = data.get("ai_triage") or {}
        ai_triage_type = ai_triage_raw.get("type", "n8n") if isinstance(ai_triage_raw, dict) else "n8n"
        return cls(
            paths=PathsConfig(exclude=tuple(paths_raw.get("exclude") or [])),
            severity=SeverityConfig(exclude=tuple(severity_raw.get("exclude") or [])),
            ai_triage=AiTriageConfig(type=ai_triage_type if ai_triage_type in ALLOWED_AI_TRIAGE_TYPES else "n8n"),
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

    # ------------------------------------------------------------------ #
    # Validation (call before persisting user input)                       #
    # ------------------------------------------------------------------ #

    @classmethod
    def validate_dict(cls, data: dict) -> None:
        """Raise :exc:`TypeError` / :exc:`ValueError` if *data* is not a valid profile."""
        if not isinstance(data, dict):
            msg = 'Profile must be a JSON object (e.g. {"paths": {"exclude": []}}).'
            raise TypeError(msg)

        severity_raw = data.get("severity", {})
        if not isinstance(severity_raw, dict):
            msg = "'severity' must be a JSON object."
            raise TypeError(msg)

        excluded_sev = severity_raw.get("exclude", [])
        if not isinstance(excluded_sev, list):
            msg = "'severity.exclude' must be a list of strings."
            raise TypeError(msg)

        invalid = {s for s in excluded_sev if s not in ALLOWED_SEVERITIES}
        if invalid:
            msg = (
                f"Invalid severity values: {sorted(invalid)}. "
                f"Allowed: {sorted(ALLOWED_SEVERITIES)}."
            )
            raise ValueError(msg)

        ai_triage_raw = data.get("ai_triage", {})
        if ai_triage_raw:
            if not isinstance(ai_triage_raw, dict):
                msg = "'ai_triage' must be a JSON object."
                raise TypeError(msg)
            triage_type = ai_triage_raw.get("type")
            if triage_type is not None and triage_type not in ALLOWED_AI_TRIAGE_TYPES:
                msg = (
                    f"Invalid ai_triage.type: {triage_type!r}. "
                    f"Allowed: {sorted(ALLOWED_AI_TRIAGE_TYPES)}."
                )
                raise ValueError(msg)

        if "agent_analyzers" in data:
            cls._validate_agent_analyzers(data["agent_analyzers"])

    @staticmethod
    def _validate_agent_analyzers(raw: object) -> None:
        if not isinstance(raw, dict):
            msg = "'agent_analyzers' must be a JSON object."
            raise TypeError(msg)
        if "full_security" not in raw:
            return
        full_raw = raw["full_security"]
        if not isinstance(full_raw, dict):
            msg = "'agent_analyzers.full_security' must be a JSON object."
            raise TypeError(msg)
        unknown = sorted(set(full_raw) - set(FULL_SECURITY_LIMIT_FIELDS))
        if unknown:
            msg = (
                f"Unknown agent_analyzers.full_security keys: {unknown}. "
                f"Allowed: {list(FULL_SECURITY_LIMIT_FIELDS)}."
            )
            raise ValueError(msg)
        for name in FULL_SECURITY_LIMIT_FIELDS:
            if name not in full_raw:
                continue
            value = full_raw[name]
            # ``bool`` is a subclass of ``int`` in Python — reject it explicitly
            # so ``"max_files": true`` cannot pass as the integer 1.
            if not isinstance(value, int) or isinstance(value, bool):
                msg = f"'agent_analyzers.full_security.{name}' must be a positive integer."
                raise TypeError(msg)
            if value <= 0:
                msg = f"'agent_analyzers.full_security.{name}' must be > 0 (got {value})."
                raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    def get_excluded_paths(self) -> list[str]:
        return list(self.paths.exclude)

    def get_excluded_severities(self) -> list[str]:
        return list(self.severity.exclude)

    def get_ai_triage_type(self) -> str:
        return self.ai_triage.type

    def get_full_security_limits(self) -> FullSecurityLimits:
        return self.agent_analyzers.full_security
