from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

from django.test import SimpleTestCase

from aist.integrations.dast_report import (
    DastReportExpectations,
    DastReportValidationError,
    ValidatedDastReport,
    validate_dast_terminal_result_bytes,
    validate_exported_dast_report_bytes,
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
            "delivery_quality": "complete",
            "audit_state": "complete",
            "findings_complete": True,
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
            ({"status": "failed"}, "does not carry an importable"),
            ({"audit": {"correlation_id": "other-pipeline", "source_verified": True}}, "correlation"),
            ({"audit": {"correlation_id": "pipeline-123", "source_verified": False}}, "source integrity"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(DastReportValidationError, message):
                validate_dast_terminal_result_bytes(_bytes(_terminal(**overrides)), expectations=_expectations())

    def test_selected_target_and_stand_conflicts_are_rejected(self):
        wrong_target = _report()
        wrong_target["dast_run_metadata"]["target"] = "another-target"
        with self.assertRaisesRegex(DastReportValidationError, "selected binding is for target"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=wrong_target)),
                expectations=_expectations(),
            )

        wrong_stand = _report()
        wrong_stand["dast_run_metadata"]["stand"] = "qa-2"
        with self.assertRaisesRegex(DastReportValidationError, "provider selected stand"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=wrong_stand)),
                expectations=_expectations(),
            )

    def test_unknown_source_key_invalid_sha_and_conflicting_source_maps_are_rejected(self):
        for source_commits, message in (
            ({"unknown": SHA}, "does not advertise"),
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

    def test_transport_metadata_extensions_are_accepted_without_changing_trust_claims(self):
        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(dast_run_metadata={"source_commits": {"backend": SHA}, "future": {"value": 7}})),
            expectations=_expectations(),
        )

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.source_verified)

    def test_standless_terminal_report_omits_stand_without_inventing_an_identity(self):
        report = _report()
        del report["dast_run_metadata"]["stand"]
        report["dast_run_metadata"]["source_commits"] = {}
        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(
                selection={"mode": "none", "note": "this scenario declares no stands"},
                trigger_resolution=None,
                dast_run_metadata={"source_commits": {}},
                report=report,
            )),
            expectations=_expectations(allowed_repository_keys=frozenset()),
        )

        self.assertIsNone(result.selection.stand_id)
        self.assertIsNone(result.run_metadata.stand_id)

        report["dast_run_metadata"]["stand"] = "invented"
        with self.assertRaisesRegex(DastReportValidationError, "stand-less"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(
                    selection={"mode": "none", "note": "this scenario declares no stands"},
                    trigger_resolution=None,
                    dast_run_metadata={"source_commits": {}},
                    report=report,
                )),
                expectations=_expectations(allowed_repository_keys=frozenset()),
            )

    def test_degraded_and_partial_terminal_states_require_matching_quality(self):
        degraded = _report()
        degraded["dast_run_metadata"]["delivery_quality"] = "degraded"
        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(status="completed_with_degradation", report=degraded)),
            expectations=_expectations(),
        )
        self.assertEqual(result.status, "completed_with_degradation")

        partial = _report()
        partial["dast_run_metadata"].update({
            "delivery_quality": "partial",
            "audit_state": "incomplete",
            "findings_complete": False,
        })
        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(
                status="failed_with_partial_results",
                report=partial,
                audit={"correlation_id": "pipeline-123", "source_verified": False},
            )),
            expectations=_expectations(),
        )
        self.assertEqual(result.status, "failed_with_partial_results")
        self.assertFalse(result.source_verified)

        inconsistent = _report()
        inconsistent["dast_run_metadata"]["delivery_quality"] = "partial"
        with self.assertRaisesRegex(DastReportValidationError, "partial status"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(status="failed_with_partial_results", report=inconsistent)),
                expectations=_expectations(),
            )

    def test_an_operator_uploads_the_exported_report_itself(self):
        """
        The reference artifact is what `dast export-findings` writes — no transport wrapper.

        A wrapper could only restate the report's own run, stand and source commits, or assert its
        own trustworthiness; neither checks anything, and demanding one made the file the provider
        actually produces impossible to import.
        """
        result = validate_exported_dast_report_bytes(
            _bytes(_report()),
            target_id="cloud-backend",
            allowed_repository_keys=frozenset({"backend"}),
        )

        self.assertEqual(result.run_id, "run-123")
        self.assertEqual(result.target_id, "cloud-backend")
        self.assertEqual(result.source_commit_for("backend"), SHA)
        self.assertEqual(result.run_metadata.stand_id, "qa-1")
        # Nothing delivered it, so there is no transport to describe.
        self.assertIsNone(result.correlation_id)
        self.assertIsNone(result.selection)
        self.assertIsNone(result.status)

    def test_an_operator_can_import_a_standless_perimeter_report(self):
        report = _report()
        report["dast_run_metadata"].pop("stand")
        report["dast_run_metadata"]["target"] = "perimeter"
        report["dast_run_metadata"]["source_commits"] = {}

        result = validate_exported_dast_report_bytes(
            _bytes(report),
            target_id="perimeter",
            allowed_repository_keys=frozenset(),
        )

        self.assertIsNone(result.run_metadata.stand_id)

    def test_the_upload_is_still_held_to_what_the_binding_knows(self):
        with self.assertRaisesRegex(DastReportValidationError, "selected binding is for target"):
            validate_exported_dast_report_bytes(
                _bytes(_report()),
                target_id="another-target",
                allowed_repository_keys=frozenset({"backend"}),
            )
        with self.assertRaisesRegex(DastReportValidationError, "does not advertise"):
            validate_exported_dast_report_bytes(
                _bytes(_report()),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"frontend"}),
            )

    def test_a_transport_wrapper_is_no_longer_accepted_as_an_upload(self):
        """The wrapper was never something the provider produced; accepting it kept the bug alive."""
        with self.assertRaises(DastReportValidationError):
            validate_exported_dast_report_bytes(
                _bytes(_terminal()),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"backend"}),
            )


