"""
Tests for ``aist.internal_upload``.

Validation in the shared default importer, and how ``upload_results_internal`` treats each analyzer report.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from dojo.models import Finding, Product, Product_Type

from aist.internal_upload import NO_EXCLUSION, upload_results_internal
from aist.test.agent_analyzer_import import write_analyzers_yaml
from aist.test.analyzer_catalog import write_analyzers_config
from aist.test.scan_import_helpers import (
    GENERIC_SCAN_TYPE,
    import_generic_report,
    upload_engagement,
    write_generic_report,
)


class ImportHardeningTests(TestCase):
    def setUp(self):
        prod_type = Product_Type.objects.create(name="Hardening PT")
        product = Product.objects.create(name="Hardening Product", description="desc", prod_type=prod_type)
        self.engagement = upload_engagement(product, "hardening engagement")
        self.lead = get_user_model().objects.create_user(username="hardening-lead")

    def _import(self, findings: list[dict]):
        return import_generic_report(self.engagement, findings, lead=self.lead)

    def test_endpoint_with_allowed_scheme_is_kept(self):
        _test_obj, findings = self._import([
            {"title": "X", "severity": "High", "description": "d", "endpoints": ["https://example.com/path"]},
        ])
        self.assertEqual(findings[0].endpoints.count(), 1)
        self.assertEqual(findings[0].endpoints.first().protocol, "https")

    def test_tcp_service_location_is_kept_for_dynamic_finding_dedupe(self):
        _test_obj, findings = self._import([
            {
                "title": "coturn peer ACL bypass",
                "severity": "High",
                "description": "The TURN service relays traffic to a denied peer range.",
                "dynamic_finding": True,
                "endpoints": [{
                    "protocol": "tcp",
                    "host": "mail.relay.example",
                    "port": 3478,
                }],
            },
        ])

        endpoint = findings[0].endpoints.get()
        self.assertEqual(endpoint.protocol, "tcp")
        self.assertEqual(endpoint.host, "mail.relay.example")
        self.assertEqual(endpoint.port, 3478)

    def test_defaultimporter_itself_already_rejects_invalid_severity(self):
        # Documents why aist/internal_upload.py has no severity-coercion guard of its own:
        # DefaultImporter.process_findings -> sanitize_severity already raises for every
        # scan_type, before any Finding is saved. If this ever stops being true, an
        # AIST-side guard needs to be added back — this test is the tripwire for that.
        with self.assertRaises(ValidationError):
            self._import([{"title": "X", "severity": "Nonsense", "description": "d"}])


class AnalyzerReportImportFailureTests(TestCase):

    """
    Each report sast-pipeline produced yields one import result, failed when the platform cannot import it.

    An analyzer that produced no report yields no result.
    """

    def setUp(self):
        prod_type = Product_Type.objects.create(name="Report Import PT")
        self.product = Product.objects.create(name="Report Import Product", description="desc", prod_type=prod_type)
        get_user_model().objects.create_user(username="report-import-lead")
        self.work_dir = Path(tempfile.mkdtemp(prefix="aist-report-import-"))
        self.addCleanup(shutil.rmtree, self.work_dir)

    def _upload(self, config_path: str):
        return upload_results_internal(
            analyzers_cfg_path=config_path,
            output_dir=str(self.work_dir),
            product=self.product,
            repo_path=str(self.work_dir),
            exclusion=NO_EXCLUSION,
            logger=logging.getLogger(__name__),
        )

    def test_an_unparseable_report_is_returned_as_an_import_failure(self):
        config_path = write_analyzers_yaml(
            self.work_dir, name="semgrep", skill_name="", result_file="semgrep.json", output_type=GENERIC_SCAN_TYPE,
        )
        (self.work_dir / "semgrep.json").write_text("{not json", encoding="utf-8")

        results = self._upload(config_path)

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].test_id)
        self.assertEqual((results[0].analyzer_name, results[0].failure_code), ("semgrep", "import_failed"))

    def test_an_analyzer_that_left_an_empty_report_is_not_imported_and_the_others_are(self):
        config_path = write_analyzers_config(self.work_dir, [
            {"name": "eslint_security", "output_type": "SARIF", "result_file": "eslint_security.sarif"},
            {"name": "semgrep", "output_type": GENERIC_SCAN_TYPE, "result_file": "semgrep.json"},
        ])
        (self.work_dir / "eslint_security.sarif").write_text("", encoding="utf-8")
        write_generic_report(
            self.work_dir / "semgrep.json",
            [{"title": "Command Injection", "severity": "High", "description": "User input reaches a shell."}],
        )

        results = self._upload(config_path)

        self.assertEqual([(result.analyzer_name, result.failure_code) for result in results], [("semgrep", None)])
        self.assertEqual(
            list(Finding.objects.filter(test_id=results[0].test_id).values_list("title", flat=True)),
            ["Command Injection"],
        )
