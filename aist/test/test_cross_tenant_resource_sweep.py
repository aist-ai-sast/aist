"""
Every registered resource, swept for cross-tenant leakage.

A text baseline of "known safe raw lookups" goes stale the moment the code is
reformatted or a model is renamed, and a stale entry is exactly how the next real
violation gets hidden. This test asserts the property instead of its textual shadow:
for each resource in ``RESOURCE_GETTERS``, a member of one organization must see its own
row and none of another organization's, at every named ``Action``.

It is driven by the registry, so it cannot fall behind the code: a resource added to
``RESOURCE_GETTERS`` without a fixture here fails
``test_every_registered_resource_is_covered``, and a fixture that silently builds nothing
fails the "owner sees its own" half of the sweep rather than passing vacuously.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)

from aist.authz.policy import RESOURCE_GETTERS, Action, queryset_for_action
from aist.models import (
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    DastProjectBinding,
    DastTarget,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    PipelineLaunchRequest,
    WorkItemProvider,
)

User = get_user_model()


class Tenant:

    """One organization with a full object graph, so every resource has a real row."""

    def __init__(self, label: str, sla: SLA_Configuration):
        self.label = label
        self.product_type = Product_Type.objects.create(name=label)
        self.organization = Organization.objects.create(name=label, product_type=self.product_type)
        self.product = Product.objects.create(
            name=label, description="d", prod_type=self.product_type,
            sla_configuration_id=sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product, supported_languages=["python"], compilable=False, profile={},
        )
        self.version = AISTProjectVersion.objects.create(project=self.project, version="main")
        # A SAST row must carry its effective version to satisfy
        # the aist_pipeline_execution_source_valid check constraint.
        self.pipeline = AISTPipeline.objects.create(
            id=f"pipe-{label}", project=self.project, project_version=self.version,
        )
        self.launch_config = AISTProjectLaunchConfig.objects.create(
            project=self.project, name=f"cfg-{label}",
        )
        self.launch_config_action = AISTLaunchConfigAction.objects.create(
            launch_config=self.launch_config, trigger_status="FINISHED", action_type="NOTIFY",
        )
        self.schedule = LaunchSchedule.objects.create(
            launch_config=self.launch_config, cron_expression="0 0 * * *",
        )
        self.launch_request = PipelineLaunchRequest.objects.create(project=self.project)
        self.org_integration = OrgIntegration.objects.create(
            organization=self.organization, integration_type="DAST", name=f"int-{label}",
        )
        self.work_item_provider = WorkItemProvider.objects.create(
            organization=self.organization, provider_type="JIRA", name=f"wip-{label}",
        )
        self.dast_target = DastTarget.objects.create(
            integration=self.org_integration, provider_id=f"t-{label}",
            display_name=f"target-{label}", contract_revision="1",
            capability_revision="1", schema_digest="d", last_seen_at=timezone.now(),
        )
        self.dast_binding = DastProjectBinding.objects.create(
            project=self.project, target=self.dast_target,
        )
        self.engagement = Engagement.objects.create(
            name=f"eng-{label}", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        test_type, _ = Test_Type.objects.get_or_create(name="Semgrep JSON Report")
        self.test = Test.objects.create(
            engagement=self.engagement, test_type=test_type,
            target_start=timezone.now(), target_end=timezone.now(),
        )

    def add_finding(self, reporter):
        self.finding = Finding.objects.create(
            test=self.test, title=f"f-{self.label}", severity="High",
            description="d", numerical_severity="S1", date=timezone.now(), reporter=reporter,
        )
        return self.finding

    def owned(self) -> dict:
        """The row of each registered resource that belongs to this tenant."""
        return {
            Product: self.product,
            AISTProject: self.project,
            AISTProjectVersion: self.version,
            DastProjectBinding: self.dast_binding,
            AISTPipeline: self.pipeline,
            AISTProjectLaunchConfig: self.launch_config,
            AISTLaunchConfigAction: self.launch_config_action,
            LaunchSchedule: self.schedule,
            PipelineLaunchRequest: self.launch_request,
            Finding: self.finding,
            Test: self.test,
            Engagement: self.engagement,
            Organization: self.organization,
            OrgIntegration: self.org_integration,
            WorkItemProvider: self.work_item_provider,
        }


class CrossTenantResourceSweepTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="SLA")
        self.owner_role, _ = Role.objects.get_or_create(id=Roles.Owner, defaults={"name": "Owner"})
        self.alpha = Tenant("alpha", sla)
        self.beta = Tenant("beta", sla)

        self.alpha_owner = self._owner_of(self.alpha, "alpha_owner")
        self.beta_owner = self._owner_of(self.beta, "beta_owner")
        self.alpha.add_finding(self.alpha_owner)
        self.beta.add_finding(self.beta_owner)

    def _owner_of(self, tenant: Tenant, username: str):
        user = User.objects.create_user(username, f"{username}@example.com", "pass")
        Product_Type_Member.objects.create(
            product_type=tenant.product_type, user=user, role=self.owner_role,
        )
        return user

    def test_every_registered_resource_is_covered(self):
        """The registry drives the sweep, so a new resource cannot slip through uncovered."""
        self.assertEqual(
            set(RESOURCE_GETTERS),
            set(self.alpha.owned()),
            "A resource is registered in RESOURCE_GETTERS with no fixture in this sweep "
            "(or the other way round). Add it to Tenant.owned() so it is actually checked.",
        )

    def test_no_action_leaks_a_resource_across_organizations(self):
        for resource in RESOURCE_GETTERS:
            own = self.alpha.owned()[resource]
            foreign = self.beta.owned()[resource]
            for action in Action:
                with self.subTest(resource=resource.__name__, action=action.value):
                    visible = queryset_for_action(
                        resource=resource, action=action, user=self.alpha_owner,
                    )
                    # Owner qualifies for every action, so anything missing here is tenancy,
                    # not role — and a fixture that built nothing fails right here.
                    self.assertTrue(
                        visible.filter(pk=own.pk).exists(),
                        f"{resource.__name__}: an Owner cannot see its own organization's row",
                    )
                    self.assertFalse(
                        visible.filter(pk=foreign.pk).exists(),
                        f"{resource.__name__}: leaked another organization's row",
                    )
