from __future__ import annotations

import logging
import re
import textwrap

from dojo.tools import factory
from dojo.tools.bearer_cli.parser import BearerCLIParser
from dojo.tools.horusec.parser import HorusecParser
from dojo.tools.sarif.parser import SarifParser
from dojo.tools.semgrep.parser import SemgrepParser
from dojo.tools.snyk_code.parser import SnykCodeParser

logger = logging.getLogger(__name__)

SNYK_CODE_SCAN_TYPE = "Snyk Code Scan"
SEMGREP_SCAN_TYPE = "Semgrep JSON Report"
HORUSEC_SCAN_TYPE = "Horusec Scan"
BEARER_SCAN_TYPE = "Bearer CLI"
SNYK_RULE_TITLE_OVERRIDES = {
    "OR": "Open Redirect Vulnerability",
}
HORUSEC_TITLE_PREFIX_PATTERN = re.compile(
    r"^\s*\(\s*\d+\s*/\s*\d+\s*\)\s*\*?\s*Possible\s+Vulnerability\s+Detected:\s*",
    flags=re.IGNORECASE,
)
BEARER_TITLE_LOCATION_SUFFIX_PATTERN = re.compile(
    r"\s+in\s+[\w./-]+:\d+\s*$",
    flags=re.IGNORECASE,
)


class HumanizedSnykCodeParser(SnykCodeParser):
    @staticmethod
    def _split_camel_case(value: str) -> str:
        normalized = value.replace("_", " ").replace("-", " ").strip()
        if not normalized:
            return normalized
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", normalized)

    @classmethod
    def _humanize_rule_id(cls, rule_id: str) -> str:
        parts = [part.strip() for part in rule_id.split("/") if part.strip()]
        if not parts:
            return ""

        candidate = parts[-1]
        if candidate.lower() in {"test", "check", "rule", "default"} and len(parts) >= 2:
            candidate = parts[-2]

        override_title = SNYK_RULE_TITLE_OVERRIDES.get(candidate.upper())
        if override_title:
            return override_title

        return cls._split_camel_case(candidate)

    def _build_short_title(self, result: dict, rule: dict | None) -> str:
        if rule:
            short_description = ((rule.get("shortDescription") or {}).get("text") or "").strip()
            if short_description:
                return short_description
            rule_name = str(rule.get("name", "")).strip()
            if rule_name:
                return rule_name

        rule_id = str(result.get("ruleId", "")).strip()
        if rule_id:
            humanized = self._humanize_rule_id(rule_id)
            if humanized:
                return humanized
        if rule_id:
            return rule_id

        return textwrap.shorten(SarifParser.get_finding_title(self, result, rule, None), 80)

    def get_finding_title(self, result: dict, rule: dict | None, location) -> str:  # type: ignore[no-untyped-def]
        return self._build_short_title(result, rule)


def install_snyk_code_parser_override() -> None:
    current_parser = factory.PARSERS.get(SNYK_CODE_SCAN_TYPE)
    if current_parser is None:
        logger.warning("Parser '%s' is not registered; skip override", SNYK_CODE_SCAN_TYPE)
        return
    if isinstance(current_parser, HumanizedSnykCodeParser):
        return

    factory.PARSERS[SNYK_CODE_SCAN_TYPE] = HumanizedSnykCodeParser()
    logger.info("Installed humanized parser override for '%s'", SNYK_CODE_SCAN_TYPE)


def _humanize_semgrep_rule(rule_id: str) -> str:
    if not rule_id:
        return "Semgrep finding"
    tail = rule_id.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail)
    normalized = normalized.replace("-", " ").replace("_", " ").strip()
    if not normalized:
        return "Semgrep finding"
    return normalized.title()


def build_semgrep_humanized_title(*, check_id: str, file_path: str | None, line: int | None) -> str:
    _ = (file_path, line)
    return _humanize_semgrep_rule(check_id)


class HumanizedSemgrepParser(SemgrepParser):
    def get_findings(self, filename, test):  # type: ignore[no-untyped-def]
        findings = super().get_findings(filename, test)
        for finding in findings:
            check_id = str(finding.vuln_id_from_tool or finding.title or "")
            finding.title = build_semgrep_humanized_title(
                check_id=check_id,
                file_path=finding.file_path,
                line=finding.line,
            )
        return findings


def install_semgrep_parser_override() -> None:
    current_parser = factory.PARSERS.get(SEMGREP_SCAN_TYPE)
    if current_parser is None:
        logger.warning("Parser '%s' is not registered; skip override", SEMGREP_SCAN_TYPE)
        return
    if isinstance(current_parser, HumanizedSemgrepParser):
        return

    factory.PARSERS[SEMGREP_SCAN_TYPE] = HumanizedSemgrepParser()
    logger.info("Installed humanized parser override for '%s'", SEMGREP_SCAN_TYPE)


def normalize_horusec_title(raw_title: str) -> str:
    title = HORUSEC_TITLE_PREFIX_PATTERN.sub("", raw_title or "")
    title = title.strip()
    return title or "Possible Vulnerability Detected"


class HumanizedHorusecParser(HorusecParser):
    def _get_finding(self, data, date):  # type: ignore[no-untyped-def]
        finding = super()._get_finding(data, date)
        finding.title = normalize_horusec_title(finding.title)
        return finding


def install_horusec_parser_override() -> None:
    current_parser = factory.PARSERS.get(HORUSEC_SCAN_TYPE)
    if current_parser is None:
        logger.warning("Parser '%s' is not registered; skip override", HORUSEC_SCAN_TYPE)
        return
    if isinstance(current_parser, HumanizedHorusecParser):
        return

    factory.PARSERS[HORUSEC_SCAN_TYPE] = HumanizedHorusecParser()
    logger.info("Installed humanized parser override for '%s'", HORUSEC_SCAN_TYPE)


def normalize_bearer_title(raw_title: str) -> str:
    title = BEARER_TITLE_LOCATION_SUFFIX_PATTERN.sub("", raw_title or "")
    title = title.strip()
    return title or "Bearer finding"


class HumanizedBearerParser(BearerCLIParser):
    def get_findings(self, file, test):  # type: ignore[no-untyped-def]
        findings = super().get_findings(file, test)
        for finding in findings:
            finding.title = normalize_bearer_title(finding.title)
        return findings


def install_bearer_parser_override() -> None:
    current_parser = factory.PARSERS.get(BEARER_SCAN_TYPE)
    if current_parser is None:
        logger.warning("Parser '%s' is not registered; skip override", BEARER_SCAN_TYPE)
        return
    if isinstance(current_parser, HumanizedBearerParser):
        return

    factory.PARSERS[BEARER_SCAN_TYPE] = HumanizedBearerParser()
    logger.info("Installed humanized parser override for '%s'", BEARER_SCAN_TYPE)
