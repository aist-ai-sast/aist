"""Strict trust-boundary validation for autonomous DAST terminal reports."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from aist.parser_overrides import DAST_SCAN_TYPE, DastReportParser

logger = logging.getLogger(__name__)

DAST_CONTRACT_VERSION = "2.0"
DAST_RESULT_MAX_BYTES = 16 * 1024 * 1024

# Structural caps on the provider-reported run metadata. Each sits about an order of
# magnitude above any plausible real inventory, so reaching one means the input is
# malformed rather than merely large — see "How a report's description is judged" in
# docs/integrations/dast.md.
DAST_COVERAGE_NAMES_MAX = 5000
DAST_COVERAGE_NAME_MAX_LENGTH = 253
DAST_TOKEN_BUCKETS_MAX = 64
DAST_QUALITY_SUMMARY_MAX = 5000
DAST_VALIDATION_CODES_MAX = 64

_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")
_REPOSITORY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPORT_FIELDS = {"name", "type", "version", "findings", "dast_run_metadata"}
_REPORT_REQUIRED_FIELDS = {"name", "type", "findings", "dast_run_metadata"}
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
    "coverage",
    "token_usage",
    "delivery_quality",
    "audit_state",
    "findings_complete",
    "operator_actions_persisted",
    "operator_actions",
    "operator_actions_total",
    "operator_actions_truncated",
    "excluded_findings",
    "excluded_findings_total",
    "excluded_findings_truncated",
}
_DESCRIPTOR_MAX_LENGTH = 64
_TARGET_HOST_MAX_LENGTH = 255
_TOKEN_BUCKET_NAME_MAX_LENGTH = 128
_TIMESTAMP_MAX_LENGTH = 64

# The metadata shape is declared once, as data, and drives both parsing and the persisted
# form. Field names match the wire names so a single comprehension fills the value object.
_RUN_DESCRIPTORS = {
    "product_family": _DESCRIPTOR_MAX_LENGTH,
    "tier": _DESCRIPTOR_MAX_LENGTH,
    "run_type": _DESCRIPTOR_MAX_LENGTH,
    "target_host": _TARGET_HOST_MAX_LENGTH,
}
_RUN_TIMESTAMPS = ("scan_started", "scan_finished")
_COVERAGE_COUNTS = ("discovered", "reachable", "analysed", "planned")
_COVERAGE_NAME_LISTS = ("analysed_names", "beyond_plan_names")
_COVERAGE_FIELDS = {"unit", *_COVERAGE_COUNTS, *_COVERAGE_NAME_LISTS}
_TOKEN_USAGE_FIELDS = {"total", "by_phase", "by_agent_type"}
# One bucket shape serves ``total`` and both breakdowns: ``name`` appears only on some
# phases, ``agents`` only on agent types, and every counter is independently optional.
# Wire counter name -> value-object attribute, since ``input`` cannot be an attribute name.
_TOKEN_BUCKET_COUNTERS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "thinking": "thinking_tokens",
    "cache_creation": "cache_creation_tokens",
    "cache_read": "cache_read_tokens",
    "calls": "calls",
}
_TOKEN_BUCKET_FIELDS = {"name", "agents", *_TOKEN_BUCKET_COUNTERS}
_TOKEN_COUNTER_ATTRIBUTES = tuple(_TOKEN_BUCKET_COUNTERS.values())
_DELIVERY_QUALITIES = {"complete", "degraded", "partial"}
_AUDIT_STATES = {"complete", "incomplete", "failed", "unavailable"}
_OPERATOR_ACTION_FIELDS = {"issue_id", "classification", "impact", "action_summary"}
_OPERATOR_CLASSIFICATIONS = {
    "engine_defect", "contract_defect", "authority_required", "evidence_corrupt",
    "infrastructure", "delivery", "teardown", "operator_stop",
}
_OPERATOR_IMPACTS = {"coverage", "findings", "audit", "delivery", "cleanup", "source", "none"}
_EXCLUDED_FINDING_FIELDS = {"finding_ref", "check_id", "validation_codes"}


class DastReportValidationError(ValueError):

    """The provider result cannot cross the report trust boundary."""


def _error(message: str) -> DastReportValidationError:
    return DastReportValidationError(message)


@dataclass(frozen=True, slots=True)
class DastReportExpectations:

    """
    What AIST already knows about a report before reading it.

    ``target_id`` and ``allowed_repository_keys`` come from the project binding and are always
    known. ``run_id`` and ``correlation_id`` are what AIST asked the *gateway* for, so they exist
    only when a transport delivered the report; an operator-exported file has no transport and
    carries its own run identity, which nothing prior can be checked against.
    """

    target_id: str
    allowed_repository_keys: frozenset[str]
    correlation_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.run_id, "run_id"),
            (self.target_id, "target_id"),
        ):
            if value is None and name != "target_id":
                continue
            if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
                msg = f"Expected {name} is invalid."
                raise _error(msg)
        if any(not _REPOSITORY_KEY_RE.fullmatch(key) for key in self.allowed_repository_keys):
            msg = "Allowed DAST repository keys are invalid."
            raise _error(msg)


@dataclass(frozen=True, slots=True)
class ValidatedDastSelection:
    stand_id: str | None
    relation: str
    distance: int


@dataclass(frozen=True, slots=True)
class _DastTransportClaims:

    """What a gateway asserted about a report while delivering it, to cross-check against it."""

    source_commits: tuple[tuple[str, str], ...]
    selection: ValidatedDastSelection


@dataclass(frozen=True, slots=True)
class DastCoverage:

    """What the run saw, in the unit the provider counts in. Every field is optional."""

    unit: str | None = None
    discovered: int | None = None
    reachable: int | None = None
    analysed: int | None = None
    planned: int | None = None
    analysed_names: tuple[str, ...] | None = None
    beyond_plan_names: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class DastTokenBucket:

    """One token-accounting bucket: the run total, a phase, or an agent type."""

    key: str = ""
    name: str | None = None
    agents: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    calls: int | None = None

    def as_wire(self) -> dict[str, Any]:
        """Persisted shape — only the counters the provider actually reported."""
        wire: dict[str, Any] = {"key": self.key} if self.key else {}
        for field_name in ("name", "agents", *_TOKEN_COUNTER_ATTRIBUTES):
            value = getattr(self, field_name)
            if value is not None:
                wire[field_name] = value
        return wire


@dataclass(frozen=True, slots=True)
class DastTokenUsage:

    """Agent token accounting for one run."""

    total: DastTokenBucket | None = None
    by_phase: tuple[DastTokenBucket, ...] | None = None
    by_agent_type: tuple[DastTokenBucket, ...] | None = None
    # True/False when a breakdown could be compared against ``total``, None when the report
    # carried nothing comparable. A False is recorded and surfaced, never rejected: both
    # sides are individually well-formed and only their relationship is off.
    accounting_consistent: bool | None = None


@dataclass(frozen=True, slots=True)
class DastOperatorAction:
    issue_id: str
    classification: str
    impact: str
    action_summary: str

    def as_wire(self) -> dict[str, str]:
        return {
            "issue_id": self.issue_id,
            "classification": self.classification,
            "impact": self.impact,
            "action_summary": self.action_summary,
        }


@dataclass(frozen=True, slots=True)
class DastExcludedFinding:
    finding_ref: str
    validation_codes: tuple[str, ...]
    check_id: str | None = None

    def as_wire(self) -> dict[str, Any]:
        return {
            "finding_ref": self.finding_ref,
            **({"check_id": self.check_id} if self.check_id is not None else {}),
            "validation_codes": list(self.validation_codes),
        }


@dataclass(frozen=True, slots=True)
class ValidatedDastRunMetadata:

    """``dast_run_metadata`` after validation. Only the three identities are guaranteed."""

    run_id: str
    target_id: str
    stand_id: str | None
    product_family: str | None = None
    tier: str | None = None
    run_type: str | None = None
    target_host: str | None = None
    scan_started: datetime | None = None
    scan_finished: datetime | None = None
    coverage: DastCoverage | None = None
    token_usage: DastTokenUsage | None = None
    delivery_quality: str | None = None
    audit_state: str | None = None
    findings_complete: bool | None = None
    operator_actions_persisted: bool | None = None
    operator_actions: tuple[DastOperatorAction, ...] | None = None
    operator_actions_total: int | None = None
    operator_actions_truncated: bool | None = None
    excluded_findings: tuple[DastExcludedFinding, ...] | None = None
    excluded_findings_total: int | None = None
    excluded_findings_truncated: bool | None = None


@dataclass(frozen=True, slots=True)
class ValidatedDastReport:

    """
    One report that has crossed the trust boundary.

    ``run_id``, ``target_id``, ``source_commits`` and ``run_metadata`` come out of the report
    itself. ``contract_version``, ``status``, ``selection`` and ``correlation_id`` describe the
    transport that delivered it and are None for a report read straight from an operator's file —
    no consumer reads them, so nothing downstream has to care which path a report arrived by.
    """

    run_id: str
    target_id: str
    source_commits: tuple[tuple[str, str], ...]
    findings_count: int
    canonical_json: bytes
    run_metadata: ValidatedDastRunMetadata | None = None
    contract_version: str | None = None
    correlation_id: str | None = None
    status: str | None = None
    source_verified: bool | None = None
    selection: ValidatedDastSelection | None = None

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
            msg = (
                "This report carries source revisions, but the selected DAST target declares no "
                "repository requirement, so it should report none."
            )
            raise _error(msg)
        return ()
    if not value:
        expected = ", ".join(f"'{key}'" for key in sorted(allowed_repository_keys))
        msg = (
            f"This report carries no source revision, but the selected DAST target is source-bound "
            f"and expects one of: {expected}."
        )
        raise _error(msg)
    normalized: list[tuple[str, str]] = []
    for repository_key, commit in value.items():
        if (
            not isinstance(repository_key, str)
            or not _REPOSITORY_KEY_RE.fullmatch(repository_key)
            or repository_key not in allowed_repository_keys
        ):
            expected = ", ".join(f"'{key}'" for key in sorted(allowed_repository_keys))
            msg = (
                f"This report reports a source revision for a repository the selected DAST target "
                f"does not advertise. It advertises: {expected}."
            )
            raise _error(msg)
        if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
            msg = "DAST source commit must be a lowercase full SHA-1."
            raise _error(msg)
        normalized.append((repository_key, commit))
    return tuple(sorted(normalized))


def _text(value: object, label: str, *, max_length: int) -> str | None:
    """
    Provider-reported free text: printable and bounded, with no shape imposed on it.

    An absent field and an explicit ``null`` mean the same thing — the provider did not
    report it. Deliberately not pattern-matched: these are names the provider chooses
    (endpoint slugs, URL paths, phase labels), and a character class here would reject
    legitimate values instead of protecting anything. Storage safety comes from the type
    and length bounds; rendering safety comes from React escaping every string it prints.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or not value.isprintable():
        msg = f"{label} is invalid."
        raise _error(msg)
    return value


