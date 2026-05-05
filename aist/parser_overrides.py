from __future__ import annotations

import logging
import re
import textwrap

from dojo.tools import factory
from dojo.tools.bearer_cli.parser import BearerCLIParser
from dojo.tools.generic.parser import GenericParser
from dojo.tools.horusec.parser import HorusecParser
from dojo.tools.sarif.parser import SarifParser
from dojo.tools.semgrep.parser import SemgrepParser
from dojo.tools.snyk_code.parser import SnykCodeParser

from aist.dedupe.canonical import (
    CanonicalFamily,
    cwe_for_family,
    infer_canonical_family,
    normalize_file_path,
    normalize_rule_key,
)

logger = logging.getLogger(__name__)

SNYK_CODE_SCAN_TYPE = "Snyk Code Scan"
SEMGREP_SCAN_TYPE = "Semgrep JSON Report"
HORUSEC_SCAN_TYPE = "Horusec Scan"
BEARER_SCAN_TYPE = "Bearer CLI"
# Claude agent-bridge analyzers register dedicated parsers (subclasses of
# GenericParser) so their findings carry distinct test_type / scan_type names
# and route through canonical dedupe like every other supported scanner.
# sast-pipeline's analyzers.yaml uses these as ``output_type``.
CLAUDE_DIFF_SECURITY_SCAN_TYPE = "Claude Diff Security"
CLAUDE_FULL_SECURITY_SCAN_TYPE = "Claude Full Security"
CLAUDE_LINE_BUCKET_SIZE = 5
SNYK_RULE_TITLE_OVERRIDES = {
    "OR": "Open Redirect Vulnerability",
}
HORUSEC_TITLE_PREFIX_PATTERN = re.compile(
    r"^\s*\(\s*\d+\s*/\s*\d+\s*\)\s*\*?\s*Possible\s+Vulnerability\s+Detected:\s*",
    flags=re.IGNORECASE,
)
HORUSEC_CWE_PATTERN = re.compile(r"CWE-(\d+)", flags=re.IGNORECASE)
BEARER_TITLE_LOCATION_SUFFIX_PATTERN = re.compile(
    r"\s+in\s+[\w./-]+:\d+\s*$",
    flags=re.IGNORECASE,
)


def _stabilize_dedupe_fields(finding, *, rule_hint: str | None = None):  # type: ignore[no-untyped-def]
    source_rule = rule_hint or str(getattr(finding, "vuln_id_from_tool", "") or getattr(finding, "title", "") or "")
    normalized_rule = normalize_rule_key(source_rule)
    if normalized_rule:
        finding.vuln_id_from_tool = normalized_rule
    family = infer_canonical_family(vuln_id=source_rule, title=str(getattr(finding, "title", "") or ""))
    family_cwe = cwe_for_family(family)
    current_cwe = getattr(finding, "cwe", None)
    if not current_cwe and family_cwe:
        finding.cwe = family_cwe


def _is_missing_cwe(value: object) -> bool:
    if value is None:
        return True
    try:
        return int(value) <= 0
    except (TypeError, ValueError):
        return True


def extract_horusec_cwe(details: str | None) -> int | None:
    if not details:
        return None
    match = HORUSEC_CWE_PATTERN.search(details)
    if not match:
        return None
    try:
        cwe = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return cwe if cwe > 0 else None


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

    def customize_finding(self, finding, result, rule, location):  # type: ignore[no-untyped-def]
        super().customize_finding(finding, result, rule, location)
        rule_id = str(result.get("ruleId", "") or "")
        _stabilize_dedupe_fields(finding, rule_hint=rule_id)


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
            _stabilize_dedupe_fields(finding, rule_hint=check_id)
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
        details = str(((data or {}).get("vulnerabilities") or {}).get("details") or "")
        extracted_cwe = extract_horusec_cwe(details)
        if _is_missing_cwe(getattr(finding, "cwe", None)) and extracted_cwe:
            finding.cwe = extracted_cwe
        finding.title = normalize_horusec_title(finding.title)
        _stabilize_dedupe_fields(finding, rule_hint=finding.title)
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
            _stabilize_dedupe_fields(finding, rule_hint=str(finding.vuln_id_from_tool or finding.title or ""))
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


