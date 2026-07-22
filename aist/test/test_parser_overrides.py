import io
import json
from datetime import datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from dojo.tools import factory
from dojo.tools.bearer_cli.parser import BearerCLIParser
from dojo.tools.horusec.parser import HorusecParser
from dojo.tools.semgrep.parser import SemgrepParser
from dojo.tools.snyk_code.parser import SnykCodeParser

from aist.parser_overrides import (
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
