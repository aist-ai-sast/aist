from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class LaunchRequestSnapshotError(ValueError):

    """Raised when a launch request snapshot is not safe to persist."""


_SENSITIVE_KEYS = frozenset({
    "access_token",
    "api_key",
    "api_token",
    "authorization",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
})
_ERR_JSON_OBJECT = "{label} must be a JSON object."
_ERR_FINITE_JSON = "{label} must contain only finite JSON values."
_ERR_STRING_KEYS = "Snapshot object keys must be strings at {path}."
_ERR_SENSITIVE_FIELD = "Sensitive field {path!r} cannot be persisted in a launch request."


def _validate_snapshot_value(value: Any, *, path: tuple[str, ...]) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str):
                message = _ERR_STRING_KEYS.format(path=".".join(path) or "<root>")
                raise LaunchRequestSnapshotError(message)
            normalized_key = raw_key.strip().lower().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS:
                message = _ERR_SENSITIVE_FIELD.format(path=".".join((*path, raw_key)))
                raise LaunchRequestSnapshotError(message)
            _validate_snapshot_value(nested, path=(*path, raw_key))
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_snapshot_value(nested, path=(*path, str(index)))


def _validated_object(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LaunchRequestSnapshotError(_ERR_JSON_OBJECT.format(label=label))
    return validated_secret_free_json(dict(value), label=label)


def validated_secret_free_json(value: Any, *, label: str) -> Any:
    """Return a detached finite JSON value after rejecting credential-shaped keys."""
    snapshot = deepcopy(value)
    _validate_snapshot_value(snapshot, path=())
    try:
        json.dumps(snapshot, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LaunchRequestSnapshotError(_ERR_FINITE_JSON.format(label=label)) from exc
    return snapshot


@dataclass(frozen=True, slots=True)
class LaunchRequestSnapshots:

    """Immutable, secret-free persistence boundary for launch request JSON."""

    params: Mapping[str, Any]
    capability: Mapping[str, Any]

    @classmethod
    def from_values(
        cls,
        *,
        params: Mapping[str, Any],
        capability: Mapping[str, Any],
    ) -> LaunchRequestSnapshots:
        return cls(
            params=_validated_object(params, label="params_snapshot"),
            capability=_validated_object(capability, label="capability_snapshot"),
        )

    def params_snapshot(self) -> dict[str, Any]:
        return deepcopy(dict(self.params))

    def capability_snapshot(self) -> dict[str, Any]:
        return deepcopy(dict(self.capability))