def build_claude_vuln_id_from_tool(
    *,
    cwe: int | None,
    file_path: str | None,
    line: int | None,
    title: str | None,
    raw_vuln_id: str | None = None,
) -> str:
    """
    Deterministic dedupe-friendly token for Claude analyzer findings.

    Format: ``claude:{cwe}:{normalized_file_path}:{line_bucket}:{family}``.
    ``line_bucket = line // CLAUDE_LINE_BUCKET_SIZE`` collapses small line
    drift across runs (the LLM occasionally re-anchors to a slightly
    different line of the same hunk). This token is used for the standard
    DefectDojo fallback hash dedup so it converges across runs even when the
    LLM paraphrases the title or shifts the anchor by a few lines.
    """
    cwe_token = str(cwe) if (isinstance(cwe, int) and cwe > 0) else "0"
    path_token = normalize_file_path(file_path) or "unknown"
    bucket = line // CLAUDE_LINE_BUCKET_SIZE if isinstance(line, int) and line > 0 else 0
    family = infer_canonical_family(vuln_id=raw_vuln_id, title=title)
    family_token = family.value if family != CanonicalFamily.UNKNOWN else CanonicalFamily.UNKNOWN.value
    return f"claude:{cwe_token}:{path_token}:{bucket}:{family_token}"


def _stabilize_claude_finding(finding) -> None:  # type: ignore[no-untyped-def]
    raw_vuln_id = str(getattr(finding, "vuln_id_from_tool", "") or "")
    title = str(getattr(finding, "title", "") or "")
    cwe = getattr(finding, "cwe", None)
    if not (isinstance(cwe, int) and cwe > 0):
        family = infer_canonical_family(vuln_id=raw_vuln_id, title=title)
        family_cwe = cwe_for_family(family)
        if family_cwe:
            finding.cwe = family_cwe
            cwe = family_cwe
    finding.vuln_id_from_tool = build_claude_vuln_id_from_tool(
        cwe=cwe if isinstance(cwe, int) else None,
        file_path=getattr(finding, "file_path", None),
        line=getattr(finding, "line", None),
        title=title,
        raw_vuln_id=raw_vuln_id,
    )


class _ClaudeGenericParserBase(GenericParser):

    """
    Generic-format parser for Claude agent-bridge analyzers.

    Inherits the JSON/CSV reading logic from ``GenericParser`` but reports a
    distinct ``ID`` so the imported Test gets a Claude-specific
    ``test_type``. Findings are stabilized in place so dedupe keys are
    reproducible across runs (paraphrased titles / drifting line anchors).
    """

    ID: str = ""

    def get_scan_types(self):
        return [self.ID]

    def get_label_for_scan_types(self, scan_type):
        return scan_type

    def get_description_for_scan_types(self, scan_type):
        return (
            "Generic Findings Import emitted by the Claude agent-bridge "
            "analyzer. Findings carry a deterministic vuln_id_from_tool so "
            "the standard DefectDojo dedup converges across runs."
        )

    def get_findings(self, filename, test):  # type: ignore[no-untyped-def]
        findings = super().get_findings(filename, test)
        for finding in findings:
            _stabilize_claude_finding(finding)
        return findings

    def get_tests(self, scan_type, filename):  # type: ignore[no-untyped-def]
        tests = super().get_tests(scan_type, filename)
        for parser_test in tests:
            # GenericJSONParser defaults ParserTest.type to its own ID
            # ("Generic Findings Import"). Reassign so consolidate_dynamic_tests
            # in base_importer keeps our Claude scan_type as the final
            # test_type_name (otherwise it produces "Generic Findings Import
            # Scan (Claude Diff Security)").
            parser_test.type = scan_type
            for finding in getattr(parser_test, "findings", None) or []:
                _stabilize_claude_finding(finding)
        return tests


class ClaudeDiffSecurityParser(_ClaudeGenericParserBase):
    ID = CLAUDE_DIFF_SECURITY_SCAN_TYPE


class ClaudeFullSecurityParser(_ClaudeGenericParserBase):
    ID = CLAUDE_FULL_SECURITY_SCAN_TYPE


def install_claude_parsers() -> None:
    for scan_type, parser_cls in (
        (CLAUDE_DIFF_SECURITY_SCAN_TYPE, ClaudeDiffSecurityParser),
        (CLAUDE_FULL_SECURITY_SCAN_TYPE, ClaudeFullSecurityParser),
    ):
        if isinstance(factory.PARSERS.get(scan_type), parser_cls):
            continue
        factory.PARSERS[scan_type] = parser_cls()
        logger.info("Registered Claude analyzer parser for '%s'", scan_type)