# Real values from run 80c744a2be37d91c07a7a8ef97c520be, the first report to carry these blocks.
COVERAGE = {
    "unit": "endpoint",
    "discovered": 784,
    "reachable": 176,
    "analysed": 38,
    "planned": 10,
    "analysed_names": ["analytics3-test-hdw-mx", "cloud-prod-hdw-mx"],
    "beyond_plan_names": ["cloud-prod-hdw-mx"],
}
TOKEN_USAGE = {
    "total": {
        "input": 26,
        "output": 4712,
        "thinking": 1075,
        "cache_creation": 50367,
        "cache_read": 1448274,
        "calls": 13,
    },
    "by_phase": {
        # Phase 2 carries no name in the real report; phase 3 does. Both must survive.
        "2": {"input": 16, "output": 2081, "thinking": 534, "cache_creation": 45877, "cache_read": 799618, "calls": 8},
        "3": {
            "name": "preconditions",
            "input": 10,
            "output": 2631,
            "thinking": 541,
            "cache_creation": 4490,
            "cache_read": 648656,
            "calls": 5,
        },
    },
    "by_agent_type": {
        "dast-verify": {
            "agents": 5,
            "input": 26,
            "output": 4712,
            "thinking": 1075,
            "cache_creation": 50367,
            "cache_read": 1448274,
            "calls": 13,
        },
    },
}


def _metadata_report(**metadata_overrides) -> dict:
    report = _report()
    report["dast_run_metadata"].update(metadata_overrides)
    return report


def _validate_metadata(**metadata_overrides):
    result = validate_dast_terminal_result_bytes(
        _bytes(_terminal(report=_metadata_report(**metadata_overrides))),
        expectations=_expectations(),
    )
    return result.run_metadata


