"""
Unit tests for the DAST analyzer parser registration.

The `dast` analyzer (an external-triggered autonomous DAST run via the integration
gateway) registers a dedicated parser subclassing `GenericParser` — same
`_SubtypedGenericParserBase` pattern the claude-* agent-bridge analyzers use — so the
imported `Test` carries a DAST-specific `test_type` and participates in canonical
dedupe instead of falling into the generic "Generic Findings Import" bucket, which
`aist/dedupe/custom.py`'s SUPPORTED_SCAN_TYPES allowlist does not cover at all.

Unlike Claude, DAST's `unique_id_from_tool`/`vuln_id_from_tool` are already
deterministic (check_id+title based — see the DAST repo's
dast/engine/dastlib/aist_export.py), so no finding-stabilization pass is needed here.
"""
from __future__ import annotations

import io
import json

from django.conf import settings
from django.test import SimpleTestCase
from dojo.tools import factory

from aist.dedupe.custom import SUPPORTED_SCAN_TYPES
from aist.parser_overrides import (
    DAST_SCAN_TYPE,
    DastReportParser,
    install_dast_parser,
)


def _dast_findings_payload() -> bytes:
    return json.dumps({
        "name": "DAST",
        "type": "DAST Autonomous Scan",
        "findings": [
            {
                "title": "Cross-tenant BOLA on subscription keys",
                "severity": "High",
                "description": "redacted description",
                "mitigation": "enforce per-tenant scoping on the lookup",
                "cwe": 639,
                "cvssv3": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
                "cvssv3_score": 6.5,
                "dynamic_finding": True,
                "unique_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "vuln_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "references": "Full authenticated report: https://dast-triage.internal/cp-backend/x.html",
                "tags": ["dast", "autonomous", "cp-backend", "prod-vulnerable"],
            },
        ],
    }).encode()


class DastParserRegistrationTests(SimpleTestCase):
    def test_install_registers_parser(self):
        install_dast_parser()
        self.assertIsInstance(factory.PARSERS.get(DAST_SCAN_TYPE), DastReportParser)

    def test_get_scan_types_advertises_unique_test_type(self):
        self.assertEqual(DastReportParser().get_scan_types(), [DAST_SCAN_TYPE])

    def test_dast_scan_type_is_dedupe_supported(self):
        # This is the whole point of the dedicated parser — a plain "Generic Findings
        # Import" scan_type finding is unconditionally excluded from canonical dedupe.
        self.assertIn(DAST_SCAN_TYPE, SUPPORTED_SCAN_TYPES)

    def test_dast_scan_type_uses_unique_id_fallback_not_legacy(self):
        # DAST findings never carry file_path/line, so they always fail canonical
        # dedupe's own eligibility gate and land in custom.py's fallback-for-
        # ineligible-targets path. Without this settings.py registration, Test.
        # deduplication_algorithm defaults to DEDUPE_ALGO_LEGACY (title/CWE matching
        # with no scan-type awareness) — DAST already emits a stable,
        # cross-run-deterministic unique_id_from_tool, so it must use that instead.
        self.assertIn(DAST_SCAN_TYPE, settings.AIST_CANONICAL_DEDUPE_SCAN_TYPES)
        self.assertEqual(
            settings.DEDUPLICATION_ALGORITHM_PER_PARSER[DAST_SCAN_TYPE],
            "unique_id_from_tool_or_hash_code",
        )

    def test_dast_scan_type_allows_null_cwe_in_hash_fallback(self):
        # aist-report-format.md does not require "cwe" in the DAST export draft —
        # without this, a CWE-less DAST finding would be silently excluded from the
        # hash-code half of the unique_id_from_tool_or_hash_code fallback match.
        self.assertTrue(settings.HASHCODE_ALLOWS_NULL_CWE.get(DAST_SCAN_TYPE))


class DastParserParsingTests(SimpleTestCase):
    def _parse(self):
        scan = io.BytesIO(_dast_findings_payload())
        scan.name = "dast_result.json"
        tests = DastReportParser().get_tests(DAST_SCAN_TYPE, scan)
        self.assertEqual(len(tests), 1)
        return tests[0]

    def test_test_type_is_reassigned_to_dast_scan_type(self):
        # GenericJSONParser defaults ParserTest.type to its own ID ("Generic Findings
        # Import") — without _SubtypedGenericParserBase's get_tests override, the
        # imported Test would render as "Generic Findings Import Scan (DAST Autonomous
        # Scan)" and dedupe's SUPPORTED_SCAN_TYPES membership check would never match.
        parser_test = self._parse()
        self.assertEqual(parser_test.type, DAST_SCAN_TYPE)

    def test_finding_fields_pass_through_unmodified(self):
        # No stabilization pass for DAST — the export is already deterministic — so the
        # finding must come through byte-for-byte from the draft.
        finding = self._parse().findings[0]
        self.assertEqual(finding.title, "Cross-tenant BOLA on subscription keys")
        self.assertEqual(finding.unique_id_from_tool, "BOLA-cross-cp-cross-tenant-bola")
        self.assertEqual(finding.vuln_id_from_tool, "BOLA-cross-cp-cross-tenant-bola")
        self.assertEqual(finding.cwe, 639)
