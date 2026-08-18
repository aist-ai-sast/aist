import io
import json
from datetime import datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from dojo.tools import factory
from dojo.tools.bearer_cli.parser import BearerCLIParser
from dojo.tools.generic.json_parser import GenericJSONParser
from dojo.tools.horusec.parser import HorusecParser
from dojo.tools.semgrep.parser import SemgrepParser
from dojo.tools.snyk_code.parser import SnykCodeParser

from aist.parser_overrides import (
    DAST_SCAN_TYPE,
    PLATFORM_FINDING_FIELDS,
    DastReportParser,
    HumanizedBearerParser,
    HumanizedHorusecParser,
    HumanizedSemgrepParser,
    HumanizedSnykCodeParser,
    extract_horusec_cwe,
    install_bearer_parser_override,
    install_semgrep_parser_override,
    install_snyk_code_parser_override,
    normalize_bearer_title,
)


class DastReportParserSourceCommitsTests(SimpleTestCase):
    def _file(self, payload: dict) -> io.BytesIO:
        return io.BytesIO(json.dumps(payload).encode())

    def test_returns_source_commits_by_repo(self):
        payload = {"dast_run_metadata": {"source_commits": {
            "cloud_portal": "fd5b25aa1234567890abcdef1234567890abcdef",
        }}}
        result = DastReportParser().extract_source_commits(self._file(payload))
        self.assertEqual(result, {"cloud_portal": "fd5b25aa1234567890abcdef1234567890abcdef"})

    def test_returns_empty_dict_when_metadata_is_absent(self):
        result = DastReportParser().extract_source_commits(self._file({}))
        self.assertEqual(result, {})

    def test_leaves_the_file_position_at_the_start(self):
        payload = {"dast_run_metadata": {"source_commits": {"cloud_portal": "a" * 40}}}
        file_obj = self._file(payload)
        DastReportParser().extract_source_commits(file_obj)
        self.assertEqual(file_obj.tell(), 0)


class HumanizedSnykCodeParserTests(SimpleTestCase):
    def test_title_uses_humanized_message_with_file_and_line(self):
        parser = HumanizedSnykCodeParser()
        result = {
            "ruleId": "cpp/BufferOverflow",
            "message": {"text": "Potential SQL injection in query builder"},
        }
        rule = {
            "name": "BufferOverflow",
            "shortDescription": {"text": "Buffer Overflow"},
        }
        location = {
            "physicalLocation": {
                "artifactLocation": {"uri": "src/api/user_query.py"},
                "region": {"startLine": 42},
            },
        }

        title = parser.get_finding_title(result=result, rule=rule, location=location)

        self.assertEqual(title, "Buffer Overflow")

    def test_install_replaces_default_snyk_code_parser(self):
        original_parsers = factory.PARSERS.copy()
        self.addCleanup(lambda: factory.PARSERS.clear() or factory.PARSERS.update(original_parsers))
        factory.PARSERS[SnykCodeParser().get_scan_types()[0]] = SnykCodeParser()

        install_snyk_code_parser_override()

        self.assertIsInstance(factory.PARSERS["Snyk Code Scan"], HumanizedSnykCodeParser)

    def test_short_title_maps_or_rule_to_open_redirect(self):
        parser = HumanizedSnykCodeParser()
        result = {
            "ruleId": "javascript/OR",
            "message": {"text": "OR message"},
        }
        location = {
            "physicalLocation": {
                "artifactLocation": {"uri": "web/router.js"},
                "region": {"startLine": 12},
            },
        }

        title = parser.get_finding_title(result=result, rule=None, location=location)

        self.assertEqual(title, "Open Redirect Vulnerability")

    def test_short_title_uses_meaningful_parent_segment_instead_of_test(self):
        parser = HumanizedSnykCodeParser()
        result = {
            "ruleId": "python/NoHardcodedPasswords/test",
            "message": {"text": "No hardcoded passwords"},
        }
        location = {
            "physicalLocation": {
                "artifactLocation": {"uri": "security/config.py"},
                "region": {"startLine": 7},
            },
        }

        title = parser.get_finding_title(result=result, rule=None, location=location)

        self.assertEqual(title, "No Hardcoded Passwords")

    def test_install_replaces_default_semgrep_parser(self):
        original_parsers = factory.PARSERS.copy()
        self.addCleanup(lambda: factory.PARSERS.clear() or factory.PARSERS.update(original_parsers))
        factory.PARSERS[SemgrepParser().get_scan_types()[0]] = SemgrepParser()

        install_semgrep_parser_override()

        self.assertIsInstance(factory.PARSERS["Semgrep JSON Report"], HumanizedSemgrepParser)

    def test_install_replaces_default_bearer_parser(self):
        original_parsers = factory.PARSERS.copy()
        self.addCleanup(lambda: factory.PARSERS.clear() or factory.PARSERS.update(original_parsers))
        factory.PARSERS[BearerCLIParser().get_scan_types()[0]] = BearerCLIParser()

        install_bearer_parser_override()

        self.assertIsInstance(factory.PARSERS["Bearer CLI"], HumanizedBearerParser)

    def test_normalize_bearer_title_removes_location_suffix(self):
        raw = "Unsanitized User Input in Dynamic HTML Insertion (XSS) in src/app/layout.tsx:112"
        self.assertEqual(
            normalize_bearer_title(raw),
            "Unsanitized User Input in Dynamic HTML Insertion (XSS)",
        )

    def test_extract_horusec_cwe_returns_first_match(self):
        details = "warning text CWE-489 and then CWE-338 in same details"
        self.assertEqual(extract_horusec_cwe(details), 489)

    def test_horusec_parser_sets_cwe_from_details_when_missing(self):
        parser = HumanizedHorusecParser()
        data = {
            "vulnerabilities": {
                "details": "(1/1) * Possible Vulnerability Detected: Debug enabled CWE-489",
                "language": "python",
                "code": "DEBUG=True",
                "severity": "HIGH",
                "file": "settings.py",
                "confidence": "HIGH",
                "line": "10",
            },
        }

        finding = parser._get_finding(data, datetime(2026, 1, 1))

        self.assertEqual(finding.cwe, 489)

    def test_horusec_parser_does_not_override_existing_cwe(self):
        parser = HumanizedHorusecParser()
        data = {
            "vulnerabilities": {
                "details": "any details CWE-489",
            },
        }

        existing = type(
            "FindingStub",
            (),
            {"title": "(1/1) * Possible Vulnerability Detected: Debug enabled", "cwe": 295, "vuln_id_from_tool": ""},
        )()
        with patch.object(HorusecParser, "_get_finding", return_value=existing):
            finding = parser._get_finding(data, datetime(2026, 1, 1))

        self.assertEqual(finding.cwe, 295)


