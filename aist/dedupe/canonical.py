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
    MISSING_AUTHENTICATION = "missing_authentication"
    CORS_MISCONFIG = "cors_misconfig"
    SSRF = "ssrf"
    HOST_HEADER_INJECTION = "host_header_injection"
    UNKNOWN = "unknown"


class MatchVerdict(StrEnum):
    DUPLICATE = "duplicate"
    CANDIDATE = "candidate"
    NO_MATCH = "no_match"


DEFAULT_AUTO_DUPLICATE_THRESHOLD = 4
DEFAULT_CANDIDATE_MIN_SCORE = 2
SCORE_CWE_EXPLICIT_MATCH = 3
SCORE_CWE_MIXED_CONFIDENCE_MATCH = 2
SCORE_FAMILY_MATCH = 3
SCORE_RULE_MATCH = 2
SCORE_COMPONENT_MATCH = 1
SCORE_TITLE_TOKEN_OVERLAP = 1
TITLE_TOKEN_MIN_OVERLAP = 3
TITLE_TOKEN_MIN_JACCARD = 0.4
_TITLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_TITLE_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "or", "the", "is", "are", "was", "were", "be", "been",
    "being", "to", "of", "in", "on", "for", "with", "without", "by", "as",
    "at", "from", "into", "via", "this", "that", "these", "those", "it",
    "its", "their", "his", "her", "our", "your", "any", "some", "no", "not",
    "do", "does", "did", "has", "have", "had", "having",
    "should", "will", "would", "than", "then", "but", "if", "else", "when",
    "while", "after", "before", "during", "between", "within", "across",
    "over", "under", "above", "below", "up", "down", "off", "out", "very",
    # SAST jargon and noise tokens
    "vulnerability", "vulnerabilities", "issue", "issues", "finding",
    "findings", "warning", "warnings", "alert", "alerts", "security",
    "detected", "detect", "detection", "exposed", "exposes", "exposure",
    "potential", "possible", "may", "could", "service", "services",
    "endpoint", "endpoints", "code", "scan", "scanner", "tool",
    "context", "report", "result", "results",
})


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
        re.compile(r"hardcodednoncryptosecret", re.IGNORECASE),
        re.compile(r"detected[_\s-]?secret", re.IGNORECASE),
        re.compile(r"detected[_\s-]?jwt[_\s-]?token", re.IGNORECASE),
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
    CanonicalFamily.MISSING_AUTHENTICATION: (
        re.compile(r"(no|missing|without|absent|lack(?:s|ing)?\s+of)\s+(authentication|authn|auth)\b", re.IGNORECASE),
        re.compile(r"\bunauthenticated\b", re.IGNORECASE),
        re.compile(r"accessible\s+without\s+(auth|authentication)", re.IGNORECASE),
        re.compile(r"endpoints?\s+(are\s+)?exposed", re.IGNORECASE),
        re.compile(r"endpoints?\s+(have|expose)\s+no\s+(auth|authentication)", re.IGNORECASE),
        re.compile(r"\bno\s+authn\b", re.IGNORECASE),
        re.compile(r"missing[_\s-]?auth(?:entication)?[_\s-]?check", re.IGNORECASE),
    ),
    CanonicalFamily.CORS_MISCONFIG: (
        re.compile(r"\bcors\b", re.IGNORECASE),
        re.compile(r"origin\s+(reflection|reflects|wildcard)", re.IGNORECASE),
        re.compile(r"access[-_\s]control[-_\s]allow[-_\s]origin", re.IGNORECASE),
        re.compile(r"credentials.*origin|origin.*credentials", re.IGNORECASE),
        re.compile(r"cross[-_\s]origin\s+(misconfig|policy|resource)", re.IGNORECASE),
    ),
    CanonicalFamily.SSRF: (
        re.compile(r"\bssrf\b", re.IGNORECASE),
        re.compile(r"server[-_\s]side\s+request\s+forgery", re.IGNORECASE),
        re.compile(r"(host[-_\s]header|webhook|user[-_\s]controlled\s+url).*request", re.IGNORECASE),
    ),
    CanonicalFamily.HOST_HEADER_INJECTION: (
        re.compile(r"host\s+header\s+(injection|impersonation|bypass|spoof(?:ing)?)", re.IGNORECASE),
        re.compile(r"\bhost[-_\s]header[-_\s]attack", re.IGNORECASE),
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
    CanonicalFamily.MISSING_AUTHENTICATION: 306,
    CanonicalFamily.CORS_MISCONFIG: 942,
    CanonicalFamily.SSRF: 918,
    CanonicalFamily.HOST_HEADER_INJECTION: 644,
}
_HARDCODED_SECRET_RULE_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"javascript_hardcodednoncryptosecret", re.IGNORECASE),
        "secret_jwt_or_noncrypto_hardcoded",
    ),
    (
        re.compile(r"generic[_\s-]?secrets[_\s-]?security[_\s-]?detected[_\s-]?jwt[_\s-]?token", re.IGNORECASE),
        "secret_jwt_or_noncrypto_hardcoded",
    ),
)


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
    title_tokens: frozenset[str] = frozenset()


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


