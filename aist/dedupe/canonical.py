from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings


class CanonicalFamily(StrEnum):
    PRIVATE_KEY = "private_key"
    AWS_KEY = "aws_key"
    HARDCODED_SECRET = "hardcoded_secret"  # noqa: S105
    SSL_VERIFICATION = "ssl_verification"
    WEAK_HASH = "weak_hash"
    PATH_TRAVERSAL = "path_traversal"
    OPEN_REDIRECT = "open_redirect"
    XSS_DOM = "xss_dom"
    EVAL_DYNAMIC_CODE = "eval_dynamic_code"
    COMMAND_INJECTION = "command_injection"
    SQL_INJECTION = "sql_injection"
    POSTMESSAGE_ORIGIN = "postmessage_origin"
    UNKNOWN = "unknown"


class MatchVerdict(StrEnum):
    DUPLICATE = "duplicate"
    CANDIDATE = "candidate"
    NO_MATCH = "no_match"


DEFAULT_AUTO_DUPLICATE_THRESHOLD = 4
DEFAULT_CANDIDATE_MIN_SCORE = 2


_FAMILY_PATTERNS: dict[CanonicalFamily, tuple[re.Pattern[str], ...]] = {
    CanonicalFamily.PRIVATE_KEY: (
        re.compile(r"private[_\s-]?key", re.IGNORECASE),
        re.compile(r"rsa[_\s-]?key", re.IGNORECASE),
    ),
    CanonicalFamily.AWS_KEY: (
        re.compile(r"aws[_\s-]?(key|access[_\s-]?key|secret)", re.IGNORECASE),
        re.compile(r"akia[0-9a-z]{8,}", re.IGNORECASE),
    ),
    CanonicalFamily.HARDCODED_SECRET: (
        re.compile(r"hardcoded[_\s-]?(secret|password|token)", re.IGNORECASE),
        re.compile(r"detected[_\s-]?secret", re.IGNORECASE),
    ),
    CanonicalFamily.SSL_VERIFICATION: (
        re.compile(r"ssl[_\s-]?(verify|verification)", re.IGNORECASE),
        re.compile(r"(no|disable[d]?)[_\s-]?verify", re.IGNORECASE),
        re.compile(r"tls[_\s-]?verify", re.IGNORECASE),
    ),
    CanonicalFamily.WEAK_HASH: (
        re.compile(r"(md5|sha1|weak[_\s-]?hash)", re.IGNORECASE),
    ),
    CanonicalFamily.PATH_TRAVERSAL: (
        re.compile(r"path[_\s-]?traversal", re.IGNORECASE),
        re.compile(r"directory[_\s-]?traversal", re.IGNORECASE),
    ),
    CanonicalFamily.OPEN_REDIRECT: (
        re.compile(r"open[_\s-]?redirect", re.IGNORECASE),
        re.compile(r"\bjavascript/or\b", re.IGNORECASE),
        re.compile(r"\bor\b", re.IGNORECASE),
    ),
    CanonicalFamily.XSS_DOM: (
        re.compile(r"\bxss\b", re.IGNORECASE),
        re.compile(r"cross[_\s-]?site[_\s-]?scripting", re.IGNORECASE),
        re.compile(r"dom[_\s-]?xss", re.IGNORECASE),
    ),
    CanonicalFamily.EVAL_DYNAMIC_CODE: (
        re.compile(r"eval", re.IGNORECASE),
        re.compile(r"dynamic[_\s-]?code", re.IGNORECASE),
    ),
    CanonicalFamily.COMMAND_INJECTION: (
        re.compile(r"command[_\s-]?injection", re.IGNORECASE),
        re.compile(r"os[_\s-]?command", re.IGNORECASE),
    ),
    CanonicalFamily.SQL_INJECTION: (
        re.compile(r"sql[_\s-]?injection", re.IGNORECASE),
        re.compile(r"sqli", re.IGNORECASE),
    ),
    CanonicalFamily.POSTMESSAGE_ORIGIN: (
        re.compile(r"postmessage", re.IGNORECASE),
        re.compile(r"origin[_\s-]?check", re.IGNORECASE),
    ),
}

_FAMILY_CWE: dict[CanonicalFamily, int] = {
    CanonicalFamily.PRIVATE_KEY: 321,
    CanonicalFamily.AWS_KEY: 798,
    CanonicalFamily.HARDCODED_SECRET: 798,
    CanonicalFamily.SSL_VERIFICATION: 295,
    CanonicalFamily.WEAK_HASH: 327,
    CanonicalFamily.PATH_TRAVERSAL: 22,
    CanonicalFamily.OPEN_REDIRECT: 601,
    CanonicalFamily.XSS_DOM: 79,
    CanonicalFamily.EVAL_DYNAMIC_CODE: 95,
    CanonicalFamily.COMMAND_INJECTION: 78,
    CanonicalFamily.SQL_INJECTION: 89,
    CanonicalFamily.POSTMESSAGE_ORIGIN: 346,
}


@dataclass(slots=True)
class CanonicalSignature:
    normalized_file_path: str
    line: int | None
    cwe: int | None
    cwe_inferred: bool
    family: CanonicalFamily
    normalized_rule: str
    component_name: str
    component_version: str


