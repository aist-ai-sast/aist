"""
Tests for validation and endpoint preservation in
``aist.internal_upload.import_scan_via_default_importer`` — the shared importer used by
AIST scan providers.

Severity has no equivalent AIST-side guard (see the assertion at the bottom of this file
for why: DefectDojo's own DefaultImporter already hard-rejects an invalid severity for
every scan_type, before a Finding is ever saved — duplicating that here would be dead code).
"""
from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from dojo.models import Engagement, Product, Product_Type

from aist.internal_upload import import_scan_via_default_importer
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import RepoParams  # noqa: E402


def _write_report(payload: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")  # noqa: SIM115
    tmp.write(json.dumps(payload))
    tmp.close()
    return Path(tmp.name)


class ImportHardeningTests(TestCase):
    def setUp(self):
        prod_type = Product_Type.objects.create(name="Hardening PT")
        product = Product.objects.create(name="Hardening Product", description="desc", prod_type=prod_type)
        today = timezone.localdate()
        self.engagement = Engagement.objects.create(
            name="hardening engagement",
            product=product,
            engagement_type="CI/CD",
            target_start=today,
            target_end=today + timedelta(days=1),
        )
        self.repo_params = RepoParams(repo_url="", branch_tag=None, commit_hash=None, scm_type="generic", local_path=None)
        self.lead = get_user_model().objects.create_user(username="hardening-lead")

    def _import(self, findings: list[dict]):
        scan_type = "Generic Findings Import"
        report_path = _write_report({"type": scan_type, "findings": findings})
        return import_scan_via_default_importer(
            engagement=self.engagement,
            scan_type=scan_type,
            report_path=report_path,
            test_title="hardening test",
            repo_params=self.repo_params,
            minimum_severity="Info",
            lead=self.lead,
        )

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
