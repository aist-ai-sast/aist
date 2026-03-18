from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.views.debug import get_exception_reporter_filter

_FILTER = get_exception_reporter_filter(None)
MASKED_VALUE = _FILTER.cleansed_substitute
_BARE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"glpat-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ASIA[0-9A-Z]{16}"
    r")(?![A-Za-z0-9])",
)


def _is_sensitive_key(key: str) -> bool:
    return _FILTER.cleanse_setting(key, "value") == MASKED_VALUE


def _mask_url_token(url_value: str) -> str:
    try:
        parsed = urlsplit(url_value)
    except Exception:
        return url_value
    if not parsed.scheme:
        return url_value

    netloc = parsed.netloc
    if "@" in netloc:
        auth_part, host_part = netloc.rsplit("@", 1)
        username, sep, _password = auth_part.partition(":")
        if sep:
            netloc = f"{username}:{MASKED_VALUE}@{host_part}"

    masked_query_parts = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        masked_query_parts.append((key, MASKED_VALUE if _is_sensitive_key(key) else value))
    query = urlencode(masked_query_parts, doseq=True)

    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _mask_kv_line(text: str, separator: str) -> str:
    if separator not in text:
        return text

    if separator == ":":
        pattern = re.compile(r"(?P<key>\b[A-Za-z0-9_.-]+\b)\s*:\s*(?P<value>\S+)")

        def replace_colon(match: re.Match[str]) -> str:
            key = match.group("key")
            if not _is_sensitive_key(key):
                return match.group(0)
            return f"{key}: {MASKED_VALUE}"

        return pattern.sub(replace_colon, text)

    pattern = re.compile(r"(?P<key>\b[A-Za-z0-9_.-]+\b)=(?P<value>\S+)")

    def replace_equals(match: re.Match[str]) -> str:
        key = match.group("key")
        if not _is_sensitive_key(key):
            return match.group(0)
        return f"{key}={MASKED_VALUE}"

    return pattern.sub(replace_equals, text)


def _mask_urls_in_line(line: str) -> str:
    if "://" not in line:
        return line

    url_pattern = re.compile(r"https?://[^\s\"'<>]+")
    return url_pattern.sub(lambda m: _mask_url_token(m.group(0)), line)


def _mask_bare_tokens_in_line(line: str) -> str:
    return _BARE_TOKEN_PATTERN.sub(MASKED_VALUE, line)


def mask_sensitive_text(text: str) -> str:
    if not text:
        return text

    masked_lines: list[str] = []
    for line in text.splitlines():
        masked_line = _mask_urls_in_line(line)
        masked_line = _mask_kv_line(masked_line, ":")
        masked_line = _mask_kv_line(masked_line, "=")
        masked_line = _mask_bare_tokens_in_line(masked_line)
        masked_lines.append(masked_line)
    return "\n".join(masked_lines)


# Keys that look sensitive by Django's heuristics but are NOT secrets.
# "key" — generic dict key; "external_key" — issue tracker reference (e.g. PROJ-42);
# "external_id" / "external_url" — issue tracker identifiers, not credentials.
_NON_SENSITIVE_KEYS: frozenset[str] = frozenset({"key", "external_key", "external_id", "external_url"})


def mask_sensitive_data(value: dict | list | tuple | str | None) -> dict | list | tuple | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, Mapping):
        masked: dict = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)) and str(key).lower() not in _NON_SENSITIVE_KEYS:
                masked[key] = MASKED_VALUE
            else:
                masked[key] = mask_sensitive_data(item)
        return masked
    if isinstance(value, tuple):
        return tuple(mask_sensitive_data(item) for item in value)
    if isinstance(value, list):
        return [mask_sensitive_data(item) for item in value]
    return value


class SensitiveLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_sensitive_text(record.msg)
        else:
            record.msg = mask_sensitive_data(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(mask_sensitive_text(arg) if isinstance(arg, str) else mask_sensitive_data(arg) for arg in record.args)
            else:
                record.args = mask_sensitive_data(record.args)
        return True


def get_sensitive_log_filter() -> logging.Filter:
    return SensitiveLogFilter()
