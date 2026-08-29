from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase

from aist.integrations.dast_report import (
    DastReportValidationError,
    ValidatedDastReport,
    validate_dast_report_bytes,
)
from aist.parser_overrides import DAST_SCAN_TYPE

SHA = "a" * 40


def _report(**overrides) -> dict:
    report = {
        "name": "DAST",
        "type": DAST_SCAN_TYPE,
        "version": "backend@aaaaaaaaaaaa",
        "findings": [],
        "dast_run_metadata": {
            "run_id": "run-123",
            "target": "cloud-backend",
            "stand": "qa-1",
            "source_commits": {"backend": SHA},
            "delivery_quality": "complete",
            "audit_state": "complete",
            "findings_complete": True,
        },
    }
    report.update(overrides)
    return report


def _bytes(payload: object) -> bytes:
    return json.dumps(payload).encode()


def _validate(report: dict | None = None, **overrides):
    options = {
        "target_id": "cloud-backend",
        "allowed_repository_keys": frozenset({"backend", "frontend"}),
    }
    options.update(overrides)
    return validate_dast_report_bytes(_bytes(report or _report()), **options)


class DastReportValidationTests(SimpleTestCase):

    def test_valid_report_returns_frozen_boundary_object(self):
        result = _validate()

        self.assertIsInstance(result, ValidatedDastReport)
        self.assertEqual(result.findings_count, 0)
        self.assertEqual(result.source_commit_for("backend"), SHA)
        self.assertEqual(json.loads(result.open_report().read()), _report())
        with self.assertRaises(FrozenInstanceError):
            result.run_id = "changed"

    def test_only_import_and_tenant_binding_invariants_reject_a_report(self):
        cases = (
            ({"type": "Generic Findings Import"}, {}, "scan type"),
            ({"findings": {}}, {}, "findings must be an array"),
            ({"dast_run_metadata": {"run_id": "", "target": "cloud-backend", "source_commits": {}}}, {}, "non-empty"),
            ({}, {"target_id": "another-target"}, "selected binding is for target"),
            (
                {"dast_run_metadata": {
                    "run_id": "run-123",
                    "target": "cloud-backend",
                    "source_commits": {"unadvertised": SHA},
                }},
                {},
                "does not advertise",
            ),
        )
        for report_overrides, call_overrides, message in cases:
            with self.subTest(message=message):
                report = _report(**report_overrides)
                with self.assertRaisesRegex(DastReportValidationError, message):
                    _validate(report, **call_overrides)

    def test_source_commit_value_is_not_a_tenant_boundary_check(self):
        report = _report()
        report["dast_run_metadata"]["source_commits"] = {"backend": "not-a-commit"}

        result = _validate(report)

        self.assertEqual(result.source_commit_for("backend"), "not-a-commit")

    def test_source_commit_keys_are_a_subset_not_a_required_complete_set(self):
        report = _report()
        report["dast_run_metadata"]["source_commits"] = {}

        result = _validate(report)

        self.assertEqual(result.source_commits, ())

    def test_perimeter_binding_accepts_only_an_empty_source_map(self):
        report = _report()
        report["dast_run_metadata"].update({"target": "perimeter", "source_commits": {}})

        result = _validate(report, target_id="perimeter", allowed_repository_keys=frozenset())

        self.assertEqual(result.source_commits, ())

    def test_file_size_duplicate_keys_and_non_json_are_rejected(self):
        with self.assertRaisesRegex(DastReportValidationError, "size limit"):
            validate_dast_report_bytes(
                _bytes(_report()),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"backend"}),
                maximum_report_bytes=8,
            )
        with self.assertRaisesRegex(DastReportValidationError, "Duplicate JSON field"):
            validate_dast_report_bytes(
                b'{"type":"DAST Autonomous Scan","type":"DAST Autonomous Scan"}',
                target_id="cloud-backend",
                allowed_repository_keys=frozenset(),
            )
        with self.assertRaisesRegex(DastReportValidationError, "UTF-8 JSON"):
            validate_dast_report_bytes(
                b"PK\x03\x04not-json",
                target_id="cloud-backend",
                allowed_repository_keys=frozenset(),
            )

    def test_envelope_extensions_and_finding_contents_are_left_to_the_importer(self):
        report = _report(
            report_path="provider-report.json",
            findings=[{"title": "the importer will report missing required fields"}],
        )

        result = _validate(report)

        self.assertEqual(result.findings_count, 1)
        self.assertEqual(json.loads(result.canonical_json)["report_path"], "provider-report.json")


