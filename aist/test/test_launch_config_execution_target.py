from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    VersionType,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.test.test_dast_target_models import _integration_config, _target_wire


class LaunchConfigExecutionTargetTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="Launch target SLA")
        product_type = Product_Type.objects.create(name="Launch target PT")
        organization = Organization.objects.create(name="Launch target org", product_type=product_type)
        product = Product.objects.create(
            name="Launch target product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        self.project = AISTProject.objects.create(product=product)
        self.trigger = AISTProjectVersion.objects.create(
            project=self.project,
            version="main",
            version_type=VersionType.GIT_BRANCH,
        )
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name="Launch target DAST",
            config=_integration_config("pub_launch_target"),
            is_active=True,
        )
        target = refresh_dast_targets(
            integration,
            [DastTargetSnapshot.from_snapshot(_target_wire("app"))],
            seen_at=timezone.now(),
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="app",
            parameter_snapshot={"depth": "light"},
        )

    def test_sast_and_dast_configs_select_only_their_own_execution_target(self):
        sast = AISTProjectLaunchConfig(
            project=self.project,
            name="SAST",
            execution_type=PipelineExecutionType.SAST,
            params={"analyzers": ["semgrep"]},
        )
        dast = AISTProjectLaunchConfig(
            project=self.project,
            name="DAST",
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.trigger,
            params={"depth": "light"},
        )
        sast.full_clean()
        dast.full_clean()
        sast.save()
        dast.save()

        self.assertEqual(self.project.launch_configs.count(), 2)
        self.assertEqual(dast.dast_binding_id, self.binding.id)

    def test_invalid_execution_target_combinations_are_rejected(self):
        invalid_configs = (
            AISTProjectLaunchConfig(
                project=self.project,
                name="SAST with binding",
                execution_type=PipelineExecutionType.SAST,
                dast_binding=self.binding,
            ),
            AISTProjectLaunchConfig(
                project=self.project,
                name="DAST without binding",
                execution_type=PipelineExecutionType.DAST,
                params={"depth": "light"},
            ),
            AISTProjectLaunchConfig(
                project=self.project,
                name="DAST with analyzers",
                execution_type=PipelineExecutionType.DAST,
                dast_binding=self.binding,
                trigger_project_version=self.trigger,
                params={"analyzers": ["semgrep"], "depth": "light"},
            ),
        )
        for config in invalid_configs:
            with self.subTest(config=config.name), self.assertRaises(ValidationError):
                config.full_clean()

        with self.assertRaises(IntegrityError), transaction.atomic():
            AISTProjectLaunchConfig.objects.create(
                project=self.project,
                name="Direct invalid DAST",
                execution_type=PipelineExecutionType.DAST,
                params={"depth": "light"},
            )

    def test_dast_binding_must_be_enabled_and_belong_to_same_project(self):
        other_product = Product.objects.create(
            name="Other launch target product",
            description="",
            prod_type=self.project.product.prod_type,
            sla_configuration=self.project.product.sla_configuration,
        )
        other_project = AISTProject.objects.create(product=other_product)
        other_trigger = AISTProjectVersion.objects.create(
            project=other_project,
            version="main",
            version_type=VersionType.GIT_BRANCH,
        )
        cross_project = AISTProjectLaunchConfig(
            project=other_project,
            name="Cross project DAST",
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=other_trigger,
            params={"depth": "light"},
        )
        with self.assertRaises(ValidationError):
            cross_project.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AISTProjectLaunchConfig.objects.create(
                project=other_project,
                name="Direct cross project DAST",
                execution_type=PipelineExecutionType.DAST,
                dast_binding=self.binding,
                trigger_project_version=other_trigger,
                params={"depth": "light"},
            )

        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        disabled = AISTProjectLaunchConfig(
            project=self.project,
            name="Disabled DAST",
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.trigger,
            params={"depth": "light"},
        )
        with self.assertRaises(ValidationError):
            disabled.full_clean()
