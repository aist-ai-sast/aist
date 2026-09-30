from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, VersionType
from aist.pipeline_args import SastPipelineArguments

GENERATED = "#!/bin/bash\necho generated-by-claude"


class CreateForVersionTests(TestCase):

    """The generated init script is bound to exactly the branch it was generated for."""

    def setUp(self):
        sla = SLA_Configuration.objects.create(name="SLA bind")
        prod_type = Product_Type.objects.create(name="PT bind")
        self.project = self._project("Bind Product", prod_type, sla)
        self.other_project = self._project("Other Bind Product", Product_Type.objects.create(name="PT other"), sla)

        self.main = self._version(self.project, "main", VersionType.GIT_BRANCH)
        self.develop_script = AISTProjectScript.objects.create(project=self.project, content="echo develop")
        self.develop = self._version(self.project, "develop", VersionType.GIT_BRANCH, script=self.develop_script)
        self.old_hash = self._version(
            self.project, "a" * 40, VersionType.GIT_HASH, resolved_from_branch=self.main,
        )
        self.foreign_main = self._version(self.other_project, "main", VersionType.GIT_BRANCH)

    @staticmethod
    def _project(name, prod_type, sla):
        product = Product.objects.create(name=name, description="d", prod_type=prod_type, sla_configuration_id=sla.id)
        return AISTProject.objects.create(product=product, supported_languages=[], compilable=False, profile={})

    @staticmethod
    def _version(project, name, version_type, **extra):
        return AISTProjectVersion.objects.create(project=project, version=name, version_type=version_type, **extra)

    def _make_args(self, project_version: dict) -> SastPipelineArguments:
        args = SastPipelineArguments.__new__(SastPipelineArguments)
        args.project = self.project
        args.project_version = project_version
        args.pipeline_path = None
        return args

    def test_binds_only_the_requested_branch(self):
        script, created = AISTProjectScript.create_for_version(content=GENERATED, version=self.main)

        self.assertTrue(created)
        self.assertEqual(script.project_id, self.project.id)
        self.assertFalse(script.is_shared)
        self.main.refresh_from_db()
        self.assertEqual(self.main.script_id, script.id)
        for untouched, expected_script_id in (
            (self.develop, self.develop_script.id),
            (self.old_hash, None),
            (self.foreign_main, None),
        ):
            untouched.refresh_from_db()
            self.assertEqual(untouched.script_id, expected_script_id, untouched.version)

    def test_same_content_reuses_the_revision(self):
        first, _ = AISTProjectScript.create_for_version(content=GENERATED, version=self.main)
        revisions_before = self.project.script_revisions.count()

        again, created = AISTProjectScript.create_for_version(content=GENERATED, version=self.main)

        self.assertFalse(created)
        self.assertEqual(again.id, first.id)
        self.assertEqual(self.project.script_revisions.count(), revisions_before)

    def test_rejects_versions_that_are_not_branches(self):
        dast_target = self._version(self.project, "https://app.example", VersionType.DAST_TARGET)
        file_hash = self._version(self.project, "b" * 64, VersionType.FILE_HASH)
        revisions_before = self.project.script_revisions.count()

        for version in (self.old_hash, dast_target, file_hash):
            with self.subTest(version_type=version.version_type), self.assertRaises(ValidationError):
                AISTProjectScript.create_for_version(content=GENERATED, version=version)
            version.refresh_from_db()
            self.assertIsNone(version.script_id)
        self.assertEqual(self.project.script_revisions.count(), revisions_before)

    def test_generated_script_runs_on_the_next_branch_pipeline(self):
        """Generate for main → a new commit of main resolves to a GIT_HASH version → its pipeline runs the script."""
        AISTProjectScript.create_for_version(content=GENERATED, version=self.main)
        args = self._make_args(self.main.as_dict())

        resolved = args.resolve_effective_project_version(resolved_commit="c" * 40)

        self.assertEqual(resolved.version_type, VersionType.GIT_HASH)
        self.assertEqual(resolved.resolved_from_branch_id, self.main.id)
        with self.assertNoLogs("aist.pipeline_args", level="WARNING"), args.script_path_context() as path:
            self.assertEqual(Path(path).read_text(encoding="utf-8"), GENERATED)


class EffectiveScriptTests(TestCase):

    """What the UI reports as a version's script is exactly what its pipeline runs."""

    def setUp(self):
        sla = SLA_Configuration.objects.create(name="SLA eff")
        product = Product.objects.create(
            name="Eff Product", description="d", prod_type=Product_Type.objects.create(name="PT eff"),
            sla_configuration_id=sla.id,
        )
        self.project = AISTProject.objects.create(product=product, supported_languages=[], compilable=False, profile={})
        # A revision that no version uses must never be presented as running.
        AISTProjectScript.objects.create(project=self.project, content="echo unbound revision")

    def test_version_without_script_runs_shared_default(self):
        branch = AISTProjectVersion.objects.create(project=self.project, version="main")

        self.assertEqual(branch.effective_script, AISTProjectScript.get_shared_default())
        self.assertEqual(self.project.active_script, AISTProjectScript.get_shared_default())

    def test_version_with_script_runs_it(self):
        own = AISTProjectScript.objects.create(project=self.project, content="echo own")
        AISTProjectVersion.objects.create(project=self.project, version="main", script=own)

        self.assertEqual(self.project.active_script, own)

    def test_new_version_starts_from_latest_project_revision(self):
        latest = AISTProjectScript.objects.create(project=self.project, content="echo configured at creation")

        self.assertEqual(AISTProjectScript.for_new_version(self.project), latest)
