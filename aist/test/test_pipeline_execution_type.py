from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    PipelineExecutionType,
    VersionType,
)


class PipelineExecutionTypeTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="Pipeline execution type SLA")
        product_type = Product_Type.objects.create(name="Pipeline execution type PT")
        product = Product.objects.create(
            name="Pipeline execution type Product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        self.project = AISTProject.objects.create(product=product)
        self.trigger_version = AISTProjectVersion.objects.create(
            project=self.project,
            version="main",
            version_type=VersionType.GIT_BRANCH,
        )
        self.effective_version = AISTProjectVersion.objects.create(
            project=self.project,
            version="0123456789abcdef0123456789abcdef01234567",
            version_type=VersionType.GIT_HASH,
        )

        other_product = Product.objects.create(
            name="Other pipeline execution type Product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        self.other_project = AISTProject.objects.create(product=other_product)
        self.other_version = AISTProjectVersion.objects.create(
            project=self.other_project,
            version="other-main",
            version_type=VersionType.GIT_BRANCH,
        )

    def test_sast_requires_effective_project_version(self):
        valid = AISTPipeline(
            id="sast-valid",
            project=self.project,
            execution_type=PipelineExecutionType.SAST,
            project_version=self.effective_version,
        )
        valid.full_clean()
        valid.save()

        invalid = AISTPipeline(
            id="sast-no-version",
            project=self.project,
            execution_type=PipelineExecutionType.SAST,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            invalid.save()

    def test_dast_allows_present_or_absent_trigger_version_and_unresolved_effective_version(self):
        pipeline = AISTPipeline(
            id="dast-valid",
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            trigger_project_version=self.trigger_version,
        )
        pipeline.full_clean()
        pipeline.save()

        pipeline.project_version = self.effective_version
        pipeline.full_clean()
        pipeline.save(update_fields=["project_version"])

        # Whether the selected binding requires a repository trigger is enforced earlier by
        # PipelineLaunchRequest/AISTProjectLaunchConfig. The nullable pipeline binding also
        # permits retained pipeline history after integration teardown. A sourceless DAST
        # pipeline (no trigger version at all) is valid here.
        sourceless = AISTPipeline(
            id="dast-sourceless",
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
        )
        sourceless.full_clean()
        sourceless.save()

    def test_pipeline_cannot_mix_sast_and_dast_source_semantics(self):
        pipeline = AISTPipeline(
            id="sast-with-trigger",
            project=self.project,
            execution_type=PipelineExecutionType.SAST,
            project_version=self.effective_version,
            trigger_project_version=self.trigger_version,
        )
        with self.assertRaises(ValidationError):
            pipeline.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            pipeline.save()

    def test_manual_import_allows_initially_unresolved_version_but_not_trigger_version(self):
        pipeline = AISTPipeline(
            id="manual-valid",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
        )
        pipeline.full_clean()
        pipeline.save()

        pipeline.trigger_project_version = self.trigger_version
        with self.assertRaises(ValidationError):
            pipeline.full_clean()

    def test_all_pipeline_versions_must_belong_to_pipeline_project(self):
        for field_name in ("project_version", "trigger_project_version"):
            values = {
                "id": f"cross-project-{field_name}",
                "project": self.project,
                "execution_type": PipelineExecutionType.DAST,
                "trigger_project_version": self.trigger_version,
            }
            values[field_name] = self.other_version
            pipeline = AISTPipeline(**values)
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                pipeline.full_clean()
