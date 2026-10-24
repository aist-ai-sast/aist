"""The completion receipt records delivery identity; destinations live on imported tests."""

from __future__ import annotations

from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_IDENTITY_PROPERTIES = {
    "binding_id": {"type": "integer"},
    "run_id": {"type": "string"},
    "correlation_id": {"type": ["string", "null"]},
    "report_sha256": {"type": "string"},
}
_RECEIPT_SCHEMA = {
    "type": "object",
    "properties": _IDENTITY_PROPERTIES,
    "required": list(_IDENTITY_PROPERTIES),
    "additionalProperties": False,
}
# Read compatibility for receipts written before destinations became test-owned.
# This is the only place that knows the old storage format.
_LEGACY_PROPERTIES = {
    **_IDENTITY_PROPERTIES,
    "version": {"const": "1"},
    "project_version_id": {"type": "integer"},
    "test_id": {"type": "integer"},
    "finding_ids": {"type": "array", "items": {"type": "integer"}},
}
_LEGACY_SCHEMA = {
    "type": "object",
    "properties": _LEGACY_PROPERTIES,
    "required": list(_LEGACY_PROPERTIES),
    "additionalProperties": False,
}
_RECEIPT_VALIDATOR = Draft202012Validator({"oneOf": [_RECEIPT_SCHEMA, _LEGACY_SCHEMA]})


@dataclass(frozen=True, slots=True)
class LegacyDastResult:
    project_version_id: int
    test_id: int
    finding_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DastImportReceipt:
    binding_id: int
    run_id: str
    correlation_id: str | None
    report_sha256: str
    legacy_result: LegacyDastResult | None = None

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in _IDENTITY_PROPERTIES}

    @classmethod
    def parse(cls, value: object) -> DastImportReceipt:
        try:
            _RECEIPT_VALIDATOR.validate(value)
        except ValidationError as exc:
            msg = "Persisted DAST finalization receipt is invalid."
            raise ValueError(msg) from exc
        legacy_result = None
        if "version" in value:
            legacy_result = LegacyDastResult(
                project_version_id=value["project_version_id"],
                test_id=value["test_id"],
                finding_ids=tuple(value["finding_ids"]),
            )
        return cls(**{name: value[name] for name in _IDENTITY_PROPERTIES}, legacy_result=legacy_result)
