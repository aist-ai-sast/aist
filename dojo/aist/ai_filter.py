from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.utils.dateparse import parse_date, parse_datetime

DEFAULT_AI_FILTER_LIMIT = 10
MAX_AI_FILTER_LIMIT = 100

ORDER_BY_DIRECTIONS = {"ASC", "DESC"}
DEFAULT_ORDER_BY = [{"field": "severity", "direction": "DESC"}]

SUPPORTED_COMPARISONS = {
    "EQUALS",
    "NOT_EQUALS",
    "IN",
    "NOT_IN",
    "CONTAINS",
    "NOT_CONTAINS",
    "PREFIX",
    "REGEX",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "EXISTS",
}

COMPARISON_DESCRIPTIONS = {
    "EQUALS": "Exact match.",
    "NOT_EQUALS": "Does not match.",
    "IN": "Value is in the provided list.",
    "NOT_IN": "Value is not in the provided list.",
    "CONTAINS": "Substring match (case-sensitive).",
    "NOT_CONTAINS": "Does not contain substring (case-sensitive).",
    "PREFIX": "Starts with (case-sensitive).",
    "REGEX": "Regular expression match.",
    "GT": "Greater than.",
    "GTE": "Greater than or equal to.",
    "LT": "Less than.",
    "LTE": "Less than or equal to.",
    "EXISTS": "Field is present (or absent when value is false).",
}

FILTER_KEYWORD_REFERENCE = [
    {
        "key": "limit",
        "type": "int",
        "required": True,
        "description": "Maximum number of findings to include in the AI batch (1..MAX).",
    },
    {
        "key": "order_by",
        "type": "list[order]",
        "required": False,
        "description": "Ordering for results. Defaults to severity (Critical first).",
    },
    {
        "key": "<field>",
        "type": "list[condition]",
        "required": False,
        "description": "Field name with a list of conditions. Field names must be from the allowed fields list.",
    },
    {
        "key": "comparison",
        "type": "str",
        "required": True,
        "description": "Operator for a condition. Case-insensitive; normalized to uppercase.",
    },
    {
        "key": "value",
        "type": "any",
        "required": True,
        "description": "Value for comparison. IN/NOT_IN require a list.",
    },
]


@dataclass(frozen=True)
class FieldSpec:
    django_path: str
    type: str  # "str" | "int" | "bool" | "date" | "datetime"
    allow_regex: bool = True
    allow_contains: bool = True


FINDING_FILTER_FIELD_MAP = {
    "severity": FieldSpec("severity", "str", allow_regex=False),
    "cwe": FieldSpec("cwe", "int", allow_regex=False, allow_contains=False),
    "analyzer": FieldSpec("test__test_type__name", "str", allow_regex=False),
    "title": FieldSpec("title", "str"),
    "file_path": FieldSpec("file_path", "str"),
    "active": FieldSpec("active", "bool", allow_regex=False, allow_contains=False),
    "verified": FieldSpec("verified", "bool", allow_regex=False, allow_contains=False),
    "false_p": FieldSpec("false_p", "bool", allow_regex=False, allow_contains=False),
    "duplicate": FieldSpec("duplicate", "bool", allow_regex=False, allow_contains=False),
    "mitigated": FieldSpec("mitigated", "bool", allow_regex=False, allow_contains=False),
    "date": FieldSpec("date", "datetime", allow_regex=False, allow_contains=False),
}