class DastRunMetadataValidationTests(SimpleTestCase):

    """
    The report's ``coverage`` and ``token_usage`` blocks.

    Everything below the three run identities is optional at every level, so absence is never an
    error; a value that *is* present is bounded; and a breakdown that disagrees with its own total
    is recorded rather than costing the report its findings.
    """

    def test_the_real_report_shape_is_accepted_whole(self):
        metadata = _validate_metadata(
            product_family="perimeter",
            tier="external",
            run_type="deep",
            target_host="analytics3.test.hdw.mx",
            scan_started="2026-08-17T17:37:46",
            scan_finished="2026-08-17T19:56:35",
            coverage=COVERAGE,
            token_usage=TOKEN_USAGE,
        )

        self.assertEqual(metadata.run_id, "run-123")
        self.assertEqual(metadata.tier, "external")
        self.assertEqual(metadata.target_host, "analytics3.test.hdw.mx")
        self.assertEqual(metadata.coverage.discovered, 784)
        self.assertEqual(metadata.coverage.analysed, 38)
        self.assertEqual(metadata.coverage.beyond_plan_names, ("cloud-prod-hdw-mx",))
        self.assertEqual(metadata.token_usage.total.output_tokens, 4712)
        self.assertEqual([bucket.key for bucket in metadata.token_usage.by_phase], ["2", "3"])
        self.assertIsNone(metadata.token_usage.by_phase[0].name)
        self.assertEqual(metadata.token_usage.by_phase[1].name, "preconditions")
        self.assertEqual(metadata.token_usage.by_agent_type[0].agents, 5)
        self.assertTrue(metadata.token_usage.accounting_consistent)

    def test_offsetless_report_timestamps_are_read_as_utc(self):
        metadata = _validate_metadata(scan_started="2026-08-17T17:37:46")

        self.assertEqual(metadata.scan_started.utcoffset().total_seconds(), 0)

    def test_a_report_that_carries_neither_block_still_validates(self):
        metadata = _validate_metadata()

        self.assertIsNone(metadata.coverage)
        self.assertIsNone(metadata.token_usage)
        self.assertIsNone(metadata.tier)
        self.assertIsNone(metadata.scan_started)

    def test_explicit_nulls_read_the_same_as_absence(self):
        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(report=_metadata_report(coverage=None, token_usage=None, tier=None))),
            expectations=_expectations(),
        )
        metadata = result.run_metadata

        self.assertIsNone(metadata.coverage)
        self.assertIsNone(metadata.token_usage)
        self.assertIsNone(metadata.tier)
        canonical_metadata = json.loads(result.canonical_json)["dast_run_metadata"]
        self.assertNotIn("coverage", canonical_metadata)
        self.assertNotIn("token_usage", canonical_metadata)
        self.assertNotIn("tier", canonical_metadata)

    def test_partial_blocks_keep_what_was_reported_without_inventing_the_rest(self):
        metadata = _validate_metadata(
            coverage={"analysed": 38},
            token_usage={"by_phase": TOKEN_USAGE["by_phase"]},
        )

        self.assertEqual(metadata.coverage.analysed, 38)
        self.assertIsNone(metadata.coverage.discovered)
        self.assertIsNone(metadata.coverage.analysed_names)
        self.assertIsNone(metadata.token_usage.total)
        # Nothing to compare a breakdown against, so consistency is unknown rather than False.
        self.assertIsNone(metadata.token_usage.accounting_consistent)

    def test_names_are_not_pattern_matched_so_path_shaped_endpoints_survive(self):
        metadata = _validate_metadata(
            coverage={"analysed_names": ["/partners/internal/grant_access", "https://host/api/v1?x=1"]},
        )

        self.assertEqual(
            metadata.coverage.analysed_names,
            ("/partners/internal/grant_access", "https://host/api/v1?x=1"),
        )

    def test_a_breakdown_that_disagrees_with_its_total_is_flagged_not_rejected(self):
        drifted = json.loads(json.dumps(TOKEN_USAGE))
        drifted["by_phase"]["2"]["output"] = 1

        metadata = _validate_metadata(token_usage=drifted)

        self.assertFalse(metadata.token_usage.accounting_consistent)
        self.assertEqual(metadata.token_usage.total.output_tokens, 4712)

    def test_a_bucket_missing_a_counter_is_skipped_rather_than_summed_wrongly(self):
        partial = json.loads(json.dumps(TOKEN_USAGE))
        del partial["by_phase"]["2"]["output"]

        metadata = _validate_metadata(token_usage=partial)

        # Output is no longer comparable, but the other five counters still are and still agree.
        self.assertTrue(metadata.token_usage.accounting_consistent)

    def test_malformed_values_cost_the_report_its_import(self):
        cases = [
            ("negative count", {"coverage": {"analysed": -1}}),
            ("boolean as count", {"coverage": {"analysed": True}}),
            ("count as string", {"coverage": {"analysed": "38"}}),
            ("coverage is not an object", {"coverage": [1, 2]}),
            ("names are not a list", {"coverage": {"analysed_names": "a,b"}}),
            ("name carries a control character", {"coverage": {"analysed_names": ["with\nnewline"]}}),
            ("names over the list cap", {"coverage": {"analysed_names": ["x"] * 5001}}),
            ("name over its length cap", {"coverage": {"analysed_names": ["x" * 254]}}),
            ("buckets over the cap", {"token_usage": {"by_phase": {str(i): {"calls": 1} for i in range(65)}}}),
            ("descriptor is not a string", {"tier": 7}),
            ("unparseable timestamp", {"scan_started": "not-a-date"}),
        ]
        for label, overrides in cases:
            with self.subTest(case=label), self.assertRaises(DastReportValidationError):
                _validate_metadata(**overrides)

    def test_the_shape_of_a_known_field_is_still_enforced_inside_a_tolerated_block(self):
        """Tolerating an unread key must not soften the fields around it."""
        with self.assertRaises(DastReportValidationError):
            _validate_metadata(coverage={"unread_by_aist": 1, "analysed": -1})


