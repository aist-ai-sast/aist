"""
A project profile's path and severity exclusions keep findings out of the platform at import.

Enrichment runs afterwards inside the search-index context the base platform opens around every task; it trims
and links the findings that were imported and deletes none.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from dojo.middleware import watson_search_context_for_task
from dojo.models import DojoMeta, Finding

from aist.internal_upload import FindingExclusion
from aist.link_builder import SOURCEFILE_LINK_META_NAME, store_source_link
from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, VersionType
from aist.profile import ProjectProfile
from aist.tasks.enrich import enrich_finding_batch
from aist.test.agent_analyzer_import import write_analyzers_yaml
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.scan_import_helpers import (
    GENERIC_SCAN_TYPE,
    import_generic_report,
    upload_engagement,
    write_generic_report,
)
from aist.test.test_api import AISTApiBase

CHECKOUT_ROOT = "/srv/checkouts/tests/payments"
PROFILE = {"paths": {"exclude": ["tests/"]}, "severity": {"exclude": ["Info"]}}
KEPT_TITLE = "SQL Injection in Order Lookup"
REPORT_FINDINGS = [
    {
        "title": "SQL injection in order lookup",
        "severity": "High",
        "description": "User input reaches a raw query.",
        "file_path": f"{CHECKOUT_ROOT}/src/orders/db.py",
        "line": 42,
    },
    {
        "title": "Hardcoded password in fixture",
        "severity": "High",
        "description": "A test fixture carries a literal password.",
        "file_path": f"{CHECKOUT_ROOT}/tests/fixtures.py",
        "line": 7,
    },
    {
        "title": "Debug logging enabled",
        "severity": "info",
        "description": "Verbose logging is switched on.",
        "file_path": f"{CHECKOUT_ROOT}/src/orders/log.py",
        "line": 3,
    },
]


class ProfileValidationTests(SimpleTestCase):

    def test_a_path_exclusion_that_is_not_a_list_of_non_empty_strings_is_rejected(self):
        for exclude in ("tests/", [None], [1], [""]):
            with self.subTest(exclude=exclude), self.assertRaises(ValueError):
                ProjectProfile.validate_dict({"paths": {"exclude": exclude}})

    def test_a_list_of_path_patterns_is_accepted(self):
        ProjectProfile.validate_dict({"paths": {"exclude": ["tests/", "node_modules"]}})

    def test_a_stored_bare_string_is_read_as_one_pattern_not_as_characters(self):
        profile = ProjectProfile.from_dict({"paths": {"exclude": "tests/"}})

        self.assertTrue(profile.excludes_finding("tests/fixtures.py", "High"))
        self.assertFalse(profile.excludes_finding("src/orders/db.py", "High"))


class ProfileFindingExclusionTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.engagement = upload_engagement(self.product, "semgrep main")

    def _import(self, exclusion: FindingExclusion):
        return import_generic_report(self.engagement, REPORT_FINDINGS, lead=self.user, exclusion=exclusion)

    def test_findings_the_profile_excludes_by_path_or_severity_are_never_saved(self):
        test, findings = self._import(FindingExclusion(ProjectProfile.from_dict(PROFILE), CHECKOUT_ROOT))

        self.assertEqual([f.title for f in findings], [KEPT_TITLE])
        self.assertEqual(list(Finding.objects.filter(test=test).values_list("title", flat=True)), [KEPT_TITLE])

    def test_a_pattern_written_from_the_checkout_root_excludes_a_top_level_directory(self):
        profile = ProjectProfile.from_dict({"paths": {"exclude": ["/tests/"]}})

        _test, findings = self._import(FindingExclusion(profile, CHECKOUT_ROOT))

        self.assertEqual(
            sorted(f.file_path for f in findings),
            [f"{CHECKOUT_ROOT}/src/orders/db.py", f"{CHECKOUT_ROOT}/src/orders/log.py"],
        )

    def test_a_report_imported_without_a_profile_keeps_every_finding(self):
        test, _findings = self._import(FindingExclusion())

        self.assertEqual(Finding.objects.filter(test=test).count(), len(REPORT_FINDINGS))

    def test_enrichment_in_the_task_search_context_deletes_no_finding_the_profile_would_exclude(self):
        self.project.profile = PROFILE
        self.project.save(update_fields=["profile"])
        test, findings = self._import(FindingExclusion())

        with patch("dojo.celery_dispatch.dojo_dispatch_task"), watson_search_context_for_task():
            enriched = enrich_finding_batch.run([f.id for f in findings], CHECKOUT_ROOT, self.pv.id)

        self.assertEqual(enriched, len(REPORT_FINDINGS))
        self.assertEqual(Finding.objects.filter(test=test).count(), len(REPORT_FINDINGS))
        kept = Finding.objects.get(test=test, title=KEPT_TITLE)
        self.assertEqual(kept.file_path, "src/orders/db.py")
        self.assertTrue(DojoMeta.objects.filter(finding=kept, name=SOURCEFILE_LINK_META_NAME).exists())

    def test_enriching_a_finding_again_replaces_its_source_link(self):
        _test, findings = self._import(FindingExclusion())
        finding = next(f for f in findings if f.title == KEPT_TITLE)
        store_source_link(finding.id, "https://aist.example/stale-link")

        enrich_finding_batch.run([finding.id], CHECKOUT_ROOT, self.pv.id)

        links = list(
            DojoMeta.objects.filter(finding=finding, name=SOURCEFILE_LINK_META_NAME).values_list("value", flat=True),
        )
        self.assertEqual(len(links), 1)
        self.assertIn(f"/{self.pv.id}/", links[0])
        self.assertTrue(links[0].endswith("src/orders/db.py"))


class SastRunProfileExclusionTests(SastWorkerTestMixin, AISTApiBase):

    """A SAST run imports its analyzer report with the project's own profile, matched relative to its checkout."""

    def test_the_run_stores_only_the_findings_its_project_profile_keeps(self):
        self.project.profile = PROFILE
        self.project.save(update_fields=["profile"])
        version = AISTProjectVersion.objects.create(
            project=self.project, version_type=VersionType.GIT_HASH, version="a" * 40,
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-profile-exclusion", project=self.project, project_version=version, status=AISTStatus.FINISHED,
        )
        work_dir = Path(tempfile.mkdtemp(prefix="aist-profile-exclusion-"))
        self.addCleanup(shutil.rmtree, work_dir)
        result_file = "semgrep_result.json"
        config_path = write_analyzers_yaml(
            work_dir, name="semgrep", skill_name="", result_file=result_file, output_type=GENERIC_SCAN_TYPE,
        )
        write_generic_report(work_dir / result_file, REPORT_FINDINGS)

        self.run_sast(
            pipeline,
            project_version=version,
            launch_data={
                "output_dir": str(work_dir),
                "project_path": str(work_dir / "checkout"),
                "trim_path": CHECKOUT_ROOT,
                "tmp_analyzer_config_path": config_path,
            },
        )

        pipeline.refresh_from_db()
        self.assertEqual(
            list(Finding.objects.filter(test__in=pipeline.tests.all()).values_list("title", flat=True)),
            [KEPT_TITLE],
        )
