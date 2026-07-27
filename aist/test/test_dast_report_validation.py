from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase

from aist.integrations.dast_report import (
    DastReportExpectations,
    DastReportValidationError,
    ValidatedDastReport,
    validate_dast_terminal_result_bytes,
    validate_manual_dast_terminal_result_bytes,
)
from aist.parser_overrides import DAST_SCAN_TYPE

SHA = "a" * 40


def _expectations(**overrides) -> DastReportExpectations:
    values = {
        "correlation_id": "pipeline-123",
        "run_id": "run-123",
        "target_id": "cloud-backend",
        "allowed_repository_keys": frozenset({"backend", "frontend"}),
    }
    values.update(overrides)
    return DastReportExpectations(**values)


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
        },
    }
    report.update(overrides)
    return report


def _terminal(**overrides) -> dict:
    payload = {
        "contract_version": "2.0",
        "run_id": "run-123",
        "status": "succeeded",
        "selection": {"stand_id": "qa-1", "relation": "exact", "distance": 0},
        "trigger_resolution": {
            "type": "GIT_HASH",
            "ref": SHA,
            "resolved_commit": SHA,
            "resolved_at": "2026-07-26T10:00:00Z",
        },
        "dast_run_metadata": {"source_commits": {"backend": SHA}},
        "report": _report(),
        "audit": {"correlation_id": "pipeline-123", "source_verified": True},
    }
    payload.update(overrides)
    return payload


def _bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


class DastReportValidationTests(SimpleTestCase):

    def test_valid_clean_terminal_report_returns_frozen_boundary_object(self):
        result = validate_dast_terminal_result_bytes(_bytes(_terminal()), expectations=_expectations())

        self.assertIsInstance(result, ValidatedDastReport)
        self.assertEqual(result.findings_count, 0)
        self.assertEqual(result.source_commit_for("backend"), SHA)
        self.assertEqual(json.loads(result.open_report().read()), _report())
        with self.assertRaises(FrozenInstanceError):
            result.run_id = "changed"

    def test_terminal_identity_and_contract_mismatches_are_rejected(self):
        cases = (
            ({"contract_version": "1.0"}, "contract version"),
            ({"run_id": "other-run"}, "run identity"),
            ({"status": "failed"}, "successful"),
            ({"audit": {"correlation_id": "other-pipeline", "source_verified": True}}, "correlation"),
            ({"audit": {"correlation_id": "pipeline-123", "source_verified": False}}, "source integrity"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(DastReportValidationError, message):
                validate_dast_terminal_result_bytes(_bytes(_terminal(**overrides)), expectations=_expectations())

    def test_selected_target_and_stand_conflicts_are_rejected(self):
        wrong_target = _report()
        wrong_target["dast_run_metadata"]["target"] = "another-target"
        with self.assertRaisesRegex(DastReportValidationError, "target conflicts"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=wrong_target)),
                expectations=_expectations(),
            )

        wrong_stand = _report()
        wrong_stand["dast_run_metadata"]["stand"] = "qa-2"
        with self.assertRaisesRegex(DastReportValidationError, "stand conflicts"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=wrong_stand)),
                expectations=_expectations(),
            )

    def test_unknown_source_key_invalid_sha_and_conflicting_source_maps_are_rejected(self):
        for source_commits, message in (
            ({"unknown": SHA}, "unknown source"),
            ({"backend": "A" * 40}, "lowercase full SHA-1"),
        ):
            with self.subTest(source_commits=source_commits), self.assertRaisesRegex(
                DastReportValidationError,
                message,
            ):
                validate_dast_terminal_result_bytes(
                    _bytes(_terminal(dast_run_metadata={"source_commits": source_commits})),
                    expectations=_expectations(),
                )

        nested = _report()
        nested["dast_run_metadata"]["source_commits"] = {"backend": "b" * 40}
        with self.assertRaisesRegex(DastReportValidationError, "source commits conflict"):
            validate_dast_terminal_result_bytes(_bytes(_terminal(report=nested)), expectations=_expectations())

    def test_malformed_finding_is_rejected_by_registered_dast_schema(self):
        report = _report(findings=[{"title": "missing required fields"}])

        with self.assertRaisesRegex(DastReportValidationError, "finding schema"):
            validate_dast_terminal_result_bytes(_bytes(_terminal(report=report)), expectations=_expectations())

    def test_oversized_zip_duplicate_and_path_payloads_fail_before_persistence(self):
        with self.assertRaisesRegex(DastReportValidationError, "size limit"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal()),
                expectations=_expectations(),
                maximum_result_bytes=8,
            )
        with self.assertRaisesRegex(DastReportValidationError, "UTF-8 JSON"):
            validate_dast_terminal_result_bytes(b"PK\x03\x04not-json", expectations=_expectations())
        with self.assertRaisesRegex(DastReportValidationError, "Duplicate JSON field"):
            validate_dast_terminal_result_bytes(
                b'{"contract_version":"2.0","contract_version":"2.0"}',
                expectations=_expectations(),
            )
        path_report = _report(report_path="provider-report.json")
        with self.assertRaisesRegex(DastReportValidationError, "envelope fields"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=path_report)),
                expectations=_expectations(),
            )

    def test_manual_boundary_uses_artifact_identity_but_enforces_bound_target(self):
        result = validate_manual_dast_terminal_result_bytes(
            _bytes(_terminal()),
            target_id="cloud-backend",
            allowed_repository_keys=frozenset({"backend"}),
        )

        self.assertEqual(result.run_id, "run-123")
        self.assertEqual(result.correlation_id, "pipeline-123")
        self.assertEqual(result.source_commit_for("backend"), SHA)
        with self.assertRaisesRegex(DastReportValidationError, "target conflicts"):
            validate_manual_dast_terminal_result_bytes(
                _bytes(_terminal()),
                target_id="another-target",
                allowed_repository_keys=frozenset({"backend"}),
            )