class PlatformFindingFieldsTests(SimpleTestCase):

    """
    `PLATFORM_FINDING_FIELDS` mirrors a set the vendor parser keeps private.

    A mirror is only safe if a vendor change breaks a test rather than drifting quietly, so every
    field is checked against what the vendor actually accepts — not against a copy of its source.
    """

    REQUIRED = {"title": "t", "severity": "High", "description": "d"}

    def _vendor_accepts(self, finding: dict) -> bool:
        try:
            GenericJSONParser()._get_test_json({"findings": [dict(finding)]})
        except (ValueError, TypeError):
            return False
        return True

    def test_every_declared_field_is_one_the_vendor_parser_accepts(self):
        """Too wide would mean handing the vendor a field it still refuses — import fails again."""
        for field in sorted(PLATFORM_FINDING_FIELDS):
            with self.subTest(field=field):
                self.assertTrue(
                    self._vendor_accepts({**self.REQUIRED, field: self._sample_for(field)}),
                    f"{field} is declared readable but the vendor parser refuses it",
                )

    def test_a_field_outside_the_declaration_is_one_the_vendor_parser_refuses(self):
        """Too narrow would mean silently dropping a field the platform could have stored."""
        self.assertFalse(self._vendor_accepts({**self.REQUIRED, "not_a_platform_field": "x"}))

    def test_the_required_fields_are_part_of_the_declaration(self):
        self.assertTrue(set(self.REQUIRED).issubset(PLATFORM_FINDING_FIELDS))

    @staticmethod
    def _sample_for(field: str):
        # Shapes the vendor parser coerces or forwards to the Finding model; the point of the test
        # is the field name being accepted, so each value only has to be of a workable type.
        if field in {"date", "mitigated", "publish_date", "planned_remediation_date", "kev_date"}:
            return "2026-08-17"
        if field in {"endpoints", "vulnerability_ids"}:
            return []
        if field == "files":
            return []
        if field == "tags":
            return ["dast"]
        if field in {"line", "cwe", "nb_occurences", "sast_source_line", "thread_id", "scanner_confidence"}:
            return 1
        if field in {
            "active", "verified", "false_p", "out_of_scope", "risk_accepted", "under_review",
            "is_mitigated", "static_finding", "dynamic_finding", "known_exploited", "ransomware_used",
            "fix_available",
        }:
            return True
        if field in {"cvssv3_score", "cvssv4_score", "epss_score", "epss_percentile"}:
            return 1.0
        return "x"


class DastReportParserUnreadFindingFieldsTests(SimpleTestCase):

    """A field the DAST side adds ahead of AIST must not cost the report its findings."""

    def _tests(self, findings: list[dict]):
        payload = {"name": "DAST", "type": DAST_SCAN_TYPE, "findings": findings}
        return DastReportParser().get_tests(DAST_SCAN_TYPE, io.BytesIO(json.dumps(payload).encode()))

    def test_a_finding_carrying_an_unmodelled_field_still_parses(self):
        tests = self._tests([{
            "title": "Cross-tenant object access",
            "severity": "High",
            "description": "redacted",
            "confidence": "high",
            "detector_version": "2026.8",
        }])

        self.assertEqual(len(tests[0].findings), 1)
        self.assertEqual(tests[0].findings[0].title, "Cross-tenant object access")

    def test_the_fields_the_platform_does_model_survive(self):
        tests = self._tests([{
            "title": "t",
            "severity": "High",
            "description": "d",
            "cwe": 306,
            "unique_id_from_tool": "REG-1",
            "unmodelled": "dropped",
        }])

        finding = tests[0].findings[0]
        self.assertEqual(finding.cwe, 306)
        self.assertEqual(finding.unique_id_from_tool, "REG-1")

    def test_a_finding_missing_a_required_field_still_fails(self):
        with self.assertRaises(ValueError):
            self._tests([{"title": "no severity or description"}])

    def test_the_callers_report_object_is_not_mutated(self):
        payload = {
            "name": "DAST",
            "type": DAST_SCAN_TYPE,
            "findings": [{"title": "t", "severity": "High", "description": "d", "tags": ["dast"], "extra": 1}],
        }
        DastReportParser()._readable_report(payload)

        self.assertEqual(payload["findings"][0]["tags"], ["dast"])
        self.assertEqual(payload["findings"][0]["extra"], 1)