def _severity_rank_case() -> Case:
    """
    Rank severities for ordering.
    Critical(5) > High(4) > Medium(3) > Low(2) > Info(1) > other(0)
    """
    return Case(
        When(severity__iexact="Critical", then=Value(5)),
        When(severity__iexact="High", then=Value(4)),
        When(severity__iexact="Medium", then=Value(3)),
        When(severity__iexact="Low", then=Value(2)),
        When(severity__iexact="Informational", then=Value(1)),
        When(severity__iexact="Info", then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )


def _coerce_value(tp: str, v: Any) -> Any:
    if tp == "bool":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        msg = f"Invalid bool: {v}"
        raise ValueError(msg)

    if tp == "int":
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not s or not s.lstrip("-").isdigit():
            msg = f"Invalid int: {v}"
            raise ValueError(msg)
        return int(s)

    if tp == "date":
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        s = str(v).strip()
        d = parse_date(s)
        if not d:
            msg = f"Invalid date (YYYY-MM-DD expected): {v}"
            raise ValueError(msg)
        return d

    if tp == "datetime":
        if isinstance(v, datetime):
            return v
        s = str(v).strip()
        dt = parse_datetime(s)
        if not dt:
            msg = f"Invalid datetime (ISO 8601 expected): {v}"
            raise ValueError(msg)
        return dt

    # default: string
    s = str(v)
    if s is None:
        msg = "String value cannot be null"
        raise ValueError(msg)
    return s


def _coerce_list(tp: str, v: Any) -> list[Any]:
    if not isinstance(v, list):
        msg = "List expected for IN/NOT_IN"
        raise TypeError(msg)
    return [_coerce_value(tp, x) for x in v]


def _q_exists(path: str, *, exists: bool) -> Q:
    if exists:
        return ~Q(**{f"{path}__isnull": True})
    return Q(**{f"{path}__isnull": True})


def _cond_to_q(spec: FieldSpec, cond: dict[str, Any]) -> Q:
    cmp_ = (cond.get("comparison") or "").strip().upper()
    if cmp_ not in SUPPORTED_COMPARISONS:
        msg = f"Unsupported comparison: {cmp_}"
        raise ValueError(msg)

    path = spec.django_path
    tp = spec.type

    # EXISTS
    if cmp_ == "EXISTS":
        exists_val = _coerce_value("bool", cond.get("value"))
        return _q_exists(path, exists=exists_val)

    # IN / NOT_IN
    if cmp_ in {"IN", "NOT_IN"}:
        values = _coerce_list(tp, cond.get("value"))
        q = Q(**{f"{path}__in": values})
        return ~q if cmp_ == "NOT_IN" else q

    # Other comparisons require scalar value
    value = _coerce_value(tp, cond.get("value"))

    if cmp_ == "EQUALS":
        return Q(**{path: value})
    if cmp_ == "NOT_EQUALS":
        return ~Q(**{path: value})

    if cmp_ == "CONTAINS":
        if not spec.allow_contains:
            msg = "CONTAINS not allowed for this field"
            raise ValueError(msg)
        return Q(**{f"{path}__icontains": value})
    if cmp_ == "NOT_CONTAINS":
        if not spec.allow_contains:
            msg = "NOT_CONTAINS not allowed for this field"
            raise ValueError(msg)
        return ~Q(**{f"{path}__icontains": value})

    if cmp_ == "PREFIX":
        return Q(**{f"{path}__istartswith": value})

    if cmp_ == "REGEX":
        if not spec.allow_regex:
            msg = "REGEX not allowed for this field"
            raise ValueError(msg)
        # prevent catastrophic patterns a bit: length cap + compile check
        s = str(value)
        if len(s) > 256:
            msg = "REGEX pattern too long"
            raise ValueError(msg)
        try:
            re.compile(s)
        except re.error as e:
            msg = f"Invalid REGEX: {e}"
            raise ValueError(msg)
        return Q(**{f"{path}__iregex": s})

    # ordering comparisons
    if cmp_ == "GT":
        return Q(**{f"{path}__gt": value})
    if cmp_ == "GTE":
        return Q(**{f"{path}__gte": value})
    if cmp_ == "LT":
        return Q(**{f"{path}__lt": value})
    if cmp_ == "LTE":
        return Q(**{f"{path}__lte": value})

    msg = f"Unhandled comparison: {cmp_}"
    raise ValueError(msg)


def _normalize_limit(raw: Any) -> int:
    if raw is None:
        msg = "Filter must contain required key 'limit'"
        raise ValueError(msg)
    try:
        n = int(raw)
    except Exception as e:
        msg = "'limit' must be an integer"
        raise ValueError(msg) from e
    if n < 1:
        msg = "'limit' must be >= 1"
        raise ValueError(msg)
    if n > MAX_AI_FILTER_LIMIT:
        msg = f"'limit' must be <= {MAX_AI_FILTER_LIMIT}"
        raise ValueError(msg)
    return n


def _normalize_order_by(raw: Any, field_map: dict[str, FieldSpec]) -> list[dict[str, str]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        msg = "order_by must be a non-empty list"
        raise ValueError(msg)

    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            msg = "order_by entries must be objects"
            raise TypeError(msg)
        field = (item.get("field") or "").strip()
        if not field:
            msg = "order_by field is required"
            raise ValueError(msg)
        if field not in field_map:
            msg = f"Unsupported order_by field: {field}"
            raise ValueError(msg)

        direction = (item.get("direction") or "").strip().upper()
        if not direction:
            direction = "DESC" if field == "severity" else "ASC"
        if direction not in ORDER_BY_DIRECTIONS:
            msg = "order_by direction must be ASC or DESC"
            raise ValueError(msg)

        normalized.append({"field": field, "direction": direction})
    return normalized


def validate_and_normalize_filter(filter_spec: Any, field_map=None) -> dict[str, Any]:
    """
    AWS-like format:
      {
        "limit": 50,
        "severity": [{"comparison":"EQUALS","value":"HIGH"}, ...],
        "cwe": [...]
      }

    - Validates structure
    - Enforces whitelist via field_map
    - Normalizes comparisons to uppercase
    - 'limit' is mandatory, stored back into normalized dict
    """
    if field_map is None:
        field_map = FINDING_FILTER_FIELD_MAP

    if filter_spec is None:
        msg = "Filter must be a JSON object with required key 'limit'"
        raise ValueError(msg)

    if not isinstance(filter_spec, dict):
        msg = "Filter must be a JSON object"
        raise TypeError(msg)

    limit = _normalize_limit(filter_spec.get("limit"))
    order_by = _normalize_order_by(filter_spec.get("order_by"), field_map)

    normalized: dict[str, Any] = {"limit": limit}
    if order_by:
        normalized["order_by"] = order_by

    for key, conditions in filter_spec.items():
        if key == "limit":
            continue
        if key == "order_by":
            continue

        if key not in field_map:
            msg = f"Unsupported filter field: {key}"
            raise ValueError(msg)

        if not isinstance(conditions, list) or not conditions:
            msg = f"Field '{key}' must be a non-empty list"
            raise ValueError(msg)

        out: list[dict[str, Any]] = []
        for c in conditions:
            if not isinstance(c, dict):
                msg = f"Condition for '{key}' must be an object"
                raise TypeError(msg)

            cmp_ = (c.get("comparison") or "").strip().upper()
            if cmp_ not in SUPPORTED_COMPARISONS:
                msg = f"Unsupported comparison: {cmp_}"
                raise ValueError(msg)

            if "value" not in c:
                msg = f"Condition for '{key}' must contain 'value'"
                raise ValueError(msg)

            out.append({"comparison": cmp_, "value": c.get("value")})

        normalized[key] = out

    has_conditions = any(k not in {"limit", "order_by"} for k in normalized)
    if not has_conditions:
        msg = "Filter must contain at least one field condition besides 'limit'"
        raise ValueError(msg)

    return normalized


def apply_ai_filter(qs: QuerySet, filter_spec: dict[str, Any], field_map=None) -> QuerySet:
    """
    AND between fields, OR between conditions within a field (AWS-like).
    'limit' is ignored here (used by caller to slice).
    """
    if field_map is None:
        field_map = FINDING_FILTER_FIELD_MAP

    normalized = validate_and_normalize_filter(filter_spec, field_map)

    combined = Q()
    for field_key, conditions in normalized.items():
        if field_key in {"limit", "order_by"}:
            continue

        spec = field_map[field_key]
        field_q = Q()
        for cond in conditions:
            field_q |= _cond_to_q(spec, cond)

        combined &= field_q

    qs = qs.filter(combined)

    order_by = normalized.get("order_by") or DEFAULT_ORDER_BY
    order_expressions: list[str] = []
    severity_rank_added = False
    for order in order_by:
        field = order["field"]
        direction = order["direction"]
        if field == "severity":
            if not severity_rank_added:
                qs = qs.annotate(sev_rank=_severity_rank_case())
                severity_rank_added = True
            expr = "-sev_rank" if direction == "DESC" else "sev_rank"
        else:
            path = field_map[field].django_path
            expr = f"-{path}" if direction == "DESC" else path
        order_expressions.append(expr)

    if order_expressions:
        qs = qs.order_by(*order_expressions)

    return qs


def resolve_effective_default_ai_filter(project):
    if not project:
        return (None, None)

    pf = getattr(project, "ai_default_filter", None)
    if pf:
        return ("PROJECT", pf)

    org = getattr(project, "organization", None)
    if org:
        of = getattr(org, "ai_default_filter", None)
        if of:
            return ("ORG", of)

    return (None, None)


def get_required_ai_filter_for_start(*, project, provided_filter):
    """
    Common logic for both Start UI and Start API.
    Returns (scope, normalized_filter).
    """
    if provided_filter is not None:
        normalized = validate_and_normalize_filter(provided_filter)
        return ("REQUEST", normalized)

    scope, eff = resolve_effective_default_ai_filter(project)
    if eff is None:
        msg = "Default AI filter is not configured for this project/organization and was not provided."
        raise ValueError(msg)

    normalized = validate_and_normalize_filter(eff)
    return (scope or "UNKNOWN", normalized)


def get_ai_filter_reference() -> dict[str, Any]:
    comparisons = sorted(SUPPORTED_COMPARISONS)
    fields = []
    for name, spec in FINDING_FILTER_FIELD_MAP.items():
        allowed = set(SUPPORTED_COMPARISONS)
        if not getattr(spec, "allow_contains", True):
            allowed.discard("CONTAINS")
            allowed.discard("NOT_CONTAINS")
        if not getattr(spec, "allow_regex", True):
            allowed.discard("REGEX")
            allowed.discard("NOT_REGEX")
        fields.append(
            {
                "name": name,
                "type": spec.type,
                "allowed_comparisons": sorted(allowed),
                "allow_regex": spec.allow_regex,
                "allow_contains": spec.allow_contains,
            },
        )
    comparison_details = [
        {"comparison": c, "description": COMPARISON_DESCRIPTIONS.get(c, "")} for c in comparisons
    ]
    return {
        "ok": True,
        "comparisons": comparisons,
        "comparison_details": comparison_details,
        "fields": fields,
        "keywords": FILTER_KEYWORD_REFERENCE,
    }
