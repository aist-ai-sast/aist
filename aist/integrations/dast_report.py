"""Strict trust-boundary validation for autonomous DAST terminal reports."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

from aist.parser_overrides import DAST_SCAN_TYPE, DastReportParser

DAST_CONTRACT_VERSION = "2.0"
DAST_RESULT_MAX_BYTES = 16 * 1024 * 1024

_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")
_REPOSITORY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPORT_FIELDS = {"name", "type", "version", "findings", "dast_run_metadata"}
_REPORT_METADATA_FIELDS = {
    "run_id",
    "target",
    "product_family",
    "tier",
    "run_type",
    "stand",
    "target_host",
    "source_commits",
    "scan_started",
    "scan_finished",
}


class DastReportValidationError(ValueError):

    """The provider result cannot cross the report trust boundary."""


def _error(message: str) -> DastReportValidationError:
    return DastReportValidationError(message)


@dataclass(frozen=True, slots=True)
class DastReportExpectations:
    correlation_id: str
    run_id: str
    target_id: str
    allowed_repository_keys: frozenset[str]

    def __post_init__(self) -> None:
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.run_id, "run_id"),
            (self.target_id, "target_id"),
        ):
            if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
                msg = f"Expected {name} is invalid."
                raise _error(msg)
        if any(not _REPOSITORY_KEY_RE.fullmatch(key) for key in self.allowed_repository_keys):
            msg = "Allowed DAST repository keys are invalid."
            raise _error(msg)


@dataclass(frozen=True, slots=True)
class ValidatedDastSelection:
    stand_id: str
    relation: str
    distance: int


@dataclass(frozen=True, slots=True)
class ValidatedDastReport:
    contract_version: str
    run_id: str
    correlation_id: str
    target_id: str
    status: str
    selection: ValidatedDastSelection
    source_commits: tuple[tuple[str, str], ...]
    findings_count: int
    canonical_json: bytes

    def source_commit_for(self, repository_key: str) -> str | None:
        return next((commit for key, commit in self.source_commits if key == repository_key), None)

    def open_report(self) -> BytesIO:
        return BytesIO(self.canonical_json)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = f"Duplicate JSON field: {key}."
            raise _error(msg)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    msg = f"Invalid JSON constant: {value}."
    raise _error(msg)


def _decode_json_object(raw: bytes, *, maximum_bytes: int) -> dict[str, Any]:
    if not raw or len(raw) > maximum_bytes:
        msg = "DAST terminal result exceeds its size limit."
        raise _error(msg)
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "DAST terminal result must be UTF-8 JSON."
        raise _error(msg) from exc
    if not isinstance(payload, dict):
        msg = "DAST terminal result must be a JSON object."
        raise _error(msg)
    return payload


def _exact_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        msg = f"{label} fields do not match the v2 contract."
        raise _error(msg)


def _required_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        msg = f"{label} is invalid."
        raise _error(msg)
    return value


def _source_commits(
    value: object,
    *,
    allowed_repository_keys: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        msg = "DAST source_commits must be an object."
        raise _error(msg)
    if not allowed_repository_keys:
        if value:
            msg = "DAST source_commits must be empty for a target with no repository requirement."
            raise _error(msg)
        return ()
    if not value:
        msg = "DAST source_commits must be a non-empty object."
        raise _error(msg)
    normalized: list[tuple[str, str]] = []
    for repository_key, commit in value.items():
        if (
            not isinstance(repository_key, str)
            or not _REPOSITORY_KEY_RE.fullmatch(repository_key)
            or repository_key not in allowed_repository_keys
        ):
            msg = "DAST report contains an unknown source repository key."
            raise _error(msg)
        if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
            msg = "DAST source commit must be a lowercase full SHA-1."
            raise _error(msg)
        normalized.append((repository_key, commit))
    return tuple(sorted(normalized))


def _selection(value: object) -> ValidatedDastSelection:
    if not isinstance(value, dict):
        msg = "DAST selection must be an object."
        raise _error(msg)
    _exact_fields(value, {"stand_id", "relation", "distance"}, "DAST selection")
    stand_id = _required_identity(value["stand_id"], "DAST selected stand")
    relation = value["relation"]
    if relation not in {"exact", "ancestor", "descendant"}:
        msg = "DAST selection relation is invalid."
        raise _error(msg)
    distance = value["distance"]
    if isinstance(distance, bool) or not isinstance(distance, int) or distance < 0:
        msg = "DAST selection distance is invalid."
        raise _error(msg)
    if relation == "exact" and distance != 0:
        msg = "An exact DAST selection must have zero distance."
        raise _error(msg)
    return ValidatedDastSelection(stand_id=stand_id, relation=relation, distance=distance)


def _validate_trigger_resolution(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        msg = "DAST trigger resolution must be an object or null."
        raise _error(msg)
    _exact_fields(value, {"type", "ref", "resolved_commit", "resolved_at"}, "DAST trigger resolution")
    if value["type"] not in {"GIT_BRANCH", "GIT_HASH"}:
        msg = "DAST trigger resolution type is invalid."
        raise _error(msg)
    if not isinstance(value["ref"], str) or not value["ref"] or len(value["ref"]) > 255:
        msg = "DAST trigger resolution ref is invalid."
        raise _error(msg)
    if not isinstance(value["resolved_commit"], str) or not _SHA_RE.fullmatch(value["resolved_commit"]):
        msg = "DAST resolved trigger commit is invalid."
        raise _error(msg)
    try:
        datetime.fromisoformat(str(value["resolved_at"]))
    except ValueError as exc:
        msg = "DAST trigger resolution time is invalid."
        raise _error(msg) from exc


def _validated_report_envelope(
    report: object,
    *,
    expectations: DastReportExpectations,
    outer_source_commits: tuple[tuple[str, str], ...],
    selection: ValidatedDastSelection,
    maximum_report_bytes: int,
) -> tuple[bytes, int]:
    if not isinstance(report, dict):
        msg = "DAST report must be an object."
        raise _error(msg)
    unknown = set(report) - _REPORT_FIELDS
    if unknown or not {"name", "type", "findings", "dast_run_metadata"}.issubset(report):
        msg = "DAST report envelope fields are invalid."
        raise _error(msg)
    if not isinstance(report["name"], str) or not report["name"].strip():
        msg = "DAST report name is invalid."
        raise _error(msg)
    if report["type"] != DAST_SCAN_TYPE:
        msg = "DAST report scan type is invalid."
        raise _error(msg)
    if "version" in report and not isinstance(report["version"], str):
        msg = "DAST report version is invalid."
        raise _error(msg)
    if not isinstance(report["findings"], list):
        msg = "DAST report findings must be an array."
        raise _error(msg)

    metadata = report["dast_run_metadata"]
    if not isinstance(metadata, dict):
        msg = "DAST report metadata must be an object."
        raise _error(msg)
    if set(metadata) - _REPORT_METADATA_FIELDS:
        msg = "DAST report metadata contains unknown fields."
        raise _error(msg)
    if _required_identity(metadata.get("run_id"), "DAST report run_id") != expectations.run_id:
        msg = "DAST report run identity conflicts with the terminal result."
        raise _error(msg)
    if _required_identity(metadata.get("target"), "DAST report target") != expectations.target_id:
        msg = "DAST report target conflicts with the selected target."
        raise _error(msg)
    if _required_identity(metadata.get("stand"), "DAST report stand") != selection.stand_id:
        msg = "DAST report stand conflicts with the provider selection."
        raise _error(msg)
    nested_commits = _source_commits(
        metadata.get("source_commits"),
        allowed_repository_keys=expectations.allowed_repository_keys,
    )
    if nested_commits != outer_source_commits:
        msg = "DAST report source commits conflict with the terminal result."
        raise _error(msg)

    canonical_json = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical_json) > maximum_report_bytes:
        msg = "DAST report exceeds its size limit."
        raise _error(msg)
    try:
        DastReportParser().get_tests(DAST_SCAN_TYPE, BytesIO(canonical_json))
    except (TypeError, ValueError, KeyError) as exc:
        msg = "DAST report does not match the registered finding schema."
        raise _error(msg) from exc
    return canonical_json, len(report["findings"])


def validate_dast_terminal_result_bytes(
    raw: bytes,
    *,
    expectations: DastReportExpectations,
    maximum_result_bytes: int = DAST_RESULT_MAX_BYTES,
    maximum_report_bytes: int = DAST_RESULT_MAX_BYTES,
) -> ValidatedDastReport:
    payload = _decode_json_object(raw, maximum_bytes=maximum_result_bytes)
    _exact_fields(
        payload,
        {
            "contract_version",
            "run_id",
            "status",
            "selection",
            "trigger_resolution",
            "dast_run_metadata",
            "report",
            "audit",
        },
        "DAST terminal result",
    )
    if payload["contract_version"] != DAST_CONTRACT_VERSION:
        msg = "Unsupported DAST terminal result contract version."
        raise _error(msg)
    run_id = _required_identity(payload["run_id"], "DAST terminal run_id")
    if run_id != expectations.run_id:
        msg = "DAST terminal run identity conflicts with the requested run."
        raise _error(msg)
    if payload["status"] != "succeeded":
        msg = "Only a successful DAST run can produce an importable report."
        raise _error(msg)

    audit = payload["audit"]
    if not isinstance(audit, dict):
        msg = "DAST terminal audit must be an object."
        raise _error(msg)
    correlation_id = _required_identity(audit.get("correlation_id"), "DAST audit correlation_id")
    if correlation_id != expectations.correlation_id:
        msg = "DAST correlation identity conflicts with the requested pipeline."
        raise _error(msg)
    if audit.get("source_verified") is not True:
        msg = "DAST terminal result did not verify source integrity."
        raise _error(msg)

    selection = _selection(payload["selection"])
    _validate_trigger_resolution(payload["trigger_resolution"])
    metadata = payload["dast_run_metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"source_commits"}:
        msg = "DAST terminal metadata fields are invalid."
        raise _error(msg)
    source_commits = _source_commits(
        metadata["source_commits"],
        allowed_repository_keys=expectations.allowed_repository_keys,
    )
    canonical_json, findings_count = _validated_report_envelope(
        payload["report"],
        expectations=expectations,
        outer_source_commits=source_commits,
        selection=selection,
        maximum_report_bytes=maximum_report_bytes,
    )
    return ValidatedDastReport(
        contract_version=DAST_CONTRACT_VERSION,
        run_id=run_id,
        correlation_id=correlation_id,
        target_id=expectations.target_id,
        status="succeeded",
        selection=selection,
        source_commits=source_commits,
        findings_count=findings_count,
        canonical_json=canonical_json,
    )


def validate_manual_dast_terminal_result_bytes(
    raw: bytes,
    *,
    target_id: str,
    allowed_repository_keys: frozenset[str],
    maximum_result_bytes: int = DAST_RESULT_MAX_BYTES,
    maximum_report_bytes: int = DAST_RESULT_MAX_BYTES,
) -> ValidatedDastReport:
    """Validate an operator-uploaded v2 terminal artifact without trusting a UI commit."""
    payload = _decode_json_object(raw, maximum_bytes=maximum_result_bytes)
    run_id = _required_identity(payload.get("run_id"), "DAST terminal run_id")
    audit = payload.get("audit")
    if not isinstance(audit, dict):
        msg = "DAST terminal audit must be an object."
        raise _error(msg)
    correlation_id = _required_identity(audit.get("correlation_id"), "DAST audit correlation_id")
    canonical_terminal = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return validate_dast_terminal_result_bytes(
        canonical_terminal,
        expectations=DastReportExpectations(
            correlation_id=correlation_id,
            run_id=run_id,
            target_id=target_id,
            allowed_repository_keys=allowed_repository_keys,
        ),
        maximum_result_bytes=maximum_result_bytes,
        maximum_report_bytes=maximum_report_bytes,
    )
