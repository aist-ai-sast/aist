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
        )

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

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    def get_excluded_paths(self) -> list[str]:
        return list(self.paths.exclude)

    def get_excluded_severities(self) -> list[str]:
        return list(self.severity.exclude)

    def get_ai_triage_type(self) -> str:
        return self.ai_triage.type
