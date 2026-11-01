"""
A DAST report imported through the real importer carries what deduplication reads.

Both inputs deduplication correlates DAST findings by — the endpoints and the
vulnerability identifiers — are written by the platform importer, not by AIST. When the
platform moves either one, nothing raises: findings simply stop correlating and every run
re-files the same defect as new. This drives the whole chain (parser, importer, database,
signature) rather than asserting against hand-built rows.
"""
from __future__ import annotations

import json
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    SLA_Configuration,
)

from aist.dedupe.canonical import DAST_SCAN_TYPE, finding_signature
from aist.internal_upload import RepoParams, import_scan_file_via_default_importer

User = get_user_model()

CVE = "CVE-2026-43994"
HOST = "mail.relay.aktt2.cloud.hdw.mx"


def _report() -> bytes:
    """The artifact `dast export-findings --format generic-aist` writes."""
    return json.dumps({
        "name": "DAST",
        "type": DAST_SCAN_TYPE,
        "version": "backend@fd5b25aa1234",
        "findings": [{
            "title": "coturn OAuth token stack overflow",
            "severity": "Critical",
            "description": "The exposed TURN service runs an affected coturn build.",
            "cwe": 121,
            "vulnerability_ids": [CVE],
            "endpoints": [{"protocol": "tcp", "host": HOST, "port": 3478, "path": ""}],
            "service": "turn",
            "component_name": "coturn",
            "component_version": "4.6.0",
            "dynamic_finding": True,
        }],
        "dast_run_metadata": {
            "run_id": "run-123",
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": "f" * 40},
        },
    }).encode()


class DastImportToDedupeChainTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="chain SLA")
        product_type = Product_Type.objects.create(name="chain PT")
        self.product = Product.objects.create(
            name="chain product", description="d", prod_type=product_type,
            sla_configuration_id=sla.id,
        )
        self.engagement = Engagement.objects.create(
            name="chain engagement", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        self.user = User.objects.create_user("chain", "chain@x.test", "pass")

    def _import(self, title: str) -> Finding:
        test, _findings = import_scan_file_via_default_importer(
            engagement=self.engagement,
            scan_type=DAST_SCAN_TYPE,
            report_file=BytesIO(_report()),
            test_title=title,
            repo_params=RepoParams(
                repo_url="https://scm.example.test/chain.git",
                branch_tag="main",
                commit_hash="f" * 40,
                scm_type="generic",
                local_path="/tmp/chain",  # noqa: S108
            ),
            minimum_severity="Info",
            lead=self.user,
        )
        return Finding.objects.get(test=test)

    def test_the_import_writes_the_endpoint_deduplication_reads(self):
        finding = self._import("first run")

        signature = finding_signature(finding)
        self.assertEqual(len(signature.web_locations), 1, "DAST route matching is blind without these")
        self.assertEqual(signature.web_locations[0].host, HOST)

    def test_the_import_writes_the_vulnerability_id_deduplication_reads(self):
        finding = self._import("first run")

        self.assertEqual(finding_signature(finding).vulnerability_ids, frozenset({CVE}))

    def test_the_candidate_prefilter_finds_an_imported_finding_by_its_cve(self):
        """The lookup ``dedupe_batch_of_findings`` narrows candidates with."""
        finding = self._import("first run")

        reachable = Finding.objects.filter(
            vulnerability_references__vulnerability__vulnerability_id__in=[CVE],
        )
        self.assertIn(finding, reachable)
