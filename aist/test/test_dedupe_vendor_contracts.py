"""
The DefectDojo contracts AIST deduplication reads through.

Both are silent when they break: the dedupe layer stops matching instead of raising, so a
scanned finding is re-filed as new on every run. These tests pin the two reads against the
vendor's own write paths rather than against a stored shape AIST assumes.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.finding.helper import save_vulnerability_ids
from dojo.models import (
    Endpoint,
    Engagement,
    Finding,
    Product,
    Product_Type,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.dedupe.canonical import DAST_SCAN_TYPE, finding_signature

User = get_user_model()


class DedupeVendorContractTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="SLA")
        product_type = Product_Type.objects.create(name="contracts")
        self.product = Product.objects.create(
            name="contracts", description="d", prod_type=product_type,
            sla_configuration_id=sla.id,
        )
        engagement = Engagement.objects.create(
            name="eng", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        test_type, _ = Test_Type.objects.get_or_create(name=DAST_SCAN_TYPE)
        self.test = Test.objects.create(
            engagement=engagement, test_type=test_type,
            target_start=timezone.now(), target_end=timezone.now(),
        )
        self.reporter = User.objects.create_user("contracts", "contracts@x.test", "pass")

    def _finding(self, title: str) -> Finding:
        return Finding.objects.create(
            test=self.test, title=title, severity="High", description="d",
            numerical_severity="S1", date=timezone.now(), reporter=self.reporter,
            dynamic_finding=True,
        )

    def test_a_cve_written_by_an_import_is_reachable_by_the_candidate_prefilter(self):
        """``dedupe_batch_of_findings`` narrows candidates with this exact lookup."""
        finding = self._finding("coturn stack overflow")
        save_vulnerability_ids(finding, ["CVE-2026-43994"])
        finding.save(dedupe_option=False, issue_updater_option=False, push_to_jira=False)

        reachable = Finding.objects.filter(
            vulnerability_references__vulnerability__vulnerability_id__in=["CVE-2026-43994"],
        )
        self.assertIn(finding, reachable)

    def test_a_cve_written_by_an_import_reaches_the_signature(self):
        finding = self._finding("coturn stack overflow")
        save_vulnerability_ids(finding, ["CVE-2026-43994"])
        finding.save(dedupe_option=False, issue_updater_option=False, push_to_jira=False)

        signature = finding_signature(Finding.objects.get(pk=finding.pk))
        self.assertEqual(signature.vulnerability_ids, frozenset({"CVE-2026-43994"}))

    def test_an_attached_endpoint_becomes_a_web_location(self):
        """A DAST route identity is built from these; an empty tuple disables route matching."""
        finding = self._finding("partner lookup exposes another tenant")
        endpoint = Endpoint.objects.create(
            product=self.product, protocol="https", host="prod-api.example.test",
            port=443, path="partners/{partner_id}",
        )
        finding.endpoints.add(endpoint)

        signature = finding_signature(Finding.objects.get(pk=finding.pk))
        self.assertEqual(len(signature.web_locations), 1)
        location = signature.web_locations[0]
        self.assertEqual(location.scheme, "https")
        self.assertEqual(location.host, "prod-api.example.test")
        self.assertEqual(location.path, ("partners", "{partner_id}"))

    def test_one_unparsable_endpoint_does_not_hide_the_others(self):
        finding = self._finding("partner lookup exposes another tenant")
        for host in ("prod-api.example.test", "qa-api.example.test"):
            finding.endpoints.add(Endpoint.objects.create(
                product=self.product, protocol="https", host=host, port=443,
                path="partners/{partner_id}",
            ))

        signature = finding_signature(Finding.objects.get(pk=finding.pk))
        self.assertEqual(
            {location.host for location in signature.web_locations},
            {"prod-api.example.test", "qa-api.example.test"},
        )

    def test_a_finding_without_endpoints_has_no_web_locations(self):
        signature = finding_signature(self._finding("no endpoints"))
        self.assertEqual(signature.web_locations, ())
