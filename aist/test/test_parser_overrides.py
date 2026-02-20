from django.test import SimpleTestCase
from dojo.tools import factory
from dojo.tools.bearer_cli.parser import BearerCLIParser
from dojo.tools.semgrep.parser import SemgrepParser
from dojo.tools.snyk_code.parser import SnykCodeParser

from aist.parser_overrides import (
    HumanizedBearerParser,
    HumanizedSemgrepParser,
    HumanizedSnykCodeParser,
    install_bearer_parser_override,
    install_semgrep_parser_override,
    install_snyk_code_parser_override,
    normalize_bearer_title,
)


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