def _required_text(value: object, label: str, *, max_length: int) -> str:
    text = _text(value, label, max_length=max_length)
    if text is None:
        msg = f"{label} is required."
        raise _error(msg)
    return text


def _count(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"{label} is invalid."
        raise _error(msg)
    return value


def _required_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise _error(f"{label} is invalid.")
    return value


def _optional_enum(value: object, label: str, allowed: set[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise _error(f"{label} is invalid.")
    return value


def _operator_action(value: object) -> DastOperatorAction:
    data = _described_object(value, "DAST operator action", known=_OPERATOR_ACTION_FIELDS)
    _exact_fields(data, _OPERATOR_ACTION_FIELDS, "DAST operator action")
    classification = _required_text(data["classification"], "DAST operator action classification", max_length=64)
    impact = _required_text(data["impact"], "DAST operator action impact", max_length=64)
    if classification not in _OPERATOR_CLASSIFICATIONS or impact not in _OPERATOR_IMPACTS:
        raise _error("DAST operator action classification or impact is invalid.")
    return DastOperatorAction(
        issue_id=_required_text(data["issue_id"], "DAST operator action issue_id", max_length=128),
        classification=classification,
        impact=impact,
        action_summary=_required_text(data["action_summary"], "DAST operator action summary", max_length=500),
    )


def _excluded_finding(value: object) -> DastExcludedFinding:
    allowed_fields = {_EXCLUDED_FINDING_FIELDS, _EXCLUDED_FINDING_FIELDS - {"check_id"}}
    if not isinstance(value, dict) or set(value) not in allowed_fields:
        raise _error("DAST excluded finding fields are invalid.")
    codes = value["validation_codes"]
    if not isinstance(codes, list) or not codes or len(codes) > DAST_VALIDATION_CODES_MAX:
        raise _error("DAST excluded finding validation_codes are invalid.")
    check_id = _text(value.get("check_id"), "DAST excluded finding check_id", max_length=255)
    return DastExcludedFinding(
        finding_ref=_required_text(value["finding_ref"], "DAST excluded finding reference", max_length=255),
        check_id=check_id,
        validation_codes=tuple(
            _required_text(code, "DAST excluded finding validation code", max_length=64) for code in codes
        ),
    )


def _summary_group(metadata: dict[str, Any], *, prefix: str, parser, persisted: bool = False) -> dict[str, Any]:
    names = {prefix, f"{prefix}_total", f"{prefix}_truncated"}
    if persisted:
        names.add(f"{prefix}_persisted")
    present = names & set(metadata)
    if not present:
        return {name: None for name in names}
    if present != names:
        raise _error(f"DAST {prefix} fields must be present as one complete group.")
    raw_rows = metadata[prefix]
    total = metadata[f"{prefix}_total"]
    truncated = metadata[f"{prefix}_truncated"]
    if not isinstance(raw_rows, list) or len(raw_rows) > DAST_QUALITY_SUMMARY_MAX:
        raise _error(f"DAST {prefix} is invalid.")
    if isinstance(total, bool) or not isinstance(total, int) or total < 1 or len(raw_rows) > total:
        raise _error(f"DAST {prefix}_total is invalid.")
    truncated = _required_bool(truncated, f"DAST {prefix}_truncated")
    if not truncated and len(raw_rows) != total:
        raise _error(f"DAST {prefix} length conflicts with its total.")
    result = {
        prefix: tuple(parser(row) for row in raw_rows),
        f"{prefix}_total": total,
        f"{prefix}_truncated": truncated,
    }
    if persisted:
        result[f"{prefix}_persisted"] = _required_bool(
            metadata[f"{prefix}_persisted"], f"DAST {prefix}_persisted",
        )
    return result


def _timestamp(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _TIMESTAMP_MAX_LENGTH:
        msg = f"{label} is invalid."
        raise _error(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"{label} is invalid."
        raise _error(msg) from exc
    # The exporter emits offset-free timestamps; read those as UTC so the stored instant is
    # unambiguous, and honour an explicit offset whenever the report carries one.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _described_object(value: object, label: str, *, known: set[str]) -> dict[str, Any]:
    """
    Read the fields we understand out of a descriptive block, ignoring any we do not.

    A key we have never heard of means the provider is ahead of us, not that its output is
    malformed — rejecting the report over it would throw away every finding in it and would need
    an AIST release to undo. Nothing here is acted on for trust, so an unread field costs nothing;
    it is logged so we notice we are behind, and it survives untouched in the stored report.

    Values of the fields we *do* understand stay strictly validated, and the trust-critical
    structure around this block (identities, source commits, selection, audit, scan type) stays
    closed to unknown fields.
    """
    if not isinstance(value, dict):
        msg = f"{label} must be an object."
        raise _error(msg)
    unread = sorted(set(value) - known)
    if unread:
        logger.info("Ignoring unread %s fields reported by the DAST provider: %s", label, ", ".join(unread))
    return value


def _names(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > DAST_COVERAGE_NAMES_MAX:
        msg = f"{label} is invalid."
        raise _error(msg)
    return tuple(
        _required_text(name, f"{label} entry", max_length=DAST_COVERAGE_NAME_MAX_LENGTH) for name in value
    )


def _coverage(value: object) -> DastCoverage | None:
    if value is None:
        return None
    data = _described_object(value, "DAST coverage", known=_COVERAGE_FIELDS)
    return DastCoverage(
        unit=_text(data.get("unit"), "DAST coverage unit", max_length=_DESCRIPTOR_MAX_LENGTH),
        **{field: _count(data.get(field), f"DAST coverage {field}") for field in _COVERAGE_COUNTS},
        **{field: _names(data.get(field), f"DAST coverage {field}") for field in _COVERAGE_NAME_LISTS},
    )


def _token_bucket(value: object, label: str, *, key: str = "") -> DastTokenBucket:
    data = _described_object(value, label, known=_TOKEN_BUCKET_FIELDS)
    return DastTokenBucket(
        key=key,
        name=_text(data.get("name"), f"{label} name", max_length=_TOKEN_BUCKET_NAME_MAX_LENGTH),
        agents=_count(data.get("agents"), f"{label} agents"),
        **{
            attribute: _count(data.get(wire_key), f"{label} {wire_key}")
            for wire_key, attribute in _TOKEN_BUCKET_COUNTERS.items()
        },
    )


def _token_buckets(value: object, label: str) -> tuple[DastTokenBucket, ...] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > DAST_TOKEN_BUCKETS_MAX:
        msg = f"{label} is invalid."
        raise _error(msg)
    return tuple(
        _token_bucket(
            bucket,
            f"{label} bucket",
            key=_required_text(key, f"{label} key", max_length=_DESCRIPTOR_MAX_LENGTH),
        )
        for key, bucket in value.items()
    )


def _accounting_consistent(
    total: DastTokenBucket | None,
    breakdowns: tuple[tuple[DastTokenBucket, ...] | None, ...],
) -> bool | None:
    """Compare every counter a breakdown can actually be checked against."""
    if total is None:
        return None
    comparable = False
    for buckets in breakdowns:
        if not buckets:
            continue
        for attribute in _TOKEN_COUNTER_ATTRIBUTES:
            expected = getattr(total, attribute)
            reported = [getattr(bucket, attribute) for bucket in buckets]
            if expected is None or any(value is None for value in reported):
                continue
            comparable = True
            if sum(reported) != expected:
                return False
    return True if comparable else None


def _token_usage(value: object) -> DastTokenUsage | None:
    if value is None:
        return None
    data = _described_object(value, "DAST token usage", known=_TOKEN_USAGE_FIELDS)
    raw_total = data.get("total")
    total = None if raw_total is None else _token_bucket(raw_total, "DAST token usage total")
    by_phase = _token_buckets(data.get("by_phase"), "DAST token usage by_phase")
    by_agent_type = _token_buckets(data.get("by_agent_type"), "DAST token usage by_agent_type")
    return DastTokenUsage(
        total=total,
        by_phase=by_phase,
        by_agent_type=by_agent_type,
        accounting_consistent=_accounting_consistent(total, (by_phase, by_agent_type)),
    )


def _run_metadata(
    metadata: dict[str, Any],
    *,
    run_id: str,
    target_id: str,
    stand_id: str | None,
) -> ValidatedDastRunMetadata:
    quality_names = {"delivery_quality", "audit_state", "findings_complete"}
    present_quality = quality_names & set(metadata)
    if present_quality and present_quality != quality_names:
        raise _error("DAST report delivery quality fields must be present as one complete group.")
    return ValidatedDastRunMetadata(
        run_id=run_id,
        target_id=target_id,
        stand_id=stand_id,
        **{
            field: _text(metadata.get(field), f"DAST report {field}", max_length=max_length)
            for field, max_length in _RUN_DESCRIPTORS.items()
        },
        **{field: _timestamp(metadata.get(field), f"DAST report {field}") for field in _RUN_TIMESTAMPS},
        coverage=_coverage(metadata.get("coverage")),
        token_usage=_token_usage(metadata.get("token_usage")),
        delivery_quality=_optional_enum(
            metadata.get("delivery_quality"), "DAST report delivery_quality", _DELIVERY_QUALITIES,
        ),
        audit_state=_optional_enum(metadata.get("audit_state"), "DAST report audit_state", _AUDIT_STATES),
        findings_complete=(
            _required_bool(metadata["findings_complete"], "DAST report findings_complete")
            if "findings_complete" in metadata else None
        ),
        **_summary_group(metadata, prefix="operator_actions", parser=_operator_action, persisted=True),
        **_summary_group(metadata, prefix="excluded_findings", parser=_excluded_finding),
    )


def _selection(value: object) -> ValidatedDastSelection:
    if not isinstance(value, dict):
        msg = "DAST selection must be an object."
        raise _error(msg)
    if set(value) == {"mode", "note"}:
        if value["mode"] != "none":
            raise _error("DAST stand-less selection mode is invalid.")
        _required_text(value["note"], "DAST stand-less selection note", max_length=500)
        return ValidatedDastSelection(stand_id=None, relation="none", distance=0)
    if set(value) == {"mode", "resolver", "tier", "commit_repo", "note"}:
        if value["mode"] != "dynamic":
            raise _error("DAST dynamic selection mode is invalid.")
        for field in ("resolver", "tier", "commit_repo"):
            _required_text(value[field], f"DAST dynamic selection {field}", max_length=255)
        _required_text(value["note"], "DAST dynamic selection note", max_length=500)
        return ValidatedDastSelection(stand_id=None, relation="dynamic", distance=0)
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
    transport: _DastTransportClaims | None,
    maximum_report_bytes: int,
) -> tuple[bytes, int, ValidatedDastRunMetadata, tuple[tuple[str, str], ...]]:
    """
    Validate one report envelope — the artifact `dast export-findings` writes.

    This is the only place a report is read, whichever way it arrived. ``transport`` carries what a
    gateway claimed about the report while delivering it, so those claims can be cross-checked
    against the report itself; an operator-exported file has no transport and nothing to
    cross-check, and every check that is about the *report* still runs either way.
    """
    if not isinstance(report, dict):
        msg = "DAST report must be an object."
        raise _error(msg)
    # The envelope itself stays closed. It is the outermost shape of the artifact AIST stores and
    # hands on to the importer, so an unrecognized key here could carry meaning to some other
    # consumer (a "report_path" is the classic shape) — a top-level addition has to be a deliberate
    # contract change, not a silent one. Descriptive evolution belongs in dast_run_metadata, which
    # is where it actually happens.
    if not isinstance(report, dict) or set(report) - _REPORT_FIELDS or not _REPORT_REQUIRED_FIELDS.issubset(report):
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

    metadata = _described_object(
        report["dast_run_metadata"],
        "DAST report metadata",
        known=_REPORT_METADATA_FIELDS,
    )
    # Conflict messages name both sides. A refusal an operator cannot act on is a refusal they
    # have to guess at, and the value they need is the one they cannot see: what the report says
    # about itself. Report-side values are already bounded and are rendered as text, never markup.
    report_run_id = _required_identity(metadata.get("run_id"), "DAST report run_id")
    if expectations.run_id is not None and report_run_id != expectations.run_id:
        msg = (
            f"This report is from DAST run '{report_run_id}', but the run being imported is "
            f"'{expectations.run_id}'."
        )
        raise _error(msg)
    report_target_id = _required_identity(metadata.get("target"), "DAST report target")
    if report_target_id != expectations.target_id:
        msg = (
            f"This report is from DAST target '{report_target_id}', but the selected binding is for "
            f"target '{expectations.target_id}'. Pick the binding for '{report_target_id}', or "
            f"synchronize the DAST catalog if that target is not bound yet."
        )
        raise _error(msg)
    if transport is None:
        # A manually carried report has no provider selection claim to compare. Stand is descriptive on
        # that path and is legitimately absent for perimeter/stand-less targets; validate it when present.
        report_stand_id = _text(metadata.get("stand"), "DAST report stand", max_length=255)
    elif transport.selection.relation == "none":
        report_stand_id = _text(metadata.get("stand"), "DAST report stand", max_length=255)
        if report_stand_id is not None:
            msg = "A stand-less DAST selection cannot report a selected stand."
            raise _error(msg)
    else:
        report_stand_id = _required_identity(metadata.get("stand"), "DAST report stand")
    if (
        transport is not None
        and transport.selection.stand_id is not None
        and report_stand_id != transport.selection.stand_id
    ):
        msg = (
            f"This report is from stand '{report_stand_id}', but the provider selected stand "
            f"'{transport.selection.stand_id}' for this run."
        )
        raise _error(msg)
    run_metadata = _run_metadata(
        metadata,
        run_id=report_run_id,
        target_id=report_target_id,
        stand_id=report_stand_id,
    )
    nested_commits = _source_commits(
        metadata.get("source_commits"),
        allowed_repository_keys=expectations.allowed_repository_keys,
    )
    if transport is not None and nested_commits != transport.source_commits:
        msg = "DAST report source commits conflict with the terminal result."
        raise _error(msg)

    # Known optional metadata nulls have one canonical spelling: absence. Unknown descriptive fields are
    # preserved exactly so a newer provider can round-trip them without AIST silently changing their value.
    canonical_report = dict(report)
    canonical_metadata = dict(metadata)
    for field in _REPORT_METADATA_FIELDS - {"run_id", "target", "source_commits"}:
        if canonical_metadata.get(field) is None:
            canonical_metadata.pop(field, None)
    canonical_report["dast_run_metadata"] = canonical_metadata
    canonical_json = json.dumps(
        canonical_report,
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
    return canonical_json, len(report["findings"]), run_metadata, nested_commits


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
    status = payload["status"]
    if status not in {"succeeded", "completed_with_degradation", "failed_with_partial_results"}:
        msg = "This DAST terminal state does not carry an importable report."
        raise _error(msg)

    audit = payload["audit"]
    if not isinstance(audit, dict):
        msg = "DAST terminal audit must be an object."
        raise _error(msg)
    _exact_fields(audit, {"correlation_id", "source_verified"}, "DAST terminal audit")
    correlation_id = _required_identity(audit.get("correlation_id"), "DAST audit correlation_id")
    if correlation_id != expectations.correlation_id:
        msg = "DAST correlation identity conflicts with the requested pipeline."
        raise _error(msg)
    source_verified = _required_bool(audit.get("source_verified"), "DAST audit source_verified")

    selection = _selection(payload["selection"])
    _validate_trigger_resolution(payload["trigger_resolution"])
    metadata = _described_object(
        payload["dast_run_metadata"],
        "DAST terminal metadata",
        known={"source_commits"},
    )
    if "source_commits" not in metadata:
        raise _error("DAST terminal metadata source_commits is required.")
    source_commits = _source_commits(
        metadata["source_commits"],
        allowed_repository_keys=expectations.allowed_repository_keys,
    )
    canonical_json, findings_count, run_metadata, report_commits = _validated_report_envelope(
        payload["report"],
        expectations=expectations,
        transport=_DastTransportClaims(source_commits=source_commits, selection=selection),
        maximum_report_bytes=maximum_report_bytes,
    )
    quality = (
        run_metadata.delivery_quality,
        run_metadata.audit_state,
        run_metadata.findings_complete,
    )
    if None in quality:
        raise _error("DAST terminal report requires the complete delivery quality group.")
    if status == "succeeded" and quality != ("complete", "complete", True):
        raise _error("DAST succeeded status conflicts with report delivery quality.")
    if status == "completed_with_degradation" and quality != ("degraded", "complete", True):
        raise _error("DAST degraded status conflicts with report delivery quality.")
    if status == "failed_with_partial_results":
        if run_metadata.delivery_quality != "partial" or (
            run_metadata.audit_state == "complete"
            and run_metadata.findings_complete is True
            and source_verified
        ):
            raise _error("DAST partial status requires at least one incomplete postcondition.")
    elif not source_verified:
        raise _error("DAST complete result did not verify source integrity.")
    return ValidatedDastReport(
        run_id=run_id,
        target_id=expectations.target_id,
        source_commits=report_commits,
        findings_count=findings_count,
        canonical_json=canonical_json,
        run_metadata=run_metadata,
        contract_version=DAST_CONTRACT_VERSION,
        correlation_id=correlation_id,
        status=status,
        source_verified=source_verified,
        selection=selection,
    )


def validate_exported_dast_report_bytes(
    raw: bytes,
    *,
    target_id: str,
    allowed_repository_keys: frozenset[str],
    maximum_report_bytes: int = DAST_RESULT_MAX_BYTES,
) -> ValidatedDastReport:
    """
    Validate the report an operator uploads: the artifact `dast export-findings` writes.

    That file — a Generic Findings envelope plus ``dast_run_metadata`` — is the whole thing the
    provider produces for a human to carry, so it is what this accepts. There is no transport
    around it to cross-check: a hand-built wrapper could only restate what the report already says
    (its run, stand and source commits) or assert its own trustworthiness, which checks nothing.
    Every check that is about the report runs exactly as it does on the autonomous path, through
    the same validator, and what the binding knows — the target and its repository keys — is still
    enforced against it.
    """
    report = _decode_json_object(raw, maximum_bytes=maximum_report_bytes)
    canonical_json, findings_count, run_metadata, source_commits = _validated_report_envelope(
        report,
        expectations=DastReportExpectations(
            target_id=target_id,
            allowed_repository_keys=allowed_repository_keys,
        ),
        transport=None,
        maximum_report_bytes=maximum_report_bytes,
    )
    return ValidatedDastReport(
        run_id=run_metadata.run_id,
        target_id=run_metadata.target_id,
        source_commits=source_commits,
        findings_count=findings_count,
        canonical_json=canonical_json,
        run_metadata=run_metadata,
        source_verified=None,
    )