@dataclass(slots=True)
class MatchScore:
    score: int
    verdict: MatchVerdict
    is_duplicate: bool


def normalize_file_path(file_path: str | None) -> str:
    value = (file_path or "").strip().replace("\\", "/")
    if not value:
        return ""
    while "//" in value:
        value = value.replace("//", "/")
    try:
        return str(PurePosixPath(value)).lower()
    except Exception:
        return value.lower()


def normalize_rule_key(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "_", raw)
    return normalized.strip("_")


def infer_canonical_family(*, vuln_id: str | None = None, title: str | None = None) -> CanonicalFamily:
    haystack = f"{vuln_id or ''} {title or ''}".strip()
    if not haystack:
        return CanonicalFamily.UNKNOWN
    for family, patterns in _FAMILY_PATTERNS.items():
        if any(pattern.search(haystack) for pattern in patterns):
            return family
    return CanonicalFamily.UNKNOWN


def cwe_for_family(family: CanonicalFamily) -> int | None:
    return _FAMILY_CWE.get(family)


def _normalize_cwe(cwe: Any) -> int | None:
    if cwe is None:
        return None
    try:
        value = int(cwe)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _as_non_negative_int(raw: Any, default: int) -> int:
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def canonical_scoring_thresholds() -> tuple[int, int]:
    auto_threshold = _as_non_negative_int(
        getattr(settings, "AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD", DEFAULT_AUTO_DUPLICATE_THRESHOLD),
        DEFAULT_AUTO_DUPLICATE_THRESHOLD,
    )
    candidate_threshold = _as_non_negative_int(
        getattr(settings, "AIST_CANONICAL_CANDIDATE_MIN_SCORE", DEFAULT_CANDIDATE_MIN_SCORE),
        DEFAULT_CANDIDATE_MIN_SCORE,
    )
    if auto_threshold > 0 and candidate_threshold >= auto_threshold:
        candidate_threshold = auto_threshold - 1
    return auto_threshold, candidate_threshold


def finding_signature(finding: Any) -> CanonicalSignature:
    family = infer_canonical_family(
        vuln_id=str(getattr(finding, "vuln_id_from_tool", "") or ""),
        title=str(getattr(finding, "title", "") or ""),
    )
    cwe = _normalize_cwe(getattr(finding, "cwe", None))
    cwe_inferred = cwe is None
    if cwe_inferred:
        cwe = cwe_for_family(family)
    normalized_rule = normalize_rule_key(
        str(getattr(finding, "vuln_id_from_tool", "") or getattr(finding, "title", "") or ""),
    )
    component_name = (str(getattr(finding, "component_name", "") or "")).strip().lower()
    component_version = (str(getattr(finding, "component_version", "") or "")).strip().lower()
    line = _normalize_cwe(getattr(finding, "line", None))
    return CanonicalSignature(
        normalized_file_path=normalize_file_path(getattr(finding, "file_path", "")),
        line=line,
        cwe=cwe,
        cwe_inferred=cwe_inferred,
        family=family,
        normalized_rule=normalized_rule,
        component_name=component_name,
        component_version=component_version,
    )


def score_signatures(left: CanonicalSignature, right: CanonicalSignature) -> MatchScore:
    if (
        not left.normalized_file_path
        or not right.normalized_file_path
        or left.line is None
        or right.line is None
        or left.normalized_file_path != right.normalized_file_path
        or left.line != right.line
    ):
        return MatchScore(score=0, verdict=MatchVerdict.NO_MATCH, is_duplicate=False)

    score = 0
    cwe_match = False
    rule_match = False
    component_match = False
    family_match = left.family == right.family and left.family != CanonicalFamily.UNKNOWN

    if left.cwe and right.cwe and left.cwe == right.cwe:
        if not left.cwe_inferred and not right.cwe_inferred:
            cwe_match = True
            score += 3
        elif left.cwe_inferred != right.cwe_inferred:
            cwe_match = True
            score += 2
    if family_match:
        score += 3
    if left.normalized_rule and right.normalized_rule and left.normalized_rule == right.normalized_rule:
        rule_match = True
        score += 2
    if (
        (left.component_name and right.component_name and left.component_name == right.component_name)
        or (
            left.component_version
            and right.component_version
            and left.component_version == right.component_version
        )
    ):
        component_match = True
        score += 1

    # Avoid candidate matches based only on inferred family classification.
    if (
        family_match
        and not cwe_match
        and not rule_match
        and not component_match
        and left.cwe_inferred
        and right.cwe_inferred
    ):
        return MatchScore(score=0, verdict=MatchVerdict.NO_MATCH, is_duplicate=False)

    auto_threshold, candidate_threshold = canonical_scoring_thresholds()
    if score >= auto_threshold:
        return MatchScore(score=score, verdict=MatchVerdict.DUPLICATE, is_duplicate=True)
    if candidate_threshold <= score < auto_threshold and score > 0:
        return MatchScore(score=score, verdict=MatchVerdict.CANDIDATE, is_duplicate=False)
    return MatchScore(score=score, verdict=MatchVerdict.NO_MATCH, is_duplicate=False)


def score_findings(left: Any, right: Any) -> MatchScore:
    return score_signatures(finding_signature(left), finding_signature(right))