class DastReportForwardCompatibilityTests(SimpleTestCase):

    """
    A field AIST has never heard of must not cost the report its findings.

    The DAST side evolves independently. Refusing a report because it carries one attribute we do
    not model would mean an AIST release per provider addition, and would throw away every real
    finding in the report meanwhile. Unread *descriptive* fields are therefore ignored and logged;
    the trust-critical structure around them stays closed, and known fields stay strictly checked.
    """

    def test_unread_descriptive_fields_are_ignored_at_every_level(self):
        cases = [
            ("a new metadata field", {"scan_profile": "aggressive"}),
            ("a new coverage field", {"coverage": {**COVERAGE, "skipped": 12}}),
            ("a new token_usage field", {"token_usage": {**TOKEN_USAGE, "by_tool": {}}}),
            (
                "a new counter in a token bucket",
                {"token_usage": {**TOKEN_USAGE, "total": {**TOKEN_USAGE["total"], "cache_hits": 5}}},
            ),
        ]
        for label, metadata_overrides in cases:
            with self.subTest(case=label):
                result = validate_dast_terminal_result_bytes(
                    _bytes(_terminal(report=_metadata_report(**metadata_overrides))),
                    expectations=_expectations(),
                )
                self.assertEqual(result.run_metadata.run_id, "run-123")

    def test_a_finding_field_the_platform_cannot_store_is_dropped_not_fatal(self):
        report = _report(findings=[{
            "title": "Cross-tenant object access",
            "severity": "High",
            "description": "redacted",
            "confidence": "high",           # not modelled by the platform
            "detector_version": "2026.8",   # nor this
        }])

        result = validate_dast_terminal_result_bytes(
            _bytes(_terminal(report=report)),
            expectations=_expectations(),
        )

        self.assertEqual(result.findings_count, 1)
        # The stored artifact keeps what the provider sent, verbatim.
        stored = json.loads(result.open_report().read())
        self.assertEqual(stored["findings"][0]["confidence"], "high")

    def test_a_finding_still_has_to_carry_what_the_platform_requires(self):
        report = _report(findings=[{"title": "No description", "severity": "High"}])

        with self.assertRaisesRegex(DastReportValidationError, "finding schema"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=report)),
                expectations=_expectations(),
            )

    def test_the_trust_critical_structure_stays_closed_to_unread_fields(self):
        cases = [
            ("terminal result", {"surprise": 1}),
            ("selection", {"selection": {"stand_id": "qa-1", "relation": "exact", "distance": 0, "extra": 1}}),
        ]
        for label, overrides in cases:
            with self.subTest(case=label), self.assertRaises(DastReportValidationError):
                validate_dast_terminal_result_bytes(_bytes(_terminal(**overrides)), expectations=_expectations())

    def test_the_report_envelope_also_stays_closed(self):
        """A top-level addition must be a deliberate contract change; "report_path" is why."""
        with self.assertRaisesRegex(DastReportValidationError, "envelope fields"):
            validate_dast_terminal_result_bytes(
                _bytes(_terminal(report=_report(summary="3 high"))),
                expectations=_expectations(),
            )

    def test_identities_still_have_to_agree(self):
        cases = [
            ("run", {"run_id": "other-run"}, "the run being imported"),
            ("stand", {"stand": "other-stand"}, "provider selected stand"),
            ("target", {"target": "other-target"}, "selected binding is for target"),
        ]
        for label, overrides, expected in cases:
            with self.subTest(case=label), self.assertRaisesRegex(DastReportValidationError, expected):
                validate_dast_terminal_result_bytes(
                    _bytes(_terminal(report=_metadata_report(**overrides))),
                    expectations=_expectations(),
                )