def normalize_canonical_rule_key(*, family: CanonicalFamily, value: str | None) -> str:
    # First normalize raw rule id/title to a stable token format.
    normalized_rule = normalize_rule_key(value)
    if not normalized_rule:
        return ""
    # Then apply family-specific aliases for known cross-scanner equivalents.
    if family == CanonicalFamily.HARDCODED_SECRET:
        for pattern, alias in _HARDCODED_SECRET_RULE_ALIASES:
            if pattern.search(normalized_rule):
                return alias
    return normalized_rule


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


def tokenize_title(*texts: str | None) -> frozenset[str]:
    tokens: set[str] = set()
    for text in texts:
        if not text:
            continue
        for raw in _TITLE_TOKEN_PATTERN.findall(text.lower()):
            if len(raw) <= 2 or raw in _TITLE_STOPWORDS:
                continue
            tokens.add(raw)
    return frozenset(tokens)


def title_tokens_overlap_score(left: frozenset[str], right: frozenset[str]) -> int:
    if not left or not right:
        return 0
    intersection = left & right
    if len(intersection) < TITLE_TOKEN_MIN_OVERLAP:
        return 0
    union = left | right
    if not union:
        return 0
    if (len(intersection) / len(union)) < TITLE_TOKEN_MIN_JACCARD:
        return 0
    return SCORE_TITLE_TOKEN_OVERLAP


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


def _component_evidence_matches(left: CanonicalSignature, right: CanonicalSignature) -> bool:
    return (
        left.component_name
        and right.component_name
        and left.component_name == right.component_name
    ) or (
        left.component_version
        and right.component_version
        and left.component_version == right.component_version
    )


def finding_signature(finding: Any) -> CanonicalSignature:
    # Build a normalized, scanner-agnostic signature used by score_signatures().
    title = str(getattr(finding, "title", "") or "")
    family = infer_canonical_family(
        vuln_id=str(getattr(finding, "vuln_id_from_tool", "") or ""),
        title=title,
    )
    cwe = _normalize_cwe(getattr(finding, "cwe", None))
    cwe_inferred = cwe is None
    if cwe_inferred:
        cwe = cwe_for_family(family)
    normalized_rule = normalize_canonical_rule_key(
        family=family,
        value=str(getattr(finding, "vuln_id_from_tool", "") or getattr(finding, "title", "") or ""),
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
        title_tokens=tokenize_title(title),
    )


def score_signatures(left: CanonicalSignature, right: CanonicalSignature) -> MatchScore:
    # Hard gate: canonical dedupe only compares findings on the same path and line.
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

    # Evidence scoring is additive; final verdict is decided by thresholds.
    if left.cwe and right.cwe and left.cwe == right.cwe:
        if not left.cwe_inferred and not right.cwe_inferred:
            cwe_match = True
            score += SCORE_CWE_EXPLICIT_MATCH
        elif left.cwe_inferred != right.cwe_inferred:
            cwe_match = True
            score += SCORE_CWE_MIXED_CONFIDENCE_MATCH
    if family_match:
        score += SCORE_FAMILY_MATCH
    if left.normalized_rule and right.normalized_rule and left.normalized_rule == right.normalized_rule:
        rule_match = True
        score += SCORE_RULE_MATCH
    if _component_evidence_matches(left, right):
        component_match = True
        score += SCORE_COMPONENT_MATCH

    # Title-token overlap is a content-based corroboration signal. It is bounded
    # by Jaccard >= TITLE_TOKEN_MIN_JACCARD AND >= TITLE_TOKEN_MIN_OVERLAP shared
    # content tokens — strict enough that random title noise does not score.
    title_overlap = title_tokens_overlap_score(left.title_tokens, right.title_tokens)
    score += title_overlap

    # Avoid candidate matches based only on inferred family classification.
    if (
        family_match
        and not cwe_match
        and not rule_match
        and not component_match
        and not title_overlap
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
