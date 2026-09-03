"""Trust-boundary validation for imported DAST reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from aist.parser_overrides import DAST_SCAN_TYPE

DAST_RESULT_MAX_BYTES = 16 * 1024 * 1024

# The metadata shape is declared once, as data, and drives both parsing and the persisted
# form. Field names match the wire names so a single comprehension fills the value object.
_RUN_DESCRIPTORS = ("product_family", "tier", "run_type", "target_host")
_RUN_TIMESTAMPS = ("scan_started", "scan_finished")
_COVERAGE_COUNTS = ("discovered", "reachable", "analysed", "planned")
_COVERAGE_NAME_LISTS = ("analysed_names", "beyond_plan_names")
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
_TOKEN_COUNTER_ATTRIBUTES = tuple(_TOKEN_BUCKET_COUNTERS.values())
_TOKEN_ECONOMY_COUNTS = (
    "tokens_per_check", "tokens", "checks", "commands", "waits", "artifact_reads", "contract_reads",
)
_DELIVERY_QUALITIES = {"complete", "degraded", "partial"}
_AUDIT_STATES = {"complete", "incomplete", "failed", "unavailable"}
_OPERATOR_CLASSIFICATIONS = {
    "engine_defect", "contract_defect", "authority_required", "evidence_corrupt",
    "infrastructure", "delivery", "teardown", "operator_stop",
}
_OPERATOR_IMPACTS = {"coverage", "findings", "audit", "delivery", "cleanup", "source", "none"}
_EXCLUDED_FINDING_CODES = {
    "CHECK_PROVENANCE_INVALID", "CURATED_REPORT_AMBIGUOUS", "CURATED_REPORT_INVALID",
    "CURATED_REPORT_MISSING", "INVALID_FIELD_VALUE", "MISSING_REQUIRED_FIELD", "UNDECLARED_FIELD",
}


class DastReportValidationError(ValueError):

    """The provider result cannot cross the report trust boundary."""


def _error(message: str) -> DastReportValidationError:
    return DastReportValidationError(message)


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
class DastRunEconomy:

    """Provider-owned measurements of one check's cost and harness overhead."""

    tokens_per_check: int | None = None
    tokens: int | None = None
    checks: int | None = None
    commands: int | None = None
    waits: int | None = None
    artifact_reads: int | None = None
    contract_reads: int | None = None

    def as_wire(self) -> dict[str, int]:
        return {
            field_name: value
            for field_name in _TOKEN_ECONOMY_COUNTS
            if (value := getattr(self, field_name)) is not None
        }


@dataclass(frozen=True, slots=True)
class DastTokenUsage:

    """Agent token accounting for one run."""

    total: DastTokenBucket | None = None
    by_phase: tuple[DastTokenBucket, ...] | None = None
    by_agent_type: tuple[DastTokenBucket, ...] | None = None
    economy: DastRunEconomy | None = None
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
    finding_ref: str | None
    validation_codes: tuple[str, ...]
    check_id: str | None = None

    def as_wire(self) -> dict[str, Any]:
        return {
            **({"finding_ref": self.finding_ref} if self.finding_ref is not None else {}),
            **({"check_id": self.check_id} if self.check_id is not None else {}),
            "validation_codes": list(self.validation_codes),
        }


