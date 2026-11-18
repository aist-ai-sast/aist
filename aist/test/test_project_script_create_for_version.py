from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject, AISTProjectScript, AISTProjectVersion, VersionType
from aist.pipeline_args import PipelineArguments, SastPipelineArguments
from aist.test.project_fixtures import create_project, create_project_version

GENERATED = "#!/bin/bash\necho generated-by-claude"
BASE_IMAGE = "registry.example/build/ubuntu:24.04@sha256:" + "a" * 64


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

    def test_base_image_change_creates_revision_and_identical_configuration_reuses_it(self):
        old, _ = AISTProjectScript.get_or_create_for_project(content=GENERATED, project=self.project)
        new, created = AISTProjectScript.get_or_create_for_project(
            content=GENERATED, project=self.project, base_image=BASE_IMAGE,
        )
        same, reused = AISTProjectScript.get_or_create_for_project(
            content=GENERATED, project=self.project, base_image=BASE_IMAGE,
        )
        self.assertTrue(created)
        self.assertFalse(reused)
        self.assertEqual(new, same)
        self.assertNotEqual(old.sha256, new.sha256)
        old.refresh_from_db()
        self.assertEqual(old.base_image, "")

    def test_regeneration_preserves_branch_base_image(self):
        self.main.script = AISTProjectScript.objects.create(
            project=self.project, content="echo original", base_image=BASE_IMAGE,
        )
        self.main.save(update_fields=["script"])
        script, _ = AISTProjectScript.create_for_version(content=GENERATED, version=self.main)
        self.assertEqual(script.base_image, BASE_IMAGE)

    def test_invalid_generated_image_leaves_branch_and_revision_history_unchanged(self):
        original, _ = AISTProjectScript.create_for_version(content=GENERATED, version=self.main)
        count = self.project.script_revisions.count()
        with self.assertRaises(ValidationError):
            AISTProjectScript.create_for_version(
                content="echo replacement", base_image="ubuntu:24.04", version=self.main,
            )
        self.main.refresh_from_db()
        self.assertEqual(self.main.script_id, original.id)
        self.assertEqual(self.project.script_revisions.count(), count)

    def test_queued_launch_keeps_environment_after_branch_is_edited(self):
        original = AISTProjectScript.objects.create(
            project=self.project, content="echo original", base_image=BASE_IMAGE,
        )
        self.main.script = original
        self.main.save(update_fields=["script"])
        launch = PipelineArguments.for_sast(
            project=self.project, raw_params={"project_version": self.main.id},
        )
        snapshot = launch.params_snapshot
        AISTProjectScript.create_for_version(content=GENERATED, version=self.main)
        resumed = SastPipelineArguments.from_dict(snapshot)
        with resumed.build_configuration_context() as configuration:
            self.assertEqual(Path(configuration.script_path).read_text(encoding="utf-8"), "echo original")
            self.assertEqual(configuration.base_image, BASE_IMAGE)
        resolved = resumed.resolve_effective_project_version(resolved_commit="b" * 40)
        self.assertEqual(resolved.script_id, original.id)

    def test_caller_cannot_override_frozen_revision_from_another_project(self):
        foreign = AISTProjectScript.objects.create(project=self.other_project, content="echo foreign")
        launch = PipelineArguments.for_sast(
            project=self.project,
            raw_params={"project_version": self.main.id, "script_revision_id": foreign.id},
        )
        self.assertNotEqual(launch.params_snapshot["script_revision_id"], foreign.id)

    def test_queued_shared_default_is_a_project_snapshot(self):
        shared = AISTProjectScript.get_shared_default()
        launch = PipelineArguments.for_sast(
            project=self.project, raw_params={"project_version": self.main.id},
        )
        snapshot = launch.params_snapshot
        shared.content = "echo new global default"
        shared.save(update_fields=["content"])
        resumed = SastPipelineArguments.from_dict(snapshot)
        self.assertEqual(resumed.selected_script.project_id, self.project.id)
        self.assertFalse(resumed.selected_script.is_shared)
        self.assertNotEqual(resumed.selected_script.content, shared.content)
        self.assertEqual(launch.params_snapshot, snapshot)

    @staticmethod
    def _project(name, prod_type, sla):
        return create_project(name=name, product_type=prod_type, sla=sla)

    @staticmethod
    def _version(project, name, version_type, **extra):
        return create_project_version(project=project, name=name, version_type=version_type, **extra)

    def _make_args(self, project_version: dict) -> SastPipelineArguments:
        return SastPipelineArguments(project=self.project, project_version=project_version)

    def test_binds_only_the_requested_branch(self):
        for base_image in ("", BASE_IMAGE):
            with self.subTest(base_image=base_image):
                script, created = AISTProjectScript.create_for_version(
                    content=GENERATED, base_image=base_image, version=self.main,
                )
                self.assertTrue(created)
                self.assertEqual(script.project_id, self.project.id)
                self.assertFalse(script.is_shared)
                self.assertEqual(script.content, GENERATED)
                self.assertEqual(script.base_image, base_image)
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