class DastReportDiagnosticsTests(SimpleTestCase):

    """
    A refusal has to say what conflicted with what.

    The value an operator cannot see is the one inside the file they are uploading, so every
    identity conflict names both the report's claim and what the binding expects. Without that the
    only actionable information in the message was that something, somewhere, disagreed.
    """

    def test_a_target_mismatch_names_the_report_and_the_binding(self):
        with self.assertRaises(DastReportValidationError) as caught:
            validate_exported_dast_report_bytes(
                _bytes(_report()),
                target_id="demo-perimeter",
                allowed_repository_keys=frozenset({"backend"}),
            )

        message = str(caught.exception)
        self.assertIn("'cloud-backend'", message)      # what the report says it is
        self.assertIn("'demo-perimeter'", message)     # what the operator picked
        self.assertIn("synchronize the DAST catalog", message)

    def test_a_missing_source_revision_names_the_repositories_the_target_expects(self):
        sourceless = _report()
        sourceless["dast_run_metadata"]["source_commits"] = {}

        with self.assertRaises(DastReportValidationError) as caught:
            validate_exported_dast_report_bytes(
                _bytes(sourceless),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"backend", "frontend"}),
            )

        message = str(caught.exception)
        self.assertIn("'backend'", message)
        self.assertIn("'frontend'", message)

    def test_an_unadvertised_repository_names_what_the_target_does_advertise(self):
        with self.assertRaises(DastReportValidationError) as caught:
            validate_exported_dast_report_bytes(
                _bytes(_report()),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"frontend"}),
            )

        self.assertIn("'frontend'", str(caught.exception))

    def test_source_revisions_sent_to_a_target_that_wants_none_say_so(self):
        with self.assertRaises(DastReportValidationError) as caught:
            validate_exported_dast_report_bytes(
                _bytes(_report()),
                target_id="cloud-backend",
                allowed_repository_keys=frozenset(),
            )

        self.assertIn("no repository requirement", str(caught.exception))