@dataclass(frozen=True, slots=True)
class ValidatedDastRunMetadata:

    """``dast_run_metadata`` after parsing. Only run and target identity are guaranteed."""

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

    Both autonomous execution and operator upload pass the same report artifact through this
    value object. Transport claims deliberately do not participate in report validation.
    """

    run_id: str
    target_id: str
    source_commits: tuple[tuple[str, Any], ...]
    findings_count: int
    canonical_json: bytes
    run_metadata: ValidatedDastRunMetadata | None = None

    def source_commit_for(self, repository_key: str) -> Any | None:
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
        msg = "DAST report exceeds its size limit."
        raise _error(msg)
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "DAST report must be UTF-8 JSON."
        raise _error(msg) from exc
    if not isinstance(payload, dict):
        msg = "DAST report must be a JSON object."
        raise _error(msg)
    return payload


def _source_commits(
    value: object,
    *,
    allowed_repository_keys: frozenset[str],
) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, dict):
        msg = "DAST source_commits must be an object."
        raise _error(msg)
    normalized: list[tuple[str, Any]] = []
    for repository_key, commit in value.items():
        if repository_key not in allowed_repository_keys:
            expected = ", ".join(f"'{key}'" for key in sorted(allowed_repository_keys))
            msg = (
                f"This report reports a source revision for a repository the selected DAST target "
                f"does not advertise. It advertises: {expected}."
            )
            raise _error(msg)
        normalized.append((repository_key, commit))
    return tuple(sorted(normalized))


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_enum(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_names(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    names: list[str] = []
    for value_item in value:
        name = _optional_text(value_item)
        if name is None:
            return None
        names.append(name)
    return tuple(names)


def _operator_action(value: object) -> DastOperatorAction | None:
    if not isinstance(value, dict):
        return None
    issue_id = _optional_text(value.get("issue_id"))
    classification = _optional_enum(value.get("classification"), _OPERATOR_CLASSIFICATIONS)
    impact = _optional_enum(value.get("impact"), _OPERATOR_IMPACTS)
    action_summary = _optional_text(value.get("action_summary"))
    if any(field is None for field in (issue_id, classification, impact, action_summary)):
        return None
    return DastOperatorAction(
        issue_id=issue_id,
        classification=classification,
        impact=impact,
        action_summary=action_summary,
    )


def _operator_actions(value: object) -> tuple[DastOperatorAction, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    actions: list[DastOperatorAction] = []
    for value_item in value:
        action = _operator_action(value_item)
        if action is None:
            return None
        actions.append(action)
    return tuple(actions)


def _excluded_finding(value: object) -> DastExcludedFinding | None:
    if not isinstance(value, dict):
        return None
    finding_ref = _optional_text(value.get("finding_ref"))
    check_id = _optional_text(value.get("check_id"))
    raw_codes = value.get("validation_codes")
    if not isinstance(raw_codes, list) or not raw_codes:
        return None
    codes: list[str] = []
    for raw_code in raw_codes:
        code = _optional_text(raw_code)
        if code is None or code not in _EXCLUDED_FINDING_CODES:
            return None
        codes.append(code)
    if finding_ref is None:
        if check_id is not None or "CHECK_PROVENANCE_INVALID" not in codes:
            return None
    elif check_id != finding_ref:
        return None
    return DastExcludedFinding(
        finding_ref=finding_ref,
        check_id=check_id,
        validation_codes=tuple(codes),
    )


def _excluded_findings(value: object) -> tuple[DastExcludedFinding, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    findings: list[DastExcludedFinding] = []
    for value_item in value:
        finding = _excluded_finding(value_item)
        if finding is None:
            return None
        findings.append(finding)
    return tuple(findings)


def _coverage(value: object) -> DastCoverage | None:
    if not isinstance(value, dict) or not value:
        return None
    return DastCoverage(
        unit=_optional_text(value.get("unit")),
        **{
            field: _optional_count(value.get(field))
            for field in _COVERAGE_COUNTS
        },
        **{
            field: _optional_names(value.get(field))
            for field in _COVERAGE_NAME_LISTS
        },
    )


def _token_bucket(value: object, *, key: str = "") -> DastTokenBucket | None:
    if not isinstance(value, dict) or not value:
        return None
    return DastTokenBucket(
        key=key,
        name=_optional_text(value.get("name")),
        agents=_optional_count(value.get("agents")),
        **{
            attribute: _optional_count(value.get(wire_key))
            for wire_key, attribute in _TOKEN_BUCKET_COUNTERS.items()
        },
    )


def _token_buckets(value: object) -> tuple[DastTokenBucket, ...] | None:
    if not isinstance(value, dict) or not value:
        return None
    buckets: list[DastTokenBucket] = []
    for key, value_item in value.items():
        parsed_key = _optional_text(key)
        if parsed_key is None:
            return None
        bucket = _token_bucket(value_item, key=parsed_key)
        if bucket is None:
            return None
        buckets.append(bucket)
    return tuple(buckets)


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
    if not isinstance(value, dict) or not value:
        return None
    total = _token_bucket(value.get("total"))
    by_phase = _token_buckets(value.get("by_phase"))
    by_agent_type = _token_buckets(value.get("by_agent_type"))
    raw_economy = value.get("economy")
    economy = (
        DastRunEconomy(**{
            field_name: _optional_count(raw_economy.get(field_name))
            for field_name in _TOKEN_ECONOMY_COUNTS
        })
        if isinstance(raw_economy, dict) and raw_economy
        else None
    )
    return DastTokenUsage(
        total=total,
        by_phase=by_phase,
        by_agent_type=by_agent_type,
        economy=economy,
        accounting_consistent=_accounting_consistent(total, (by_phase, by_agent_type)),
    )


def _run_metadata(
    metadata: dict[str, Any],
    *,
    run_id: str,
    target_id: str,
    stand_id: str | None,
) -> ValidatedDastRunMetadata:
    return ValidatedDastRunMetadata(
        run_id=run_id,
        target_id=target_id,
        stand_id=stand_id,
        **{
            field: _optional_text(metadata.get(field))
            for field in _RUN_DESCRIPTORS
        },
        **{
            field: _optional_timestamp(metadata.get(field))
            for field in _RUN_TIMESTAMPS
        },
        coverage=_coverage(metadata.get("coverage")),
        token_usage=_token_usage(metadata.get("token_usage")),
        delivery_quality=_optional_enum(metadata.get("delivery_quality"), _DELIVERY_QUALITIES),
        audit_state=_optional_enum(metadata.get("audit_state"), _AUDIT_STATES),
        findings_complete=_optional_bool(metadata.get("findings_complete")),
        operator_actions_persisted=_optional_bool(metadata.get("operator_actions_persisted")),
        operator_actions=_operator_actions(metadata.get("operator_actions")),
        operator_actions_total=_optional_count(metadata.get("operator_actions_total")),
        operator_actions_truncated=_optional_bool(metadata.get("operator_actions_truncated")),
        excluded_findings=_excluded_findings(metadata.get("excluded_findings")),
        excluded_findings_total=_optional_count(metadata.get("excluded_findings_total")),
        excluded_findings_truncated=_optional_bool(metadata.get("excluded_findings_truncated")),
    )


def validate_dast_report_bytes(
    raw: bytes,
    *,
    target_id: str,
    allowed_repository_keys: frozenset[str],
    expected_run_id: str | None = None,
    maximum_report_bytes: int = DAST_RESULT_MAX_BYTES,
) -> ValidatedDastReport:
    """Validate only the report properties required for safe, correct tenant-bound import."""
    report = _decode_json_object(raw, maximum_bytes=maximum_report_bytes)
    if report.get("type") != DAST_SCAN_TYPE:
        msg = "DAST report scan type is invalid."
        raise _error(msg)
    findings = report.get("findings")
    if not isinstance(findings, list):
        msg = "DAST report findings must be an array."
        raise _error(msg)
    metadata = report.get("dast_run_metadata")
    if not isinstance(metadata, dict):
        msg = "DAST report metadata must be an object."
        raise _error(msg)
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        msg = "DAST report run_id must be a non-empty string."
        raise _error(msg)
    if expected_run_id is not None and run_id != expected_run_id:
        msg = "DAST report run_id does not match the provider run being finalized."
        raise _error(msg)
    report_target_id = metadata.get("target")
    if report_target_id != target_id:
        msg = (
            f"This report is from DAST target '{report_target_id}', but the selected binding is for "
            f"target '{target_id}'. Pick the binding for '{report_target_id}', or synchronize the "
            "DAST catalog if that target is not bound yet."
        )
        raise _error(msg)
    source_commits = _source_commits(
        metadata.get("source_commits"),
        allowed_repository_keys=allowed_repository_keys,
    )
    stand_id = _optional_text(metadata.get("stand"))
    run_metadata = _run_metadata(
        metadata,
        run_id=run_id,
        target_id=target_id,
        stand_id=stand_id,
    )
    canonical_json = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ValidatedDastReport(
        run_id=run_id,
        target_id=target_id,
        source_commits=source_commits,
        findings_count=len(findings),
        canonical_json=canonical_json,
        run_metadata=run_metadata,
    )
