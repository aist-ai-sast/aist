"""
A SARIF report whose locations are absolute ``file:`` URIs, as ESLint's formatter writes them: profile exclusions
match the decoded path at import, enrichment stores it relative to the scanned checkout and links it, and the line
fingerprint is read from the checked-out source.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import warnings
from pathlib import Path
from unittest.mock import patch

from celery.exceptions import CPendingDeprecationWarning
from dojo.middleware import watson_search_context_for_task
from dojo.models import DojoMeta, Finding

from aist.dedupe.evolution import AIST_LHASH_META_NAME
from aist.internal_upload import FindingExclusion
from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder
from aist.models import AISTStatus
from aist.profile import ProjectProfile
from aist.tasks.enrich import annotate_line_hash_batch, enrich_finding_batch, make_enrich_chord
from aist.test.scan_import_helpers import import_report, upload_engagement
from aist.test.test_api import AISTApiBase, create_pipeline

CHECKOUT_ROOT = "/workspace/storefront"
PAGE_SOURCE = "export default function Page({ params }) {\n  const raw = params.query;\n  return lookup[raw];\n}\n"
PAGE_LINE = 3


def eslint_sarif(locations: list[tuple[str, int]]) -> bytes:
    """An ESLint SARIF report with one object-injection result at each (file URI, line)."""
    return json.dumps({
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "ESLint",
                "rules": [{
                    "id": "security/detect-object-injection",
                    "shortDescription": {"text": "Detects variable[key] as a left- or right-hand assignment operand."},
                }],
            }},
            "artifacts": [{"location": {"uri": uri}} for uri, _line in locations],
            "results": [
                {
                    "level": "warning",
                    "message": {"text": "Generic Object Injection Sink"},
                    "ruleId": "security/detect-object-injection",
                    "ruleIndex": 0,
                    "locations": [{"physicalLocation": {
                        "artifactLocation": {"uri": uri, "index": index},
                        "region": {"startLine": line, "startColumn": 10},
                    }}],
                }
                for index, (uri, line) in enumerate(locations)
            ],
        }],
    }).encode()


class SarifFileUriPathTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.engagement = upload_engagement(self.product, "eslint_security main")
        self.source_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.source_root, ignore_errors=True)
        page = self.source_root / "src" / "app" / "[slug]" / "page.tsx"
        page.parent.mkdir(parents=True)
        page.write_text(PAGE_SOURCE, encoding="utf-8")

    def _import(self, locations, exclusion=None):
        return import_report(
            self.engagement,
            eslint_sarif(locations),
            report_name="eslint_security.sarif",
            scan_type="SARIF",
            lead=self.user,
            exclusion=exclusion or FindingExclusion(),
        )

    def test_a_profile_path_exclusion_matches_the_decoded_file_uri_path(self):
        profile = ProjectProfile.from_dict({"paths": {"exclude": ["/src/generated code/"]}})

        test, _findings = self._import(
            [
                (f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx", PAGE_LINE),
                (f"file://{CHECKOUT_ROOT}/src/generated%20code/client.ts", 1),
            ],
            FindingExclusion(profile, CHECKOUT_ROOT),
        )

        self.assertEqual(list(Finding.objects.filter(test=test).values_list("line", flat=True)), [PAGE_LINE])

    def test_enrichment_stores_the_checkout_relative_path_and_links_it(self):
        _test, findings = self._import([(f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx", PAGE_LINE)])
        finding = findings[0]

        with patch("dojo.celery_dispatch.dojo_dispatch_task"), watson_search_context_for_task():
            enrich_finding_batch.run([finding.id], CHECKOUT_ROOT, self.pv.id)

        finding.refresh_from_db()
        self.assertEqual(finding.file_path, "src/app/[slug]/page.tsx")
        link = DojoMeta.objects.get(finding=finding, name=SOURCEFILE_LINK_META_NAME).value
        self.assertEqual(LinkBuilder.parse(link), (self.pv.id, "src/app/[slug]/page.tsx"))

    def test_the_line_fingerprint_is_read_from_the_checked_out_source(self):
        _test, findings = self._import([(f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx", PAGE_LINE)])
        finding = findings[0]

        annotated = annotate_line_hash_batch.run([finding.id], str(self.source_root), CHECKOUT_ROOT)

        self.assertEqual(annotated, 1)
        expected = hashlib.sha256(b"return lookup[raw];").hexdigest()[:16]
        self.assertEqual(DojoMeta.objects.get(finding=finding, name=AIST_LHASH_META_NAME).value, expected)

    def test_a_source_link_built_from_an_unenriched_file_uri_opens_the_decoded_path(self):
        link = LinkBuilder(self.pv.id, CHECKOUT_ROOT).build(f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx")

        self.assertEqual(LinkBuilder.parse(link), (self.pv.id, "src/app/[slug]/page.tsx"))

    def test_the_pipeline_enrich_chord_fingerprints_findings_from_the_checkout_its_scan_recorded(self):
        test, findings = self._import([(f"file://{CHECKOUT_ROOT}/src/app/%5Bslug%5D/page.tsx", PAGE_LINE)])
        pipeline = create_pipeline(
            "eslint-run",
            status=AISTStatus.UPLOADING_RESULTS,
            project_version=self.pv,
            launch_data={"trim_path": CHECKOUT_ROOT, "project_path": str(self.source_root)},
        )
        pipeline.tests.add(test)

        with (
            patch("aist.tasks.enrich.get_redis"),
            patch("aist.tasks.enrich.install_pipeline_logging"),
            warnings.catch_warnings(),
        ):
            # make_enrich_chord's errback triggers Celery's CPendingDeprecationWarning; the chord is never dispatched.
            warnings.simplefilter("ignore", CPendingDeprecationWarning)
            chord = make_enrich_chord(pipeline_id=pipeline.id)
            with patch("dojo.celery_dispatch.dojo_dispatch_task"), watson_search_context_for_task():
                chord.tasks[0].apply()

        finding = Finding.objects.get(pk=findings[0].pk)
        self.assertEqual(finding.file_path, "src/app/[slug]/page.tsx")
        expected = hashlib.sha256(b"return lookup[raw];").hexdigest()[:16]
        self.assertEqual(DojoMeta.objects.get(finding=finding, name=AIST_LHASH_META_NAME).value, expected)