class DastRunMetadataBestEffortTests(SimpleTestCase):

    def test_empty_stand_is_absence_without_a_selection_special_case(self):
        report = _report()
        report["dast_run_metadata"]["stand"] = ""

        result = _validate(report)

        self.assertIsNone(result.run_metadata.stand_id)

    def test_malformed_description_becomes_null_without_rejecting_findings(self):
        report = _report()
        report["dast_run_metadata"].update({
            "tier": 7,
            "product_family": "",
            "run_type": [],
            "target_host": "\n",
            "scan_started": "not-a-date",
            "coverage": {"analysed": -1, "reachable": 4},
            "token_usage": {"total": {"output": "unknown", "calls": 3}},
            "delivery_quality": "future-quality",
            "audit_state": {},
            "findings_complete": "yes",
            "operator_actions": "unknown",
            "excluded_findings": "unknown",
            "provider_extension": {"kept": True},
        })

        metadata = _validate(report).run_metadata

        self.assertIsNone(metadata.tier)
        self.assertIsNone(metadata.product_family)
        self.assertIsNone(metadata.run_type)
        self.assertIsNone(metadata.target_host)
        self.assertIsNone(metadata.scan_started)
        self.assertIsNone(metadata.coverage.analysed)
        self.assertEqual(metadata.coverage.reachable, 4)
        self.assertIsNone(metadata.token_usage.total.output_tokens)
        self.assertEqual(metadata.token_usage.total.calls, 3)
        self.assertIsNone(metadata.delivery_quality)
        self.assertIsNone(metadata.audit_state)
        self.assertIsNone(metadata.findings_complete)
        self.assertIsNone(metadata.operator_actions)
        self.assertIsNone(metadata.excluded_findings)

    def test_valid_coverage_and_token_usage_are_still_parsed_for_ui_storage(self):
        report = _report()
        report["dast_run_metadata"].update({
            "coverage": {
                "unit": "endpoint",
                "analysed": 8,
                "analysed_names": ["synthetic.example/api"],
            },
            "token_usage": {
                "total": {"input": 5, "output": 7, "calls": 2},
                "by_phase": {"verify": {"input": 5, "output": 7, "calls": 2}},
            },
        })

        metadata = _validate(report).run_metadata

        self.assertEqual(metadata.coverage.analysed, 8)
        self.assertEqual(metadata.coverage.analysed_names, ("synthetic.example/api",))
        self.assertEqual(metadata.token_usage.total.output_tokens, 7)
        self.assertTrue(metadata.token_usage.accounting_consistent)

    def test_descriptive_columns_are_read_independently_and_empty_values_are_null(self):
        report = _report()
        report["dast_run_metadata"].update({
            "coverage": {"analysed_names": []},
            "operator_actions": [],
            "operator_actions_persisted": True,
            "operator_actions_total": 3,
            "operator_actions_truncated": "unknown",
            "excluded_findings": [],
            "excluded_findings_total": 0,
            "excluded_findings_truncated": False,
        })

        metadata = _validate(report).run_metadata

        self.assertIsNone(metadata.coverage.analysed_names)
        self.assertIsNone(metadata.operator_actions)
        self.assertTrue(metadata.operator_actions_persisted)
        self.assertEqual(metadata.operator_actions_total, 3)
        self.assertIsNone(metadata.operator_actions_truncated)
        self.assertIsNone(metadata.excluded_findings)
        self.assertEqual(metadata.excluded_findings_total, 0)
        self.assertFalse(metadata.excluded_findings_truncated)
